"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ApiError, createRun, getActiveRun } from "../lib/api.mjs";

export default function TaskLauncher() {
  const router = useRouter();
  const [task, setTask] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [activeRun, setActiveRun] = useState(null);

  useEffect(() => {
    let cancelled = false;
    getActiveRun().then((run) => {
      if (!cancelled) setActiveRun(run);
    }).catch(() => {
      // The launcher remains usable when the recovery lookup is unavailable.
    });
    return () => { cancelled = true; };
  }, []);

  async function submit(event) {
    event.preventDefault();
    if (!task.trim() || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const run = await createRun(task);
      router.push(`/runs/${encodeURIComponent(run.run_id)}`);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Unable to start the run.");
      try {
        const run = await getActiveRun();
        if (run) setActiveRun(run);
      } catch {
        // Keep the original creation error visible if recovery lookup fails.
      }
      setSubmitting(false);
    }
  }

  return (
    <form className="task-card" onSubmit={submit}>
      <div className="card-kicker">NEW RUN</div>
      <label htmlFor="task">Task</label>
      <textarea
        id="task"
        data-testid="task-input"
        maxLength={10000}
        onChange={(event) => setTask(event.target.value)}
        placeholder="Describe the focused code change to prepare…"
        required
        rows={7}
        value={task}
      />
      {error ? <p className="error-banner" role="alert">{error}</p> : null}
      {activeRun ? (
        <section className="active-run-recovery" data-testid="active-run-recovery">
          <div>
            <strong>Active run found</strong>
            <p>{activeRun.task} · {activeRun.status}</p>
          </div>
          <a data-testid="resume-active-run" href={`/runs/${encodeURIComponent(activeRun.run_id)}`}>Open active run</a>
        </section>
      ) : null}
      <button className="primary-button" data-testid="run-button" disabled={submitting || !task.trim()}>
        {submitting ? "Running backend workflow…" : "Run task"}
      </button>
      {submitting ? (
        <div className="form-note" role="status" aria-live="polite">
          <div className="spinner" aria-hidden="true" />
          <p>Waiting for the backend to finish planning and checks.</p>
          <p>Live step progress is not available during creation. This page will open the run when the response arrives.</p>
        </div>
      ) : null}
      <p className="form-note">
        The current API completes planning and checks before returning this run.
      </p>
    </form>
  );
}
