export class ApiError extends Error {
  constructor(message, { status = 0, code = "request_failed", body = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

async function requestJSON(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    // Preserve the status when an intermediary returns a non-JSON error page.
  }
  if (!response.ok) {
    const detail = body?.error || {};
    const message = detail.message || body?.detail || `Request failed with HTTP ${response.status}`;
    throw new ApiError(message, {
      status: response.status,
      code: detail.code || `http_${response.status}`,
      body,
    });
  }
  return body;
}

export function createRun(task) {
  return requestJSON("/api/runs", {
    method: "POST",
    body: JSON.stringify({ task }),
  });
}

export function getActiveRun() {
  return requestJSON("/api/runs/active");
}

export function resumeRun(runId, decisionId) {
  return requestJSON(`/api/runs/${encodeURIComponent(runId)}/resume`, {
    method: "POST",
    body: JSON.stringify({ decision_id: decisionId }),
  });
}

export function getRun(runId) {
  return requestJSON(`/api/runs/${encodeURIComponent(runId)}`);
}

export function getPatch(runId) {
  return requestJSON(`/api/runs/${encodeURIComponent(runId)}/patch`);
}

export function decideRun(runId, kind, payload) {
  return requestJSON(`/api/runs/${encodeURIComponent(runId)}/${kind}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

const volatileDecisions = new Map();

function normalizeFeedback(feedback) {
  return String(feedback ?? "").trim() || null;
}

function reusableDecision(value, patchRevision, feedback) {
  return value
    && typeof value.decision_id === "string"
    && value.decision_id.length > 0
    && value.patch_revision === patchRevision
    && (value.feedback ?? null) === feedback;
}

export function stableDecision(runId, kind, patchRevision, feedback) {
  const key = `devflow:decision:${runId}:${kind}:${patchRevision}`;
  const normalizedFeedback = normalizeFeedback(feedback);
  let existing = null;
  try {
    const stored = window.localStorage.getItem(key);
    if (stored) {
      try {
        existing = JSON.parse(stored);
      } catch {
        // A damaged browser cache is replaced by a fresh idempotency payload.
      }
    }
  } catch {
    // Storage can be disabled by browser policy; keep this page session usable.
  }
  if (reusableDecision(existing, patchRevision, normalizedFeedback)) return existing;
  const inMemory = volatileDecisions.get(key);
  if (reusableDecision(inMemory, patchRevision, normalizedFeedback)) return inMemory;

  const payload = {
    decision_id: window.crypto.randomUUID(),
    patch_revision: patchRevision,
    feedback: normalizedFeedback,
  };
  volatileDecisions.set(key, payload);
  try {
    window.localStorage.setItem(key, JSON.stringify(payload));
  } catch {
    // The in-memory copy still makes duplicate clicks stable for this page session.
  }
  return payload;
}
