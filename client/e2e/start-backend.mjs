import { existsSync, mkdirSync, rmSync } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";

const clientRoot = path.resolve(process.cwd());
const runtime = path.resolve(clientRoot, ".e2e-runtime");
const expectedPrefix = `${clientRoot}${path.sep}`;
if (!runtime.startsWith(expectedPrefix) || path.basename(runtime) !== ".e2e-runtime") {
  throw new Error(`Refusing to clean unexpected E2E runtime path: ${runtime}`);
}
rmSync(runtime, { recursive: true, force: true });
mkdirSync(runtime, { recursive: true });

const repositoryRoot = path.dirname(clientRoot);
const candidates = process.platform === "win32"
  ? [path.join(repositoryRoot, "backend", ".venv", "Scripts", "python.exe")]
  : [path.join(repositoryRoot, "backend", ".venv", "bin", "python"), "python3"];
const python = candidates.find((candidate) => candidate === "python3" || existsSync(candidate));
if (!python) throw new Error("Backend virtualenv Python was not found.");

const child = spawn(
  python,
  ["-m", "uvicorn", "backend_app:app", "--app-dir", "e2e", "--host", "127.0.0.1", "--port", "8000", "--timeout-graceful-shutdown", "1"],
  {
    cwd: clientRoot,
    env: { ...process.env, E2E_RUNTIME_DIR: runtime },
    stdio: "inherit",
  },
);

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 1);
});
