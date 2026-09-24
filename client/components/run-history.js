"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listRuns, newTaskUrl } from "../lib/api.mjs";

export default function RunHistory() {
  const [runs, setRuns] = useState([]);
  const [offset, setOffset] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function load(next = 0) {
    setBusy(true); setError("");
    try {
      const result = await listRuns(next);
      setRuns((current) => next === 0 ? result.runs : [...current, ...result.runs.filter((run) => !current.some((item) => item.run_id === run.run_id))]);
      setOffset(result.next_offset);
    } catch { setError("Run history unavailable. Retry to reload persisted runs."); }
    finally { setBusy(false); }
  }
  useEffect(() => { load(); }, []);
  return <section className="setup-panel panel" aria-label="Run history">
    <h2>Run history</h2>
    <button disabled={busy} onClick={() => load()} type="button">Refresh history</button>
    {error ? <p role="alert" className="error-banner">{error}</p> : null}
    {!runs.length && !busy && !error ? <p>No runs yet.</p> : null}
    <ul className="history-list">{runs.map((run) => <li key={run.run_id}>
      <Link href={`/runs/${encodeURIComponent(run.run_id)}`}>{run.task}</Link>
      <span>{run.status === "COMPLETE" ? "Approved — patch ready" : run.status} · {run.provider || "Provider unavailable"}</span>
      {run.source_commit ? <code>{run.source_commit}</code> : null}
      {["FAILED", "REJECTED"].includes(run.status) ? <Link href={newTaskUrl(run.task)}>Copy to new task</Link> : null}
    </li>)}</ul>
    {offset != null ? <button disabled={busy} onClick={() => load(offset)} type="button">Load more runs</button> : null}
    {busy ? <p role="status">Loading run history…</p> : null}
  </section>;
}
