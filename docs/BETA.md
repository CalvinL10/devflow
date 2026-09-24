# DevFlow beta operator notes

## Status and distribution

**Target: v0.2.0-beta.1 — PENDING validation.** This document describes the beta
candidate setup and intended acceptance exercise. It does not announce a published
release, tag, binary, container registry image, or successful hosted validation.
Distribution is source: use the reviewed source checkout/archive and build locally.
Do not run `git checkout v0.2.0-beta.1` unless the maintainers actually publish that tag.
The existing MIT license applies; preserve `LICENSE` when redistributing source.

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
`/project` modes against the commit before import. On the diagnosed Windows host,
both Ubuntu WSL distributions are installed but Docker integration is unavailable:
**the WSL route remains unvalidated on this host**. Distribution installation alone
is not evidence of Docker integration. User Docker settings are not changed
automatically.

Native Windows importer execution can check ordinary `100644` files, but rejects
committed `100755` files because executable-bit verification is unavailable.
The explicit no-import demo remains separate from real-project import support.
The existing Ubuntu CI smoke checks the Linux fixture's mounted modes before
import; it does not establish Windows NTFS support.

## Prerequisites

- Windows with Docker Desktop using Linux containers and PowerShell 5.1+, or Linux
  with Bash and Docker Engine. Use a local Docker context with access to the repository
  and backup directories. Remote daemons and Windows containers are unsupported.
- Git and Docker Compose 2.24.4+ (`!override` is used to remove the demo project bind).
- Free loopback port 3000. Backend port 8000 is not published and need not be free on
  the host. Do not change the published hostname without also reviewing origin security.
- Network access for the first image build, real provider requests, and supported PyPI
  wheel downloads. Offline stop/backup does not imply offline first-time startup.
- A trusted, small Python repository with a real `.git` directory, a committed HEAD,
  and no staged, unstaged or untracked files. Keep the source unchanged during import.
  Startup checks only the repository directory and committed HEAD; the safe import
  preview determines cleanliness before import. No commits are made by startup.

From the DevFlow source root, start real mode:

```powershell
.\scripts\devflow.ps1 start -Repository 'C:\projects\my-python-project'
```

```bash
bash scripts/devflow.sh start --repository /home/me/projects/my-python-project
```

Quote paths containing spaces. On Windows allow Docker Desktop access to the source
and backup drive. Do not run the shell script in Git Bash as a substitute for Linux
validation: MSYS path rewriting can alter Docker mount arguments. Use PowerShell on
Windows or Bash inside a properly configured Linux/WSL environment.

The scripts diagnose Docker/Compose availability, Linux container mode, repository/HEAD
existence and port 3000, then build and wait for startup. They do not determine whether
the source is clean. Source-repository `git status` can execute configured clean filters
even when hooks and fsmonitor are disabled; startup therefore never runs source status
or refreshes its index. Cleanliness is delegated to the safe isolated import preview,
which uses temporary Git state rather than running filters against the source. Failures exit nonzero. They do not clean the
repository, delete volumes, install host packages, or print credential files. If a build
or health check fails, inspect `status` and `logs`; do not disable security checks.
A missing project variable in direct real-mode Compose usage points to a deliberately
missing mount path and fails startup rather than silently importing this checkout.

## Setup, import and approval

Open **http://127.0.0.1:3000**. Keep this exact origin, including the port.

1. Save a Chat Completions-compatible provider's base URL, model name and API key in
   provider setup, then test it. The connection test does not send project code.
   Use a trusted HTTPS endpoint. Local HTTP is an explicit opt-in for local development,
   not a general reason to weaken transport or network-address validation. Container
   loopback refers to the container itself, not the host's loopback server.
2. Refresh project preview; review included/excluded files and import errors. Preview
   is authoritative: startup success does not establish a clean source. Windows automatic
   CRLF conversion or Git filters can make checked-out bytes differ from the commit;
   use a separate byte-identical checkout rather than force-changing an active project.
3. Choose a supported static dependency source or none. Only one source is selected;
   extras refer to selected static `pyproject.toml` optional dependencies. Manifest
   discovery does not mean that every discovered manifest type is supported.
4. Consent to sharing the listed code with that provider and import the snapshot.
   Re-review consent after changing the provider/import. Exclusion rules are not a
   general secret scanner: inspect the source yourself before giving consent.
5. Create a focused task, follow the durable timeline, and inspect the patch, lint/test
   results and review. Model output and passing checks are not proof of correctness.
6. Approve or reject. Approval publishes a managed revision, not the host checkout.
   Download the patch only from an approved imported run. Stop is a separate operation
   for in-progress work; rejection/cancel at an approval boundary are not the same as
   interrupting a running provider request.

Never store keys in `.env`, commit them, pass them as shell arguments, or paste secrets
or raw provider responses into issues. Provider settings use a dedicated credential
volume; local administrators and the Docker daemon still belong to the trust boundary.
Do not expose this service on a LAN or the internet. There is no multi-user authentication,
role separation, tenant isolation, or production high-availability claim.

## Apply an exported patch

Save the downloaded patch **outside** the source repository. Independently inspect it.
Use a clean checkout at the exact source commit shown by the import, not an unrelated
or subsequently modified branch. These commands apply changes only when you run them:

PowerShell (replace paths and `<imported-commit>`):

```powershell
git -C 'C:\projects\my-python-project' rev-parse HEAD
# Confirm HEAD equals the imported commit and safe import preview accepts the unchanged source.
git -C 'C:\projects\my-python-project' switch -c review-devflow-change
git -C 'C:\projects\my-python-project' apply --check 'C:\Downloads\devflow-change.patch'
# Only after --check succeeds:
git -C 'C:\projects\my-python-project' apply 'C:\Downloads\devflow-change.patch'
git -C 'C:\projects\my-python-project' diff --check
git -C 'C:\projects\my-python-project' diff
```

Linux:

```bash
git -C /home/me/projects/my-python-project rev-parse HEAD
# Confirm HEAD equals the imported commit and safe import preview accepts the unchanged source.
git -C /home/me/projects/my-python-project switch -c review-devflow-change
git -C /home/me/projects/my-python-project apply --check /home/me/Downloads/devflow-change.patch
# Only after --check succeeds:
git -C /home/me/projects/my-python-project apply /home/me/Downloads/devflow-change.patch
git -C /home/me/projects/my-python-project diff --check
git -C /home/me/projects/my-python-project diff
```

Choose a different review branch name if it exists. Stop on any failed command: do not
force-apply. Independently inspect all changed and added files as well as the diff, run the
project's own tests, and commit manually only when satisfied. DevFlow neither pushes
changes nor creates a pull request for you.

## Explicit demo

```powershell
.\scripts\devflow.ps1 start -Demo
.\scripts\devflow.ps1 stop -Demo
```

```bash
bash scripts/devflow.sh start --demo
bash scripts/devflow.sh stop --demo
```

The override selects `mock`, clears the container project setting, and replaces the
backend mounts without `/project`. It does not disable local security. No source or key
is required. Demo uses `devflow-demo`, while real mode uses `devflow`; stop one before
starting the other because both publish loopback port 3000. Always use the same mode
flag for status/logs/stop/backup. Mock results are not live-provider validation.

## Storage and container interfaces

- Backend: `DEVFLOW_PROJECT_PATH=/project`, read-only bind from the host's
  `DEVFLOW_PROJECT_PATH`; `DEVFLOW_DATABASE_PATH=/var/lib/devflow/devflow.sqlite`;
  `DEVFLOW_PROVIDER_SETTINGS=/var/lib/devflow-secrets`;
  `DEVFLOW_LLM_PROVIDER=chat_completions`; `DEVFLOW_SECURITY_ENABLED=1`;
  `DEVFLOW_PUBLIC_ORIGIN=http://127.0.0.1:3000`.
- `devflow-data` contains the database, import snapshots and managed workspaces. Treat
  these as one recovery unit. Do not copy only the live SQLite main file and omit its
  WAL, or restore database and workspaces from different points in time.
- `devflow-secrets` contains provider credentials, mounted only in the backend. It is
  not encrypted merely because it is a separate named volume. Protect Docker host access.
- `runner-control` carries the backend/dispatcher Unix socket; it is not a data backup.
- Dispatcher: `DEVFLOW_WORKSPACES_ROOT=/var/lib/devflow/workspaces`, read-only data
  volume and the only host Docker socket mount. It does not receive provider credentials.
  With `cap_drop: ALL`, only `DAC_READ_SEARCH` and `CHOWN` are added to this service.
  `CHOWN` permits startup to assign the fixed control socket to UID/GID 10001 while
  retaining mode 0660 (owner/group only, never 0666). The backend receives neither
  added capability.
- The runner remains derived from the Dockerfile `test` stage, preserving the DevFlow
  virtual environment and tooling. The Python base image also provides system Python
  with pip/venv for the isolated wheel environment. Git is installed for backend import
  and temporary-repository patch export. The backend remains non-root and read-only.

## Offline backup and restore

Back up before replacing a beta installation. From the source distribution:

```powershell
.\scripts\devflow.ps1 stop
.\scripts\devflow.ps1 backup -OutputDirectory 'C:\backups\devflow'
```

```bash
bash scripts/devflow.sh stop
bash scripts/devflow.sh backup --output /home/me/backups/devflow
```

Use a directory outside the imported repository. Add `-Demo`/`--demo` for demo. The
backup command stops all services even if `stop` was not run first, checks for other
running containers using the data volume, and archives its complete contents (including
SQLite sidecars, imports, dependency data and workspaces) as `devflow-data-<UTC>.tar.gz`.
The stack stays stopped on completion or failure. Do not restart it, run another
backup, or let another controller access the volume until the command finishes.
The archive operation is not a global lock against external Docker/host administrators.

The backend container and its local image must still exist: use `stop`, not `down`,
before backup. The helper uses the existing image ID with `--pull=never`, no network,
and only the data volume plus the backup destination. It does not mount the project,
Docker socket, credential volume or control volume. It does not use `--volumes-from`
or inherit the backend environment: the helper runs the backend image with exactly the
read-only data mount and writable backup destination, not the credential-bearing backend
container. No API key is passed to the helper, dispatcher or candidate runner. Docker
itself must remain running.
**Provider secrets are intentionally excluded.** Re-enter credentials after a restore.
Keep archives private: code, prompts and database records can still be sensitive.
The helper writes mode-0600 archives where the filesystem supports it; on Linux these
are root-owned, so privileged access may be needed to inspect or move them. Keep the
Windows destination ACL private as well. A failed archive is not usable; do not assume
a filename alone means success.

Restoration is a manual operator procedure; there is no automated restore command in
this beta. Test it on a separate installation before relying on a backup:

1. Keep all services stopped and retain the original volumes/archive until recovery is
   verified. Use the same source revision and compatible image as the backup.
2. Create a fresh data volume and extract the trusted archive's `data/` contents into
   its root, preserving numeric ownership (backend UID/GID 10001). Do not extract an
   untrusted tar file on your host or overwrite a live volume.
3. Attach that data volume as `devflow-data` for the recovery installation. Start with a
   fresh credential volume and re-enter the provider settings. Never restore only SQLite
   or only workspaces; the entire recovery unit must come from the same archive.
4. Start the recovery installation, inspect history, artifacts and workspace revisions,
   and verify an approved imported run's exported patch before resuming normal work.

Do not use `docker compose down --volumes`, volume prune, or manual volume removal as
routine stop/upgrade commands. No automatic cross-version migration compatibility is
promised for this pending beta.

## Known limitations and pending acceptance

- Single local user, one backend worker, one SQLite database and bounded concurrency;
  not production HA, remote team hosting or an unrestricted autonomous coding agent.
- Import is bounded text-only: ordinary repositories, up to 1,000 paths, 256 KiB per
  file and 16 MiB of text. Links, submodules, unsupported Git layouts, binary/filtered
  or non-byte-identical checkouts can be rejected. Ignore/exclusion handling is explicit.
- Python checks and static public-PyPI wheel dependencies only. No source builds,
  arbitrary install scripts, local/VCS/URL dependency sources, private package indexes,
  general JavaScript/build-system execution or arbitrary user-supplied shell commands.
- Networkless candidate execution can break tests requiring external services. Dependency
  preparation and real provider calls have their own network needs and bounded timeouts.
- Container resource limits and no-network/read-only execution reduce exposure; they do
  not turn hostile code into safe code. The dispatcher controls the Docker daemon.
- A Chat Completions-shaped API is not guaranteed to produce valid structured output;
  unsupported responses, refusals, network failures or invalid patches fail visibly,
  not by silently switching to demo. Live provider quality/cost must be evaluated separately.
- Original checkout updates are manual. No automatic merge, branch push or PR publishing.

**PENDING for v0.2.0-beta.1:** clean-source Windows and Linux startup; real provider
setup/test/import/run/approve/export/apply; explicit no-repository demo; stop during
work/restart recovery; credential isolation; supported/unsupported wheel cases; full
offline backup and restore; ordinary backend/frontend/E2E verification and actual hosted
CI results. Record the exact source revision, platform and outcomes in release notes;
never substitute historical mock evidence for these exercises.

## Model request deadlines and cleanup failures

The connection-test API uses a separate spawned process with a 30-second deadline
(including DNS and worker startup), followed by bounded terminate/kill cleanup.
Task model stages (`plan`, `code`, `review`) additionally have a supervised hard
130-second limit, independent of transport socket timeouts. These are not automatic
retry budgets. A timeout does not imply a remote provider refunded or stopped billing.

If process/container cleanup cannot be confirmed, the task remains active and emits
a persisted `run.cleanup_pending` event. Do not interpret this as canceled or safe to
start another task. Retry stop/cleanup or restart the backend to reconcile managed
resources; the UI continues to show the occupied task. Do not delete database rows
or disable container cleanup to bypass the active-task constraint.
