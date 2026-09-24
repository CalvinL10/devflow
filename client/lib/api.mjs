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
    credentials: "same-origin",
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.method && options.method !== "GET" ? { "X-DevFlow-Request": "1" } : {}),
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
    const message = detail.message || (typeof body?.detail === "string" ? body.detail : null)
      || `Request failed with HTTP ${response.status}`;
    throw new ApiError(message, {
      status: response.status,
      code: detail.code || `http_${response.status}`,
      body,
    });
  }
  return body;
}

export function createRun(task, importId, requestId) {
  return requestJSON("/api/runs", {
    method: "POST",
    body: JSON.stringify({ task, import_id: importId, request_id: requestId }),
  });
}

export const getHealth = () => requestJSON("/api/health");
export const getProvider = () => requestJSON("/api/settings/provider");
export const saveProvider = (settings) => requestJSON("/api/settings/provider", {
  method: "PUT", body: JSON.stringify(settings),
});
export const clearProvider = () => requestJSON("/api/settings/provider", { method: "DELETE" });
export const testProvider = () => requestJSON("/api/settings/provider/test", { method: "POST", body: "{}" });
export const getProjectPreview = () => requestJSON("/api/project/preview");
export const importProject = (selection) => requestJSON("/api/imports", {
  method: "POST", body: JSON.stringify(selection),
});
export const listRuns = (offset = 0) => requestJSON(`/api/runs?limit=20&offset=${offset}`);
export const stopRun = (runId) => requestJSON(`/api/runs/${encodeURIComponent(runId)}/stop`, {
  method: "POST", body: "{}",
});
export const patchDownloadUrl = (runId) => `/api/runs/${encodeURIComponent(runId)}/patch/download`;

export function canDownloadPatch(run) {
  return Boolean(run?.import_id && run.status === "COMPLETE"
    && run.last_decision?.kind === "approve" && !run.pending_decision);
}

export const newTaskUrl = (task) => `/?task=${encodeURIComponent(task)}`;

// Keep one ID for retries of this exact launch; editing either input starts a new request.
export function launchRequest(previous, task, importId) {
  if (previous?.task === task && previous.import_id === importId) return previous;
  return { task, import_id: importId, request_id: globalThis.crypto.randomUUID() };
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
