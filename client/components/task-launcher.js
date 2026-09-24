"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { createRun, getActiveRun, getHealth, launchRequest } from "../lib/api.mjs";
import ProviderSetup from "./provider-setup";
import ProjectImport from "./project-import";
import RunHistory from "./run-history";

export default function TaskLauncher({ initialTask = "" }) {
  const router = useRouter();
  const [task, setTask] = useState(initialTask);
  const [setupBusy, setSetupBusy] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [activeRun, setActiveRun] = useState(null);
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState("");
  const [imported, setImported] = useState(null);
  const [importFormVersion, setImportFormVersion] = useState(0);
  const request = useRef(null);
  const inFlight = useRef(false);
  const demo = health?.provider === "mock";
  const refreshHealth = useCallback(async () => {
    setHealth(null); setHealthError("");
    try { setHealth(await getHealth()); }
    catch { setHealthError("Cannot verify provider mode. Retry before starting a run."); }
  }, []);
  const providerChanged = async () => {
    setImported(null); setImportFormVersion((version) => version + 1);
    request.current = null; await refreshHealth();
  };

  useEffect(() => {
    refreshHealth();
    getActiveRun().then(setActiveRun).catch(() => {});
  }, [refreshHealth]);

  async function submit(event) {
    event.preventDefault();
    if (!task.trim() || inFlight.current || setupBusy || !health || (!demo && !imported)) return;
    inFlight.current = true; setSubmitting(true); setError("");
    request.current = launchRequest(request.current, task.trim(), imported?.import_id ?? null);
    try {
      const payload = request.current;
      const run = await createRun(payload.task, payload.import_id, payload.request_id);
      router.push(`/runs/${encodeURIComponent(run.run_id)}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to start the run. Retry uses the same request ID.");
      try { setActiveRun(await getActiveRun()); } catch { /* Keep original error. */ }
      inFlight.current = false; setSubmitting(false);
    }
  }

  return <div className="beta-flow">
    <section className="mode-banner" aria-label="Provider mode">
      <strong>{health ? (demo ? "Demo mode — mock provider" : `Provider: ${health.provider}`) : "Checking provider mode…"}</strong>
      <p>{demo ? "Deterministic demo output, not a real model response. Real-provider failures never switch to demo automatically." : "Import source explicitly before starting. Provider errors remain visible; no mock fallback."}</p>
      {healthError ? <p role="alert">{healthError}</p> : null}
      <button type="button" disabled={submitting} onClick={refreshHealth}>Check provider mode</button>
    </section>
    <div className="setup-grid">
      <ProviderSetup onChanged={providerChanged} onBusyChange={setSetupBusy} disabled={submitting || setupBusy} />
      <ProjectImport key={importFormVersion} demo={demo} imported={imported} onImported={setImported} onBusyChange={setSetupBusy} disabled={submitting || setupBusy} />
    </div>
    <form className="task-card" onSubmit={submit}>
      <div className="card-kicker">3. NEW RUN</div>
      <label htmlFor="task">Task</label>
      <textarea id="task" data-testid="task-input" disabled={submitting} maxLength={10000}
        onChange={(event) => { setTask(event.target.value); request.current = null; }}
        placeholder="Describe the focused code change to prepare…" required rows={5} value={task} />
      {!demo && !imported ? <p className="form-note">Import source with code-sharing consent to enable a real-provider run.</p> : null}
      {error ? <p className="error-banner" role="alert">{error} Retry keeps the same request ID for this task and import.</p> : null}
      {activeRun ? <section className="active-run-recovery" data-testid="active-run-recovery">
        <strong>Active run found</strong><p>{activeRun.task} · {activeRun.status}</p>
        <a data-testid="resume-active-run" href={`/runs/${encodeURIComponent(activeRun.run_id)}`}>Open active run</a>
      </section> : null}
      <button className="primary-button" data-testid="run-button" disabled={submitting || setupBusy || !task.trim() || !health || (!demo && !imported)}>
        {submitting ? "Starting run…" : demo ? "Run demo task" : "Run task"}
      </button>
      {submitting ? <div className="form-note" role="status" aria-live="polite"><div className="spinner" aria-hidden="true" /><p>Starting run. Opening live progress as soon as the backend accepts it.</p></div> : null}
      <p className="form-note">Runs continue asynchronously. Review live progress, stop execution, and approve a patch before downloading. Your checkout is never applied automatically.</p>
    </form>
    <RunHistory />
  </div>;
}
