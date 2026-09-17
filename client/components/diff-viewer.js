"use client";

import dynamic from "next/dynamic";
import { Component } from "react";

const MonacoDiff = dynamic(
  () => import("./monaco-diff"),
  { ssr: false, loading: () => <div className="diff-loading">Loading Monaco Diff…</div> },
);

class MonacoErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return <div className="diff-loading" role="alert">Monaco failed to load. Use the persisted patch text below.</div>;
    }
    return this.props.children;
  }
}

function languageFor(path) {
  const extension = path.split(".").pop()?.toLowerCase();
  return {
    js: "javascript",
    jsx: "javascript",
    mjs: "javascript",
    ts: "typescript",
    tsx: "typescript",
    py: "python",
    json: "json",
    md: "markdown",
    css: "css",
    html: "html",
    yml: "yaml",
    yaml: "yaml",
    toml: "ini",
  }[extension] || "plaintext";
}

export default function DiffViewer({ file, runId }) {
  if (!file) {
    return <div className="empty-panel">No changed file is available for this run.</div>;
  }
  const kind = file.original == null ? "ADDED" : file.modified == null ? "DELETED" : "MODIFIED";
  const language = languageFor(file.path);
  return (
    <section className="diff-panel" data-testid="diff-editor">
      <header className="diff-header">
        <div>
          <span className={`file-kind ${kind.toLowerCase()}`}>{kind}</span>
          <strong>{file.path}</strong>
        </div>
        <span>Patch is read-only</span>
      </header>
      <div className="monaco-shell">
        <MonacoErrorBoundary key={`${runId}:${file.path}`}>
          <MonacoDiff
            file={file}
            language={language}
            runId={runId}
          />
        </MonacoErrorBoundary>
      </div>
      <details className="diff-source-proof">
        <summary>Persisted patch text</summary>
        <div className="source-columns">
          <pre data-testid="diff-original">{file.original ?? ""}</pre>
          <pre data-testid="diff-modified">{file.modified ?? ""}</pre>
        </div>
      </details>
    </section>
  );
}
