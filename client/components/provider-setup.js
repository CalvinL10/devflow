"use client";

import { useEffect, useState } from "react";
import { clearProvider, getProvider, saveProvider, testProvider } from "../lib/api.mjs";

export default function ProviderSetup({ onChanged, onBusyChange, disabled }) {
  const [settings, setSettings] = useState(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [allowLocal, setAllowLocal] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function accept(value) {
    setSettings(value);
    setBaseUrl(value.base_url || "");
    setModel(value.model || "");
    setAllowLocal(Boolean(value.allow_local_http));
  }
  useEffect(() => {
    let cancelled = false;
    getProvider().then((value) => { if (!cancelled) accept(value); })
      .catch(() => { if (!cancelled) setError("Provider settings unavailable. Retry loading settings."); });
    return () => { cancelled = true; };
  }, []);

  async function act(kind) {
    if (busy) return;
    setBusy(kind); onBusyChange(true); setError(""); setNotice("");
    try {
      if (kind === "load") accept(await getProvider());
      if (kind === "save") {
        await saveProvider({ base_url: baseUrl.trim(), model: model.trim(),
          allow_local_http: allowLocal, ...(apiKey ? { api_key: apiKey } : {}) });
        setApiKey("");
        accept(await getProvider());
        setNotice("Provider settings saved. No automatic fallback to demo.");
      }
      if (kind === "clear") {
        await clearProvider(); setApiKey("");
        accept(await getProvider());
        setNotice("Provider settings cleared.");
      }
      if (kind === "test") {
        const result = await testProvider();
        if (result?.ok === false || result?.success === false || result?.error) {
          throw new Error("Provider test failed.");
        }
        setNotice("Provider connection test succeeded. No project code was sent.");
      }
    } catch {
      // Do not render provider responses that might echo credentials.
      setError(`Unable to ${kind} provider settings. Check the server configuration and retry.`);
    } finally {
      // A lost mutation response may still have changed the provider: renew import consent.
      if (kind === "save" || kind === "clear") await onChanged();
      setBusy(""); onBusyChange(false);
    }
  }

  return (
    <section className="setup-panel panel" aria-label="Provider setup">
      <h2>1. Configure provider</h2>
      <p>Credentials stay on the server after saving, never in browser storage.</p>
      <form onSubmit={(event) => { event.preventDefault(); act("save"); }}>
        <fieldset disabled={Boolean(busy) || disabled}>
          <label htmlFor="provider-url">Base URL</label>
          <input id="provider-url" type="url" required value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://provider.example/v1" />
          <label htmlFor="provider-model">Model</label>
          <input id="provider-model" required value={model} onChange={(event) => setModel(event.target.value)} />
          <label htmlFor="provider-key">API key {settings?.key_configured ? "(configured)" : "(not configured)"}</label>
          <input id="provider-key" type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="Leave blank to retain saved key" />
          <label className="checkbox-row"><input type="checkbox" checked={allowLocal} onChange={(event) => setAllowLocal(event.target.checked)} />Allow local HTTP provider (unencrypted local connection)</label>
          <div className="setup-actions">
            <button type="submit">Save provider</button>
            <button type="button" onClick={() => act("clear")}>Clear provider</button>
            <button type="button" onClick={() => act("load")}>Reload settings</button>
          </div>
          <p className="form-note">Test uses saved settings, may incur a provider charge, and sends no project code. Save edits first.</p>
          <button type="button" onClick={() => act("test")} disabled={!settings}>Test saved provider (may charge)</button>
        </fieldset>
      </form>
      {busy ? <p role="status">Provider request in progress…</p> : null}
      {notice ? <p role="status">{notice}</p> : null}
      {error ? <p className="error-banner" role="alert">{error}</p> : null}
    </section>
  );
}
