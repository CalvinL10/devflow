"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { RunStreamState, watchRun } from "../run-stream.mjs";
import { ApiError, canDownloadPatch, decideRun, getHealth, getPatch, getRun, newTaskUrl, patchDownloadUrl, resumeRun, stableDecision, stopRun } from "../lib/api.mjs";
import DiffViewer from "./diff-viewer";

const TERMINAL = new Set(["COMPLETE", "REJECTED", "CANCELED", "FAILED"]);

function connectionLabel(connection, terminal) {
  if (connection === "connected") return "Connected";
  if (connection === "reconnecting") return "Reconnecting";
  if (terminal && connection === "closed") return "Synced / closed";
  return "Closed";
}

function commandText(result) {
  return result?.command?.join(" ") || "—";
}

function CheckCard({ name, result }) {
  if (!result) return <div className="empty-panel">{name} result not available.</div>;
  return (
    <article className="check-card" data-testid={`${name.toLowerCase()}-result`}>
      <header>
        <strong>{name}</strong>
        <span className={result.passed ? "result-pass" : "result-fail"}>
          {result.passed ? "PASS" : "FAIL"}
        </span>
      </header>
      <dl>
        <div><dt>Command</dt><dd>{commandText(result)}</dd></div>
        <div><dt>Exit</dt><dd>{result.exit_code ?? "—"}</dd></div>
        <div><dt>Duration</dt><dd>{result.duration_ms} ms</dd></div>
      </dl>
      {result.stdout ? <pre className="command-output">{result.stdout}</pre> : null}
      {result.stderr ? <pre className="command-output error-output">{result.stderr}</pre> : null}
    </article>
  );
}

export default function RunWorkbench({ runId }) {
  const streamState = useRef(new RunStreamState(runId));
  const abortRef = useRef(null);
  const patchRequestRef = useRef(0);
  const [snapshot, setSnapshot] = useState(null);
  const [patch, setPatch] = useState(null);
  const [selectedPath, setSelectedPath] = useState("");
  const [events, setEvents] = useState([]);
  const [connection, setConnection] = useState("closed");
  const [connectionHistory, setConnectionHistory] = useState([]);
  const [statusHistory, setStatusHistory] = useState([]);
  const [streamError, setStreamError] = useState("");
  const [actionError, setActionError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [pendingAction, setPendingAction] = useState("");
  const [healthProvider, setHealthProvider] = useState(null);

  useEffect(() => { getHealth().then((health) => setHealthProvider(health.provider)).catch(() => {}); }, []);

  const acceptSnapshot = useCallback((next) => {
    if (streamState.current.acceptSnapshot(next)) {
      const authoritative = streamState.current.snapshot;
      setSnapshot(authoritative);
      setStatusHistory((current) => current.at(-1) === authoritative.status
        ? current : [...current, authoritative.status]);
      return authoritative;
    }
    return streamState.current.snapshot;
  }, []);

  const loadPatch = useCallback(async (expectedRevision) => {
    const requestId = ++patchRequestRef.current;
    try {
      const nextPatch = await getPatch(runId);
      if (
        nextPatch?.run_id !== runId
        || nextPatch?.patch_revision !== expectedRevision
        || !Array.isArray(nextPatch?.files)
      ) {
        throw new ApiError("Patch response does not match the current run revision.", {
          code: "patch_mismatch",
          body: nextPatch,
        });
      }
      if (requestId !== patchRequestRef.current) return null;
      setPatch(nextPatch);
      setSelectedPath((current) => (
        nextPatch.files.some((file) => file.path === current)
          ? current
          : (nextPatch.files[0]?.path || "")
      ));
      return nextPatch;
    } catch (cause) {
      if (requestId !== patchRequestRef.current) return null;
      if (!(cause instanceof ApiError && cause.status === 409)) {
        setActionError(cause instanceof Error ? cause.message : "Unable to load the patch.");
      }
      return null;
    }
  }, [runId]);

  useEffect(() => {
    const controller = new AbortController();
    abortRef.current = controller;
    watchRun({
      runId,
      signal: controller.signal,
      state: streamState.current,
      onState: (next) => {
        setSnapshot(next);
        setStatusHistory((current) => current.at(-1) === next.status
          ? current : [...current, next.status]);
        if (
          TERMINAL.has(next.status) && !next.pending_decision
          && streamState.current.lastEventSeq === next.latest_seq
        ) {
          queueMicrotask(() => controller.abort());
        }
      },
      onEvent: (event) => setEvents((current) => (
        current.some((item) => item.seq === event.seq)
          ? current
          : [...current, event].sort((left, right) => left.seq - right.seq)
      )),
      onReset: () => setEvents([]),
      onError: (error) => setStreamError(error.message || "The event stream failed."),
      onConnection: (next) => {
        setConnection(next);
        setConnectionHistory((current) => current.at(-1) === next ? current : [...current, next]);
      },
    }).catch((cause) => {
      if (!controller.signal.aborted) setStreamError(cause.message || "The event stream failed.");
    });
    return () => controller.abort();
  }, [runId]);

  useEffect(() => {
    if (snapshot?.patch_revision && patch?.patch_revision === snapshot.patch_revision) return;
    setPatch(null);
    setSelectedPath("");
    // Revision 1 is reserved before CODE creates the patch. A 409 is temporary:
    // retry a missing patch as authoritative run progress arrives at that revision.
    if (snapshot?.patch_revision) loadPatch(snapshot.patch_revision);
    else patchRequestRef.current += 1;
  }, [snapshot?.patch_revision, snapshot?.status, snapshot?.latest_seq, patch?.patch_revision, loadPatch]);

  async function refreshAuthority() {
    const latest = await getRun(runId);
    const authoritative = acceptSnapshot(latest);
    if (authoritative?.patch_revision) await loadPatch(authoritative.patch_revision);
    return authoritative;
  }

  async function decide(kind) {
    if (!snapshot?.patch_revision || pendingAction) return;
    setPendingAction(kind);
    setActionError("");
    let payload;
    try {
      payload = stableDecision(runId, kind, snapshot.patch_revision, feedback);
      const next = await decideRun(runId, kind, payload);
      acceptSnapshot(next);
    } catch (cause) {
      const message = cause instanceof ApiError && cause.code === "revision_conflict"
        ? "Stale revision: the persisted workspace or patch revision changed. This request did not apply a decision."
        : (cause instanceof Error ? cause.message : "Decision request failed.");
      setActionError(message);
      try {
        const authoritative = await refreshAuthority();
        const recorded = authoritative?.last_decision;
        const expectedStatus = { approve: "COMPLETE", reject: "REJECTED", cancel: "CANCELED" }[kind];
        // A lost response is reconciled only by this exact completed decision.
        // Another reviewer's terminal state does not make this request succeed.
        const ambiguousFailure = !(cause instanceof ApiError)
          || cause.status === 0 || cause.status >= 500;
        if (
          ambiguousFailure && payload && recorded
          && authoritative.status === expectedStatus && !authoritative.pending_decision
          && recorded.decision_id === payload.decision_id && recorded.kind === kind
          && recorded.patch_revision === payload.patch_revision
          && recorded.feedback === payload.feedback
        ) {
          setActionError("");
        }
      } catch {
        // Keep the original decision error visible if authority refresh also fails.
      }
    } finally {
      setPendingAction("");
    }
  }
  async function resumePending() {
    const decisionId = snapshot?.pending_decision?.decision_id;
    if (!decisionId || pendingAction) return;
    setPendingAction("resume");
    setActionError("");
    try {
      const next = await resumeRun(runId, decisionId);
      acceptSnapshot(next);
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "Resume request failed.");
      try {
        await refreshAuthority();
      } catch {
        // Keep the resume error visible if authority refresh also fails.
      }
    } finally {
      setPendingAction("");
    }
  }

  async function stop() {
    if (pendingAction || (snapshot?.stop_requested && !snapshot?.cleanup_pending)) return;
    setPendingAction("stop"); setActionError("");
    try { acceptSnapshot(await stopRun(runId)); }
    catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "Unable to request stop.");
      try { await refreshAuthority(); } catch { /* Retain stop error. */ }
    } finally { setPendingAction(""); }
  }

  const selectedFile = patch?.files.find((file) => file.path === selectedPath) || null;
  const latestNode = useMemo(() => [...events].reverse().find((event) => event.node)?.node || "—", [events]);
  const canResume = Boolean(snapshot?.pending_decision?.decision_id) && !pendingAction;
  const canDecide = snapshot?.status === "AWAITING_APPROVAL"
    && !snapshot?.pending_decision && !snapshot?.stop_requested
    && patch?.run_id === runId
    && patch?.patch_revision === snapshot.patch_revision;
  const terminal = TERMINAL.has(snapshot?.status) && !snapshot?.pending_decision;

  if (!snapshot) {
    return (
      <main className="loading-shell">
        <div className="spinner" />
        <p>Loading persisted run state…</p>
        {streamError ? <p className="error-banner">{streamError}</p> : null}
      </main>
    );
  }

  return (
    <main className="workbench-shell">
      <header className="topbar">
        <div>
          <Link href="/" className="brand">DEVFLOW</Link>
          <span className="run-id">{runId}</span>
        </div>
        <div className="topbar-badges">
          <span className={`connection-badge ${connection}`} data-testid="connection-status">
            <i />{connectionLabel(connection, terminal)}
          </span>
          <span className={`status-badge status-${snapshot.status.toLowerCase()}`} data-testid="run-status">
            {snapshot.status === "COMPLETE" ? "Approved — patch ready" : snapshot.status}
          </span>
        </div>
      </header>

      <section className="run-summary">
        <div>
          <span className="summary-label">Task</span>
          <h1>{snapshot.task}</h1>
        </div>
        <dl className="revision-grid">
          <div><dt>Current node</dt><dd data-testid="current-node">{terminal ? `${snapshot.status} (Finished)` : latestNode}</dd></div>
          <div><dt>Patch revision</dt><dd>{snapshot.patch_revision}</dd></div>
          <div><dt>Base workspace</dt><dd>r{snapshot.base_workspace_revision}</dd></div>
          <div><dt>Workspace head</dt><dd data-testid="workspace-revision">r{snapshot.workspace_revision}</dd></div>
        </dl>
      </section>

      <section className="run-context panel">
        <p>Provider: <strong>{snapshot.provider || healthProvider || "Unavailable"}</strong>
          {(snapshot.provider || healthProvider) === "mock" ? " · Demo mode — deterministic mock output, not a real model response." : " · No automatic mock fallback."}</p>
        <p>Source commit: <code>{snapshot.source_commit || "No imported source"}</code> · Import: {snapshot.import_id || "None"}</p>
        {["CREATED", "RUNNING"].includes(snapshot.status) ? <div>
          <button data-testid="stop-button" type="button" disabled={Boolean(pendingAction) || (snapshot.stop_requested && !snapshot.cleanup_pending)} onClick={stop}>
            {pendingAction === "stop" ? "Requesting stop…" : snapshot.cleanup_pending ? "Retry stop/cleanup" : snapshot.stop_requested ? "Stop requested" : "Stop running task"}
          </button>
          {snapshot.cleanup_pending ? <p className="error-banner" role="alert" data-testid="cleanup-warning">Cleanup pending: containers have not been confirmed stopped. This run still occupies the running slot. Retry stop/cleanup or restart the backend to retry cleanup; wait for backend confirmation before starting another run.</p>
            : <p role="status">{snapshot.stop_requested ? "Stopping at a safe execution boundary. Waiting for backend confirmation." : "Run is in progress. Stop requests execution cancellation; it is separate from canceling an approval."}</p>}
        </div> : null}
        {snapshot.error ? <p className="error-banner" role="alert"><strong>{snapshot.error.code}</strong> · {snapshot.error.phase || "run"}: {snapshot.error.message}</p> : null}
        {["FAILED", "REJECTED"].includes(snapshot.status) ? <Link href={newTaskUrl(snapshot.task)}>Copy to new task</Link> : null}
        {snapshot.status === "COMPLETE" ? <div>
          <h2>Approved — patch ready</h2>
          <p>Approval does not apply changes to your local checkout.</p>
          {canDownloadPatch(snapshot) ? <>
            <a data-testid="download-patch" href={patchDownloadUrl(runId)} download="devflow.patch">Download approved patch</a>
            <p>In a clean checkout at the source commit shown above, save the download as <code>devflow.patch</code>. Review it, then check before applying manually:</p>
            <pre>git apply --check devflow.patch{"\n"}git apply devflow.patch</pre>
            <p>If the check fails, do not force it. Resolve the checkout differences or start a new import and run.</p>
          </> : <p>Downloads require an imported run with a finalized approval. Demo-only patches are not downloadable.</p>}
        </div> : null}
      </section>

      {streamError ? <p className="error-banner" role="alert">Stream: {streamError}</p> : null}
      {actionError ? <p className="error-banner" data-testid="decision-error" role="alert">{actionError}</p> : null}

      <section className="workbench-grid">
        <aside className="side-column">
          <section className="panel">
            <div className="panel-title"><span>01</span> Changed files</div>
            <nav className="file-list" data-testid="file-list">
              {patch?.files.map((file) => (
                <button
                  className={file.path === selectedPath ? "selected" : ""}
                  data-testid="file-item"
                  key={file.path}
                  onClick={() => setSelectedPath(file.path)}
                  type="button"
                >
                  <span>{file.path}</span>
                  <b>{file.original == null ? "+" : file.modified == null ? "−" : "±"}</b>
                </button>
              ))}
            </nav>
          </section>

          <section className="panel">
            <div className="panel-title"><span>02</span> Workflow timeline</div>
            <ol className="timeline" data-testid="timeline">
              {events.map((event) => (
                <li data-seq={event.seq} data-testid={`event-${event.seq}`} key={event.seq}>
                  <i />
                  <div><strong>{event.type}</strong><span>{event.node || "run"} · #{event.seq}</span></div>
                </li>
              ))}
            </ol>
          </section>
        </aside>

        <div className="main-column">
          <DiffViewer file={selectedFile} runId={runId} />

          <section className="evidence-grid">
            <section className="panel">
              <div className="panel-title"><span>03</span> Lint / test evidence</div>
              <div className="check-grid">
                <CheckCard name="Lint" result={snapshot.check_report?.lint} />
                <CheckCard name="Test" result={snapshot.check_report?.test} />
              </div>
            </section>
            <section className="panel review-panel" data-testid="review-findings">
              <div className="panel-title"><span>04</span> Review</div>
              <div className="review-recommendation">
                <span>{snapshot.review_report?.recommendation || "pending"}</span>
                <p>{snapshot.review_report?.summary || "Review has not completed."}</p>
              </div>
              <ul className="finding-list">
                {snapshot.review_report?.findings?.length
                  ? snapshot.review_report.findings.map((finding, index) => (
                    <li key={`${finding.path}:${index}`}>
                      <b>{finding.severity}</b> {finding.path ? `${finding.path}: ` : ""}{finding.message}
                    </li>
                  ))
                  : <li>No findings.</li>}
              </ul>
            </section>
          </section>

          <section className="decision-panel panel">
            <div>
              <div className="panel-title"><span>05</span> Human decision</div>
              <p>Every action is sent to the backend with a stable idempotency key.</p>
            </div>
            <textarea
              aria-label="Decision feedback"
              disabled={!canDecide || Boolean(pendingAction)}
              maxLength={10000}
              onChange={(event) => setFeedback(event.target.value)}
              placeholder="Optional feedback"
              rows={3}
              value={feedback}
            />
            {snapshot.last_decision ? (
              <div data-testid="recorded-decision">
                <p>Recorded decision: <strong>{snapshot.last_decision.kind}</strong></p>
                <p>Recorded feedback:</p>
                <p data-testid="recorded-feedback" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                  {snapshot.last_decision.feedback ?? "No feedback provided."}
                </p>
              </div>
            ) : null}
            {snapshot.pending_decision ? (
              <div className="pending-decision" data-testid="pending-decision">
                <p>Decision <strong>{snapshot.pending_decision.kind}</strong> is recorded but not finalized.</p>
                <button data-testid="resume-button" disabled={!canResume} onClick={resumePending} type="button">
                  {pendingAction === "resume" ? "Resuming…" : "Resume recorded decision"}
                </button>
              </div>
            ) : null}
            <div className="decision-actions">
              <button data-testid="cancel-button" disabled={!canDecide || Boolean(pendingAction)} onClick={() => decide("cancel")} type="button">
                {pendingAction === "cancel" ? "Canceling…" : "Cancel approval"}
              </button>
              <button className="reject-button" data-testid="reject-button" disabled={!canDecide || Boolean(pendingAction)} onClick={() => decide("reject")} type="button">
                {pendingAction === "reject" ? "Rejecting…" : "Reject"}
              </button>
              <button className="approve-button" data-testid="approve-button" disabled={!canDecide || Boolean(pendingAction)} onClick={() => decide("approve")} type="button">
                {pendingAction === "approve" ? "Approving…" : "Approve"}
              </button>
            </div>
          </section>
        </div>
      </section>

      <section className="event-console panel">
        <div className="panel-title"><span>06</span> Durable SSE event log</div>
        <div className="event-console-body">
          {events.map((event) => <pre key={event.seq}>{JSON.stringify(event)}</pre>)}
        </div>
      </section>

      <div className="test-observability" aria-hidden="true">
        <span data-testid="connection-history">{connectionHistory.join(",")}</span>
        <span data-testid="status-history">{statusHistory.join(",")}</span>
      </div>
    </main>
  );
}
