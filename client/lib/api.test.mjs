import assert from "node:assert/strict";
import test from "node:test";

import { stableDecision } from "./api.mjs";

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
