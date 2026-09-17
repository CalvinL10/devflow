// Framework-neutral SSE consumer. No UI, storage side effects or dependencies.
export class StreamProtocolError extends Error {}

function sequence(value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new StreamProtocolError("Invalid event sequence");
  }
  return value;
}

export class RunStreamState {
  constructor(runId) {
    this.runId = runId;
    this.lastEventSeq = 0;
    this.snapshot = null;
  }

  acceptSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") throw new StreamProtocolError("Invalid snapshot");
    if (snapshot.run_id !== this.runId) throw new StreamProtocolError("Wrong run snapshot");
    const seq = sequence(snapshot.latest_seq);
    // SSE is an invalidation signal, not a second state authority. A REST
    // response that predates an observed event must never overwrite newer state.
    if (seq < this.lastEventSeq || seq < (this.snapshot?.latest_seq ?? 0)) return false;
    const revision = sequence(snapshot.workspace_revision);
    this.snapshot = {
      ...snapshot,
      // Workspace revision belongs to the workspace, not the per-run seq clock.
      workspace_revision: Math.max(revision, this.snapshot?.workspace_revision ?? 0),
    };
    return true;
  }

  acceptEvent(event) {
    if (!event || typeof event !== "object") throw new StreamProtocolError("Invalid event");
    if (event.run_id !== this.runId) throw new StreamProtocolError("Wrong run event");
    const seq = sequence(event.seq);
    if (seq <= this.lastEventSeq) return false;
    if (seq !== this.lastEventSeq + 1) throw new StreamProtocolError("Event sequence gap");
    this.lastEventSeq = seq;
    return true;
  }

  resetToSnapshot(snapshot) {
    if (!this.acceptSnapshot(snapshot)) throw new StreamProtocolError("Stale reset snapshot");
    this.lastEventSeq = snapshot.latest_seq;
  }
}

// Handles UTF-8 and CRLF split across arbitrary network chunks. Comments and
// retry-only frames do not become business events; incomplete EOF frames drop.
export async function* readSSE(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "", frame = { data: [] }, frameSize = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      while (true) {
        const end = buffer.search(/[\r\n]/);
        if (end < 0 || (buffer[end] === "\r" && end === buffer.length - 1)) break;
        const line = buffer.slice(0, end);
        buffer = buffer.slice(end + (buffer.slice(end, end + 2) === "\r\n" ? 2 : 1));
        frameSize += line.length;
        if (frameSize > 1024 * 1024) throw new StreamProtocolError("SSE frame too large");
        if (!line) {
          if (frame.data.length) yield { ...frame, data: frame.data.join("\n") };
          frame = { data: [] };
          frameSize = 0;
          continue;
        }
        if (line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const name = colon < 0 ? line : line.slice(0, colon);
        const text = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (name === "data") frame.data.push(text);
        if (name === "event" || name === "id") frame[name] = text;
      }
      if (buffer.length > 1024 * 1024) throw new StreamProtocolError("SSE line too large");
    }
  } finally {
    try { await reader.cancel(); } catch { /* already aborted by fetch */ }
    reader.releaseLock();
  }
}

class HTTPError extends Error {
  constructor(status) { super(`HTTP ${status}`); this.status = status; }
}

function notify(callback, value) {
  try { callback(value); }
  catch (cause) { throw new StreamProtocolError("Run stream callback failed", { cause }); }
}

function pause(ms, signal) {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, ms);
    signal.addEventListener("abort", finish, { once: true });
  });
}

export async function watchRun({
  runId, baseUrl = "", signal, state = new RunStreamState(runId), fetchImpl = fetch,
  onState = () => {}, onEvent = () => {}, onReset = () => {}, onError = () => {},
  onConnection = () => {},
  retryMs = 1000, maxRetryMs = 15000,
}) {
  if (!signal || state.runId !== runId) throw new StreamProtocolError("Run and AbortSignal required");
  const url = `${baseUrl}/api/runs/${encodeURIComponent(runId)}`;
  let delay = retryMs;
  let needsReset = false;
  const refresh = async (reset = false) => {
    const response = await fetchImpl(url, { signal, cache: "no-store" });
    if (!response.ok) {
      await response.body?.cancel();
      throw new HTTPError(response.status);
    }
    let snapshot;
    try { snapshot = await response.json(); }
    catch (error) {
      if (error instanceof SyntaxError) throw new StreamProtocolError("Invalid snapshot JSON");
      throw error; // response body can fail due to a transport disconnect
    }
    if (reset) {
      state.resetToSnapshot(snapshot);
      // Consumers must clear/mark their incomplete timeline on this callback.
      notify(onReset, state.snapshot);
      notify(onState, state.snapshot);
    } else if (state.acceptSnapshot(snapshot)) notify(onState, state.snapshot);
  };
  try {
    while (!signal.aborted) {
      let streamResponse;
      try {
        // Refresh also on reconnect: an earlier event may have been accepted
        // immediately before its REST refresh failed.
        await refresh(needsReset);
        needsReset = false;
        const response = await fetchImpl(`${url}/events`, {
          signal, cache: "no-store",
          headers: { Accept: "text/event-stream", "Last-Event-ID": String(state.lastEventSeq) },
        });
        streamResponse = response;
        if (response.status === 410) {
          await response.body?.cancel();
          needsReset = true;
        } else {
          if (!response.ok) {
            if (response.status === 409) {
              let failure;
              try { failure = await response.json(); } catch { /* use the HTTP status */ }
              if (failure?.error?.code === "event_store_corrupt" &&
                  typeof failure.error.message === "string") {
                notify(onError, failure.error);
                throw new StreamProtocolError(failure.error.message);
              }
            } else {
              await response.body?.cancel();
            }
            throw new HTTPError(response.status);
          }
          if (!response.headers.get("content-type")?.startsWith("text/event-stream") || !response.body) {
            await response.body?.cancel();
            throw new StreamProtocolError("Expected SSE response");
          }
          notify(onConnection, "connected");
          for await (const frame of readSSE(response.body)) {
            if (signal.aborted) break;
            let data;
            try { data = JSON.parse(frame.data); }
            catch { throw new StreamProtocolError("Invalid SSE JSON"); }
            if (!data || typeof data !== "object") throw new StreamProtocolError("Invalid SSE data");
            if (frame.event === "stream.reset") {
              if (data.error?.code !== "cursor_expired") throw new StreamProtocolError("Stream unavailable");
              needsReset = true;
              break;
            }
            if (frame.event === "stream.error") {
              if (data.error?.code !== "event_store_corrupt" ||
                  typeof data.error.message !== "string") {
                throw new StreamProtocolError("Stream unavailable");
              }
              notify(onError, data.error);
              throw new StreamProtocolError(data.error.message);
            }
            if (frame.event !== "run.event" || frame.id !== String(data.seq)) {
              throw new StreamProtocolError("Invalid SSE event identity");
            }
            if (state.acceptEvent(data)) {
              notify(onEvent, data);
              // Replayed events already covered by REST cannot invalidate it.
              // Still deliver every event and signal catch-up so terminal UIs close
              // only after consuming the complete timeline.
              if (data.seq > state.snapshot.latest_seq) await refresh();
              else if (data.seq === state.snapshot.latest_seq) notify(onState, state.snapshot);
            }
            if (signal.aborted) break;
            delay = retryMs;
          }
        }
      } catch (error) {
        if (signal.aborted) break;
        if (error instanceof StreamProtocolError ||
            (error instanceof HTTPError && error.status < 500 && ![408, 429].includes(error.status))) {
          throw error;
        }
        // Network/5xx failures retry from the last accepted business event.
      } finally {
        // Covers errors before the parser acquires its reader as well (for
        // example a connection-status callback throwing).
        if (streamResponse?.body && !streamResponse.body.locked) {
          try { await streamResponse.body.cancel(); } catch { /* already disconnected */ }
        }
      }
      if (!signal.aborted) {
        notify(onConnection, "reconnecting");
        await pause(delay, signal);
        delay = Math.min(maxRetryMs, Math.max(retryMs, delay * 2));
      }
    }
  } finally {
    onConnection("closed");
  }
  return state;
}
