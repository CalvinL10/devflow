import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, canDownloadPatch, clearProvider, createRun, decideRun, getHealth,
  getProjectPreview, getProvider, importProject, launchRequest, listRuns, newTaskUrl,
  patchDownloadUrl, resumeRun, saveProvider, stableDecision, stopRun, testProvider } from "./api.mjs";

function installWindow(storage) {
  let nextId = 0;
  globalThis.window = {
    crypto: { randomUUID: () => `decision-${++nextId}` },
    localStorage: storage,
  };
}

test("decision payloads reuse only matching feedback and recover from damaged storage", () => {
  const values = new Map();
  installWindow({
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  });

  const first = stableDecision("run-cache", "approve", 1, " reviewed ");
  const duplicate = stableDecision("run-cache", "approve", 1, "reviewed");
  assert.deepEqual(duplicate, first);

  const changed = stableDecision("run-cache", "approve", 1, "updated feedback");
  assert.notEqual(changed.decision_id, first.decision_id);
  assert.equal(changed.feedback, "updated feedback");

  values.set("devflow:decision:run-damaged:reject:2", "{broken");
  const recovered = stableDecision("run-damaged", "reject", 2, "");
  assert.equal(recovered.patch_revision, 2);
  assert.equal(recovered.feedback, null);
  assert.deepEqual(JSON.parse(values.get("devflow:decision:run-damaged:reject:2")), recovered);
});

test("decision payloads stay stable for the page session when localStorage is unavailable", () => {
  installWindow({
    getItem() { throw new Error("storage denied"); },
    setItem() { throw new Error("storage denied"); },
  });

  const first = stableDecision("run-memory", "cancel", 3, "same");
  const duplicate = stableDecision("run-memory", "cancel", 3, "same");
  const changed = stableDecision("run-memory", "cancel", 3, "different");
  assert.deepEqual(duplicate, first);
  assert.notEqual(changed.decision_id, first.decision_id);
});


test("a read-only stale localStorage entry does not break in-memory retries", () => {
  const stale = JSON.stringify({ decision_id: "old", patch_revision: 4, feedback: "old feedback" });
  installWindow({
    getItem: () => stale,
    setItem() { throw new Error("read only"); },
  });

  const first = stableDecision("run-read-only", "approve", 4, "new feedback");
  const duplicate = stableDecision("run-read-only", "approve", 4, "new feedback");
  assert.deepEqual(duplicate, first);
  assert.notEqual(first.decision_id, "old");
});

test("beta mutations send same-origin JSON with CSRF header and exact request data", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (path, options) => {
    calls.push({ path, ...options });
    return new Response(JSON.stringify({ ok: true }), { status: 202 });
  });
  const provider = { base_url: "https://example.test/v1", model: "model", allow_local_http: false, api_key: "secret" };
  await saveProvider(provider);
  await clearProvider();
  await testProvider();
  await importProject({ commit: "source-commit", dependency_source: "pyproject.toml", extras: ["test"] });
  await createRun("Task", "import-1", "request-1");
  await stopRun("run/1");
  await decideRun("run-1", "cancel", { decision_id: "d", patch_revision: 1 });
  await resumeRun("run-1", "d");
  for (const call of calls) {
    assert.equal(call.credentials, "same-origin");
    assert.equal(call.headers["X-DevFlow-Request"], "1");
    assert.ok(call.path.startsWith("/api/"));
    if (call.body) assert.equal(call.headers["Content-Type"], "application/json");
  }
  assert.deepEqual(JSON.parse(calls[0].body), provider);
  assert.deepEqual(JSON.parse(calls[4].body), { task: "Task", import_id: "import-1", request_id: "request-1" });
  assert.equal(calls[5].path, "/api/runs/run%2F1/stop");
  assert.equal(calls[1].method, "DELETE");
});

test("beta reads and pagination use same-origin GET; errors retain HTTP status", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (path, options) => {
    calls.push({ path, ...options });
    return new Response("{}", { status: 200 });
  });
  await getHealth(); await getProvider(); await getProjectPreview(); await listRuns(20);
  assert.equal(calls.at(-1).path, "/api/runs?limit=20&offset=20");
  for (const call of calls) {
    assert.equal(call.headers["X-DevFlow-Request"], undefined);
    assert.equal(call.credentials, "same-origin");
  }
  t.mock.method(globalThis, "fetch", async () => new Response("upstream failure", { status: 502 }));
  await assert.rejects(getProvider(), (error) => error instanceof ApiError && error.status === 502);
});

test("launch retries retain their ID, but changed task or import starts a new request", () => {
  const first = launchRequest(null, "task", null);
  assert.strictEqual(launchRequest(first, "task", null), first);
  assert.notEqual(launchRequest(first, "other", null).request_id, first.request_id);
  assert.notEqual(launchRequest(first, "task", "import-1").request_id, first.request_id);
});

test("only finalized approved imported runs expose patch download; task copy is URL-encoded", () => {
  const run = { run_id: "run-1", status: "COMPLETE", import_id: "import-1", last_decision: { kind: "approve" } };
  assert.equal(canDownloadPatch(run), true);
  for (const changes of [{ import_id: null }, { status: "RUNNING" }, { status: "REJECTED" },
    { pending_decision: {} }, { last_decision: null }, { last_decision: { kind: "reject" } }]) {
    assert.equal(canDownloadPatch({ ...run, ...changes }), false);
  }
  assert.equal(patchDownloadUrl("run/1"), "/api/runs/run%2F1/patch/download");
  const task = "Fix & review <script> + résumé?";
  assert.equal(new URL(newTaskUrl(task), "http://localhost").searchParams.get("task"), task);
});
