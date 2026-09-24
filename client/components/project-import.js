"use client";

import { useEffect, useState } from "react";
import { getProjectPreview, importProject } from "../lib/api.mjs";

function describe(item) { return typeof item === "string" ? item : JSON.stringify(item); }

export default function ProjectImport({ demo, imported, onImported, onBusyChange, disabled }) {
  const [preview, setPreview] = useState(null);
  const [source, setSource] = useState("");
  const [extras, setExtras] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dependency = preview?.dependency_details?.find((item) => item.source === source);
  const sourceHasErrors = Boolean(dependency?.errors?.length);

  useEffect(() => {
    let cancelled = false;
    getProjectPreview().then((value) => { if (!cancelled) setPreview(value); })
      .catch(() => { if (!cancelled) setError("Project preview unavailable. No project has been imported."); });
    return () => { cancelled = true; };
  }, []);

  function invalidate() { onImported(null); setConsent(false); }

  async function refresh() {
    invalidate(); setPreview(null); setSource(""); setExtras(""); setError(""); setBusy(true); onBusyChange(true);
    try { setPreview(await getProjectPreview()); }
    catch { setError("Project preview unavailable. No project has been imported."); }
    finally { setBusy(false); onBusyChange(false); }
  }

  async function submit(event) {
    event.preventDefault();
    if (!consent || !preview?.commit || preview.errors?.length || sourceHasErrors || busy || disabled) return;
    setBusy(true); onBusyChange(true); setError(""); onImported(null);
    try {
      const result = await importProject({ commit: preview.commit, dependency_source: source || null,
        extras: extras.split(",").map((item) => item.trim()).filter(Boolean) });
      onImported(result);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Import failed. Refresh preview and retry.");
    } finally { setBusy(false); onBusyChange(false); }
  }

  return (
    <section className="setup-panel panel" aria-label="Project import">
      <h2>2. Review and import project</h2>
      <p>Only the selected committed source is imported. Review the file list before sharing code with your configured provider.</p>
      {demo ? <p className="form-note">Demo can run without a project preview or import. Demo patches cannot be downloaded without an approved imported run.</p> : null}
      <button type="button" disabled={busy || disabled} onClick={refresh}>Refresh project preview</button>
      {error ? <p className="error-banner" role="alert">{error}</p> : null}
      {preview ? <form onSubmit={submit}>
        <p className="commit-label">Source commit: <code>{preview.commit || "Unavailable"}</code></p>
        <details open><summary>Files to share ({preview.files?.length || 0})</summary>
          <ul className="preview-list">{preview.files?.map((file) => <li key={file}>{file}</li>)}</ul>
        </details>
        <details><summary>Excluded ({preview.excluded?.length || 0})</summary>
          <ul className="preview-list">{preview.excluded?.map((item, index) => <li key={index}>{describe(item)}</li>)}</ul>
        </details>
        {preview.errors?.length ? <div role="alert" className="error-banner"><strong>Resolve preview errors before importing.</strong><ul>{preview.errors.map((item, index) => <li key={index}>{describe(item)}</li>)}</ul></div> : null}
        {preview.warnings?.length ? <section aria-label="Preview warnings" className="form-note"><h3>Preview warnings</h3><ul>{preview.warnings.map((item, index) => <li key={index}>{item}</li>)}</ul></section> : null}
        <fieldset disabled={busy || disabled}>
          <label htmlFor="dependency-source">Dependency source</label>
          <select id="dependency-source" value={source} onChange={(event) => { setSource(event.target.value); setExtras(""); invalidate(); }}>
            <option value="">None</option>
            {preview.dependency_sources?.map((file) => <option key={file} value={file}>{file}</option>)}
          </select>
          {dependency ? <section aria-label="Selected dependency details">
            <h3>Requirements</h3>
            {dependency.requirements?.length ? <ul>{dependency.requirements.map((item, index) => <li key={index}>{describe(item)}</li>)}</ul> : <p>No requirements listed.</p>}
            <h3>Available extras</h3>
            {dependency.extras?.length ? <ul>{dependency.extras.map((item) => <li key={item}>{item}</li>)}</ul> : <p>No extras available.</p>}
            {sourceHasErrors ? <div role="alert" className="error-banner"><strong>Resolve dependency source errors or choose another source before importing.</strong><ul>{dependency.errors.map((item, index) => <li key={index}>{describe(item)}</li>)}</ul></div> : null}
          </section> : null}
          <label htmlFor="dependency-extras">Dependency extras (comma-separated, optional)</label>
          <input id="dependency-extras" value={extras} onChange={(event) => { setExtras(event.target.value); invalidate(); }} placeholder="dev, test" />
          <label className="checkbox-row"><input type="checkbox" checked={consent} onChange={(event) => { setConsent(event.target.checked); onImported(null); }} />I consent to sharing the listed source code with the configured provider for this import and its runs.</label>
          <button type="submit" disabled={!consent || !preview.commit || Boolean(preview.errors?.length) || sourceHasErrors}>{busy ? "Importing…" : "Import selected source"}</button>
        </fieldset>
      </form> : null}
      {imported ? <p role="status">Imported {imported.import_id} · commit {imported.commit}</p> : null}
    </section>
  );
}
