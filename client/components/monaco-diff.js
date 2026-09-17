"use client";

import * as monaco from "monaco-editor";
import { DiffEditor, loader } from "@monaco-editor/react";

// Avoid the default CDN worker path in restricted/offline browser runners.
window.MonacoEnvironment = {
  getWorker() {
    // Monaco catches this and uses its synchronous editor-worker fallback.
    // This keeps diff computation functional without a CDN or a fake worker.
    throw new Error("Monaco worker bundle is not configured");
  },
};
loader.config({ monaco });

export default function MonacoDiff({ file, language, runId }) {
  return (
    <DiffEditor
      height="520px"
      key={`${runId}:${file.path}`}
      language={language}
      modified={file.modified ?? ""}
      original={file.original ?? ""}
      options={{
        automaticLayout: true,
        fontSize: 13,
        minimap: { enabled: false },
        originalEditable: false,
        readOnly: true,
        renderSideBySide: true,
        scrollBeyondLastLine: false,
      }}
      theme="vs-dark"
    />
  );
}
