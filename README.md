# DevFlow

DevFlow is a local-first, human-in-the-loop code-change workflow: import a clean Git
snapshot, ask a Chat Completions-compatible provider for a change, inspect isolated
Python checks and review, then approve and export a patch. **Your original checkout
is mounted read-only; approval does not apply changes to it.**

**v0.2.0-beta.1 — source-distributed local beta.** No desktop installer or prebuilt
registry image is provided. See [Beta setup and limitations](docs/BETA.md),
[validation record](docs/GITHUB_RELEASE.md), and the
[GitHub Prerelease](https://github.com/CalvinL10/devflow/releases/tag/v0.2.0-beta.1).

**Stack:** Python 3.11 · FastAPI · LangGraph · SQLite · Next.js · React · Docker

## Windows NTFS import limitation

**Direct Windows drive binds with synthetic executable bits are unsupported for
real-project imports.** The tested `F:` mount exposes all five committed `100644`
fixture files as `0777` inside `/project`, including `fixture.py` and
`test_fixture.py`. The patched importer correctly rejects these mismatches.
Successful startup or an earlier workflow run does not establish compatibility.
Do not disable mode validation, trust `core.fileMode=false`, or chmod the user's
source to force acceptance. Commit modes are preserved for export, not used to
conceal dirty worktree modes.

Use a fresh, mode-preserving Linux checkout, or WSL-native Linux storage such as
`/home/<user>/projects` (not `/mnt/c` or `/mnt/f`). Launch Compose from that Linux
environment only when Docker is already available there, and verify actual
`/project` modes against the commit before import. The WSL-native route has been validated with Ubuntu and Docker Desktop integration:
Git modes survived the read-only bind and the imported source remained unchanged.
Enable integration for your chosen distro in Docker Desktop before starting.
Linux filesystem semantics do not require the C: drive: the WSL virtual disk and Docker
storage may live on D: or F:. A Linux path inside that disk is different from an NTFS
bind at `/mnt/f`. Startup scripts do not relocate disks or change Docker settings.

Native Windows importer execution can check ordinary `100644` files, but rejects
committed `100755` files because executable-bit verification is unavailable.
The explicit no-import demo remains separate from real-project import support.
The existing Ubuntu CI smoke checks the Linux fixture's mounted modes before
import; it does not establish Windows NTFS support.

## Start real mode

Install Git and Docker with Linux containers and Docker Compose **2.24.4 or newer**.
Use a local Docker daemon, not a remote context. Port 127.0.0.1:3000 must be free, or select another local port.
Run from the DevFlow source directory; no host Python or Node installation is needed
for the Compose path. Initial builds and provider/dependency operations need networking.

The target must be an ordinary Git repository root with a committed HEAD and a clean
working tree (including untracked files). Use a small Python project for this beta;
linked worktrees, submodules, binary files and large repositories are unsupported.
Startup checks only the repository directory and committed HEAD, not working-tree
cleanliness. It deliberately does not run source-repository `git status` or refresh its
index: clean filters can execute even with hooks and fsmonitor disabled. The safe,
isolated import preview decides cleanliness and must accept the source before import.

**Windows PowerShell startup syntax** (Docker Desktop in Linux-container mode;
the NTFS import limitation above still applies):

```powershell
.\scripts\devflow.ps1 start -Repository 'C:\projects\my-python-project'
.\scripts\devflow.ps1 status
```

**Linux Bash:**

```bash
bash scripts/devflow.sh start --repository /home/me/projects/my-python-project
bash scripts/devflow.sh status
```

If port 3000 is occupied, append `-Port 3001` in PowerShell or `--port 3001`
in Bash. The launcher updates both the loopback listener and the allowed origin;
open `http://127.0.0.1:3001` instead. Use the same port flag for lifecycle commands.
For direct Compose usage, set `DEVFLOW_PORT=3001`. No backend port is published.

Open **http://127.0.0.1:3000**, not a LAN address or an alternate hostname.

1. In provider setup, enter the provider base URL, model and API key. Save and test
   the connection. Real mode never silently falls back to mock mode.
2. Refresh the project preview. Inspect included/excluded paths and resolve errors.
   Choose one supported dependency source (or none) and optional extras.
3. Give explicit consent to share the listed source with the configured provider,
   then import. Do not submit secrets or proprietary code without authorization.
4. Submit a task, inspect its timeline, patch, checks and review, then approve or
   reject. An approved imported run can export a patch; independently review it
   and follow the [manual apply procedure](docs/BETA.md#apply-an-exported-patch).

Credentials are stored separately from the database/workspaces, not in a browser
storage setting or the source repository. Do not put API keys in `.env`, terminal
arguments, screenshots or issue reports. Provider usage may incur charges.

## Explicit deterministic demo

Demo is a separate Compose override, not the default and not evidence of real model
quality. It needs neither a source repository mount nor provider credentials.

```powershell
.\scripts\devflow.ps1 start -Demo
.\scripts\devflow.ps1 status -Demo
.\scripts\devflow.ps1 stop -Demo
```

```bash
bash scripts/devflow.sh start --demo
bash scripts/devflow.sh status --demo
bash scripts/devflow.sh stop --demo
```

The scripts use separate projects (`devflow` and `devflow-demo`) so demo state does not
mix with real state. Both use port 3000: stop one before starting the other. Demo runs
without an imported project cannot export an approved imported-project patch.

For direct Compose usage, set `DEVFLOW_PROJECT_PATH` to an absolute repository path
(or copy `.env.example` to `.env` and edit the path), then run:

```bash
docker compose -p devflow -f compose.yaml config --quiet
docker compose -p devflow -f compose.yaml up --build --detach --wait
```

Explicit demo, on either shell, with no `.env` or project path required:

```bash
docker compose -p devflow-demo -f compose.yaml -f compose.demo.yaml up --build --detach --wait
```

## Status, logs, stop and offline backup

```powershell
.\scripts\devflow.ps1 status
.\scripts\devflow.ps1 logs
.\scripts\devflow.ps1 stop
.\scripts\devflow.ps1 backup -OutputDirectory 'C:\backups\devflow'
```

```bash
bash scripts/devflow.sh status
bash scripts/devflow.sh logs
bash scripts/devflow.sh stop
bash scripts/devflow.sh backup --output /home/me/backups/devflow
```

Add `-Demo` / `--demo` for the demo stack. Ctrl+C exits log following.
Stop retains containers and volumes; it never deletes user data. Backup also stops the
stack and leaves it stopped. It archives the entire database/workspace volume in one
offline operation using the already-built backend image, without pulling an image or
contacting a provider. **Secrets are excluded**; re-enter credentials after restoration.
Backups still contain source, prompts and run records and must be protected. Keep all
other writers stopped during the backup. See [backup and restore](docs/BETA.md#offline-backup-and-restore).

## Runtime boundaries

- Only the frontend publishes a loopback port. The backend health check stays internal.
- The backend reads `/project` from the configured read-only host bind. It stores SQLite,
  imports and managed workspaces under `/var/lib/devflow` in `devflow-data`.
- Provider settings live at `/var/lib/devflow-secrets` in the dedicated `devflow-secrets`
  volume. That volume is not mounted in the dispatcher or candidate containers.
- Only the trusted dispatcher mounts the Docker socket. It reads managed workspaces at
  `/var/lib/devflow/workspaces` and runs constrained candidate checks. The runner image
  retains DevFlow's test-environment tooling; project dependencies use isolated wheel-only
  environments, not arbitrary package build scripts.
- The frontend proxies same-origin API/SSE traffic. Local-origin security is enabled;
  this is not a public, multi-user hosting setup. Do not publish backend port 8000 or
  disable origin/security checks to work around setup problems.
- One coordinator process and one SQLite database are supported. Docker isolation is
  defense in depth, not a guarantee against hostile code or a kernel escape.

## Development and evidence

See [CONTRIBUTING.md](CONTRIBUTING.md) for contributor commands and
[SECURITY.md](SECURITY.md) for security reporting. Historical portfolio material is
background, not beta validation evidence. Ordinary tests can be run from source:

```bash
cd backend
uv sync --locked --python 3.11
uv run --locked pytest -q
```

In a separate terminal, from the source root:

```bash
cd client
npm ci
npm run lint
npm test
npm run build
npm run test:e2e
```

Passing mock/unit tests does not establish live provider compatibility, end-to-end
Docker behavior, Windows/Linux startup, or backup recovery. Those beta checks remain
**PENDING** until the actual revision and results are recorded in the release notes.

## License

[MIT](LICENSE). The existing license and copyright notice are unchanged.
