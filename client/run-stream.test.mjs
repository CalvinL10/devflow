import assert from "node:assert/strict";
import test from "node:test";
import { RunStreamState, StreamProtocolError, readSSE, watchRun } from "./run-stream.mjs";

const snapshot = (seq, status = "RUNNING", revision = 0) => ({
  run_id: "run-1", latest_seq: seq, status, workspace_revision: revision,
});
const event = (seq) => ({ run_id: "run-1", seq, type: "node.started", payload: {} });
const frame = (seq) => `id: ${seq}\nevent: run.event\ndata: ${JSON.stringify(event(seq))}\n\n`;
const sse = (text) => new Response(text, { headers: { "Content-Type": "text/event-stream" } });

test("duplicate and stale events cannot pollute newer REST state", () => {
  const state = new RunStreamState("run-1");
  state.acceptSnapshot(snapshot(5, "COMPLETE", 1));
  assert.equal(state.acceptEvent(event(1)), true);
  assert.equal(state.acceptEvent(event(1)), false);
  assert.equal(state.acceptSnapshot(snapshot(3)), false);
  assert.equal(state.snapshot.status, "COMPLETE");
  assert.equal(state.acceptSnapshot(snapshot(5, "COMPLETE", 0)), true);
  assert.equal(state.snapshot.workspace_revision, 1);
  assert.throws(() => state.acceptEvent(event(3)), /gap/);
  assert.throws(() => state.acceptEvent({ ...event(2), run_id: "other" }), /Wrong run/);
});

test("REST begun before SSE cannot overwrite newer observed event", () => {
  const state = new RunStreamState("run-1");
  state.acceptSnapshot(snapshot(1));
  state.acceptEvent(event(1));
  state.acceptEvent(event(2));
  assert.equal(state.acceptSnapshot(snapshot(1)), false);
  assert.equal(state.acceptSnapshot(snapshot(2, "AWAITING_APPROVAL")), true);
  assert.equal(state.snapshot.status, "AWAITING_APPROVAL");
});

test("UTF-8, multiline data, CRLF and heartbeats across single-byte chunks", async () => {
  const bytes = new TextEncoder().encode(
    ': heartbeat\r\n\r\nretry: 1000\r\n\r\nid: 1\r\nevent: run.event\r\ndata: {"text":\r\ndata: "中文"}\r\n\r\ndata: unfinished',
  );
  let canceled = false;
  const body = new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      controller.close();
    },
    cancel() { canceled = true; },
  });
  const frames = [];
  for await (const value of readSSE(body)) frames.push(value);
  assert.deepEqual(frames, [{ id: "1", event: "run.event", data: '{"text":\n"中文"}' }]);
  assert.equal(body.locked, false);
  assert.equal(canceled, false); // naturally closed, no outstanding source
});

test("reconnect cursor advances only for accepted events; duplicate callback suppressed", async () => {
  const controller = new AbortController();
  const received = [], cursors = [], statuses = [];
  let connections = 0;
  const fetchImpl = async (url, options) => {
    if (!url.endsWith("/events")) return Response.json(snapshot(6, "COMPLETE"));
    cursors.push(options.headers["Last-Event-ID"]);
    connections++;
    return sse(connections === 1 ? frame(1) + frame(2) + frame(3) : frame(3) + frame(4) + frame(5) + frame(6));
  };
  const state = await watchRun({
    runId: "run-1", signal: controller.signal, fetchImpl, retryMs: 1,
    onEvent(value) { received.push(value.seq); if (value.seq === 6) controller.abort(); },
    onState(value) { statuses.push(value.status); },
  });
  assert.deepEqual(cursors, ["0", "3"]);
  assert.deepEqual(received, [1, 2, 3, 4, 5, 6]);
  assert.ok(statuses.every((status) => status === "COMPLETE"));
  assert.equal(state.lastEventSeq, 6);
});

for (const inStream of [false, true]) {
  test(`expired cursor resyncs from REST and not an error-frame id (in-stream=${inStream})`, async () => {
    const controller = new AbortController();
    const cursors = [], resets = [];
    let connections = 0;
    const fetchImpl = async (url, options) => {
      if (!url.endsWith("/events")) return Response.json(snapshot(connections ? 10 : 1));
      cursors.push(options.headers["Last-Event-ID"]);
      if (connections++ === 0) return inStream
        ? sse('event: stream.reset\ndata: {"error":{"code":"cursor_expired","latest_seq":999}}\n\n')
        : Response.json({ error: { code: "cursor_expired", latest_seq: 999 } }, { status: 410 });
      return sse(frame(11));
    };
    const state = await watchRun({
      runId: "run-1", signal: controller.signal, fetchImpl, retryMs: 1,
      onReset(value) { resets.push(value.latest_seq); }, onEvent() { controller.abort(); },
    });
    assert.deepEqual(cursors, ["0", "10"]);
    assert.deepEqual(resets, [10]);
    assert.equal(state.lastEventSeq, 11);
  });
}

test("transient snapshot failure after accepting event refreshes on reconnect", async () => {
  const controller = new AbortController();
  const cursors = [];
  let snapshots = 0;
  const state = new RunStreamState("run-1");
  const fetchImpl = async (url, options) => {
    if (!url.endsWith("/events")) {
      snapshots++;
      if (snapshots === 2) throw new TypeError("network interrupted");
      return Response.json(snapshot(snapshots === 1 ? 0 : 2, "COMPLETE"));
    }
    cursors.push(options.headers["Last-Event-ID"]);
    return sse(frame(cursors.length));
  };
  await watchRun({
    runId: "run-1", state, signal: controller.signal, fetchImpl, retryMs: 1,
    onEvent(value) { if (value.seq === 2) controller.abort(); },
  });
  assert.deepEqual(cursors, ["0", "1"]);
  assert.equal(state.snapshot.latest_seq, 2);
});

test("abort clears reconnect delay and releases stream reader", async () => {
  const controller = new AbortController();
  let canceled = false;
  const stream = new ReadableStream({
    start(c) { c.enqueue(new TextEncoder().encode(frame(1))); },
    cancel() { canceled = true; },
  });
  const fetchImpl = async (url) => url.endsWith("/events")
    ? new Response(stream, { headers: { "Content-Type": "text/event-stream" } })
    : Response.json(snapshot(1));
  await watchRun({
    runId: "run-1", signal: controller.signal, fetchImpl,
    onEvent() { controller.abort(); },
  });
  assert.equal(canceled, true);
  assert.equal(stream.locked, false);
});

test("wrong run/gap and non-retryable HTTP fail explicitly", async () => {
  for (const response of [sse(frame(2)), new Response("missing", { status: 404 })]) {
    const fetchImpl = async (url) => url.endsWith("/events") ? response : Response.json(snapshot(0));
    await assert.rejects(watchRun({
      runId: "run-1", signal: new AbortController().signal, fetchImpl,
    }), /gap|HTTP 404/);
  }
  assert.throws(() => new RunStreamState("run-1").acceptSnapshot(snapshot(-1)), StreamProtocolError);
});

test("malformed data and callback failures are not swallowed as network failures", async () => {
  for (const body of ["null", "{broken"] ) {
    const fetchImpl = async (url) => url.endsWith("/events")
      ? sse(`id: 1\nevent: run.event\ndata: ${body}\n\n`) : Response.json(snapshot(0));
    await assert.rejects(watchRun({
      runId: "run-1", signal: new AbortController().signal, fetchImpl,
    }), StreamProtocolError);
  }
  await assert.rejects(watchRun({
    runId: "run-1", signal: new AbortController().signal,
    fetchImpl: async () => Response.json(snapshot(0)),
    onState() { throw new Error("consumer failed"); },
  }), /callback failed/);
});

test("persisted stream error is reported once and not retried", async () => {
  const storedError = {
    code: "event_store_corrupt",
    message: "stored run event payload is invalid",
    event_seq: 2,
  };
  const reported = [];
  let eventRequests = 0;
  const fetchImpl = async (url) => {
    if (!url.endsWith("/events")) return Response.json(snapshot(1));
    eventRequests++;
    return sse(`event: stream.error\ndata: ${JSON.stringify({ error: storedError })}\n\n`);
  };

  await assert.rejects(watchRun({
    runId: "run-1", signal: new AbortController().signal, fetchImpl,
    onError(value) { reported.push(value); },
  }), /stored run event payload is invalid/);
  assert.equal(eventRequests, 1);
  assert.deepEqual(reported, [storedError]);
});

test("persisted replay error response is reported once and not retried", async () => {
  const storedError = {
    code: "event_store_corrupt",
    message: "stored run event payload is invalid",
    event_seq: 1,
  };
  const reported = [];
  let eventRequests = 0;
  const fetchImpl = async (url) => {
    if (!url.endsWith("/events")) return Response.json(snapshot(1));
    eventRequests++;
    return Response.json({ error: storedError }, { status: 409 });
  };

  await assert.rejects(watchRun({
    runId: "run-1", signal: new AbortController().signal, fetchImpl,
    onError(value) { reported.push(value); },
  }), /stored run event payload is invalid/);
  assert.equal(eventRequests, 1);
  assert.deepEqual(reported, [storedError]);
});

test("abort during backoff stops further requests", async () => {
  const controller = new AbortController();
  let calls = 0;
  await watchRun({
    runId: "run-1", signal: controller.signal,
    fetchImpl: async () => { calls++; throw new TypeError("offline"); },
    onConnection(value) { if (value === "reconnecting") setTimeout(() => controller.abort(), 1); },
  });
  assert.equal(calls, 1);
});

test("failure before acquiring SSE reader still closes the transport", async () => {
  let canceled = false;
  const body = new ReadableStream({ cancel() { canceled = true; } });
  await assert.rejects(watchRun({
    runId: "run-1", signal: new AbortController().signal,
    fetchImpl: async (url) => url.endsWith("/events")
      ? new Response(body, { headers: { "Content-Type": "text/event-stream" } })
      : Response.json(snapshot(0)),
    onConnection(value) { if (value === "connected") throw new Error("render failed"); },
  }), /callback failed/);
  assert.equal(canceled, true);
});

for (const liveEvent of [false, true]) {
  test(`replay uses its authoritative snapshot without per-event REST requests (live=${liveEvent})`, async () => {
    const controller = new AbortController();
    const state = new RunStreamState("run-1");
    const received = [];
    let reads = 0;
    const target = liveEvent ? 17 : 16;
    await watchRun({
      runId: "run-1", state, signal: controller.signal,
      fetchImpl: async (url) => {
        if (url.endsWith("/events")) {
          return sse(Array.from({ length: target }, (_, i) => frame(i + 1)).join(""));
        }
        reads++;
        return Response.json(snapshot(reads === 1 ? 16 : target, "COMPLETE"));
      },
      onEvent(value) { received.push(value.seq); },
      onState(value) {
        // Same termination condition as the workbench: keep the full timeline.
        if (state.lastEventSeq === target && value.latest_seq === target) controller.abort();
      },
    });
    assert.deepEqual(received, Array.from({ length: target }, (_, i) => i + 1));
    assert.equal(state.snapshot.latest_seq, target);
    assert.equal(reads, liveEvent ? 2 : 1);
  });
}
