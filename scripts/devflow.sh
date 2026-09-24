#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
COMMAND="${1:-help}"
if (($#)); then shift; fi
REPOSITORY=''
OUTPUT=''
DEMO=false
PORT=3000
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
while (($#)); do
    case "$1" in
        --repository) (($# >= 2)) || fail '--repository needs a path'; REPOSITORY="$2"; shift 2 ;;
        --output) (($# >= 2)) || fail '--output needs a directory'; OUTPUT="$2"; shift 2 ;;
        --demo) DEMO=true; shift ;;
        --port) (($# >= 2)) || fail '--port needs a number'; PORT="$2"; shift 2 ;;
        *) fail "Unknown option: $1" ;;
    esac
done
case "$COMMAND" in
    help|-h|--help)
        printf '%s\n' 'Usage: bash scripts/devflow.sh start --repository /path/to/clean-repo [--port 3000]' \
            '       bash scripts/devflow.sh start --demo' \
            '       bash scripts/devflow.sh status|logs|stop [--demo]' \
            '       bash scripts/devflow.sh backup --output /path/to/backups [--demo]'
        exit 0 ;;
    start|status|logs|stop|backup) ;;
    *) fail "Unknown command: $COMMAND" ;;
esac
[[ $DEMO == false || -z $REPOSITORY ]] || fail 'Choose --demo OR --repository, not both.'
[[ $COMMAND == start || -z $REPOSITORY ]] || fail '--repository is only used with start.'
[[ $COMMAND == backup || -z $OUTPUT ]] || fail '--output is only used with backup.'
[[ $PORT =~ ^[1-9][0-9]{3,4}$ ]] || fail '--port must be an integer from 1024 to 65535.'
(( PORT >= 1024 && PORT <= 65535 )) || fail '--port must be an integer from 1024 to 65535.'
export DEVFLOW_PORT="$PORT"
command -v docker >/dev/null || fail 'Install Docker with the Compose plugin.'
VERSION="$(docker compose version --short)"
VERSION="${VERSION#v}"; VERSION="${VERSION%%-*}"
IFS=. read -r MAJOR MINOR PATCH <<< "$VERSION"
[[ $MAJOR =~ ^[0-9]+$ && $MINOR =~ ^[0-9]+$ && $PATCH =~ ^[0-9]+$ ]] || fail 'Cannot parse Docker Compose version.'
(( MAJOR > 2 || (MAJOR == 2 && (MINOR > 24 || (MINOR == 24 && PATCH >= 4))) )) || fail 'Docker Compose 2.24.4 or newer is required.'
[[ $(docker info --format '{{.OSType}}') == linux ]] || fail 'Start a local Docker daemon using Linux containers.'
PROJECT=devflow
[[ $DEMO == false ]] || PROJECT=devflow-demo
COMPOSE=(docker compose --project-name "$PROJECT" --project-directory "$ROOT" -f "$ROOT/compose.yaml")
[[ $DEMO == false ]] || COMPOSE+=(-f "$ROOT/compose.demo.yaml")

if [[ $COMMAND == start && $DEMO == false ]]; then
    [[ -n $REPOSITORY ]] || fail 'Real mode requires start --repository <clean committed Git repository>.'
    command -v git >/dev/null || fail 'Install Git and make git available on PATH.'
    REPOSITORY="$(cd -- "$REPOSITORY" && pwd -P)"
    [[ -d $REPOSITORY/.git ]] || fail 'Use a repository root with a .git directory; linked worktrees and bare repositories are unsupported.'
    GIT=(git --no-optional-locks -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$REPOSITORY")
    # Do not inspect source status/index: clean filters can execute even with hooks/fsmonitor disabled.
    # The isolated import preview, not this startup check, decides repository cleanliness.
    "${GIT[@]}" rev-parse --verify 'HEAD^{commit}' >/dev/null 2>&1 || fail 'Repository needs a committed HEAD.'
    export DEVFLOW_PROJECT_PATH="$REPOSITORY"
    printf '%s\n' 'Git diagnostic: repository and committed HEAD found. Cleanliness is NOT checked at startup; the safe import preview must accept the source before import.'
else
    # Lifecycle commands do not require the original checkout to still exist.
    export DEVFLOW_PROJECT_PATH="$ROOT"
fi
"${COMPOSE[@]}" config --quiet
case "$COMMAND" in
    start)
        RUNNING="$("${COMPOSE[@]}" ps --status running --quiet frontend)"
        if [[ -z $RUNNING ]] && (exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then
            fail "Port 127.0.0.1:$PORT is occupied. Choose another --port or stop the other service first."
        fi
        printf '%s\n' "Docker/Compose diagnostics passed. Only frontend 127.0.0.1:$PORT is published; backend 8000 stays internal."
        "${COMPOSE[@]}" up --build --detach --wait --wait-timeout 180
        printf '%s\n' "Open http://127.0.0.1:$PORT (use this exact origin)."
        ;;
    status) "${COMPOSE[@]}" ps --all ;;
    logs) "${COMPOSE[@]}" logs --tail 200 --follow ;;
    stop)
        "${COMPOSE[@]}" stop --timeout 60
        printf '%s\n' 'Stopped; all volumes retained. No network access or image pull is needed.'
        ;;
    backup)
        [[ -n $OUTPUT ]] || fail 'backup requires --output <directory outside the imported repository>.'
        mkdir -p -- "$OUTPUT"
        OUTPUT="$(cd -- "$OUTPUT" && pwd -P)"
        ID="$("${COMPOSE[@]}" ps --all --quiet backend)"
        [[ -n $ID && $ID != *$'\n'* ]] || fail 'Backup requires an existing backend container. Use stop, not down, before backup.'
        VOLUME="$(docker inspect --format '{{range .Mounts}}{{if and (eq .Type "volume") (eq .Destination "/var/lib/devflow")}}{{.Name}}{{end}}{{end}}' "$ID")"
        [[ -n $VOLUME ]] || fail 'Cannot identify the database/workspace volume.'
        IMAGE="$(docker inspect --format '{{.Image}}' "$ID")"
        "${COMPOSE[@]}" stop --timeout 60
        WRITERS="$(docker ps --quiet --filter "volume=$VOLUME")"
        [[ -z $WRITERS ]] || fail 'Another running container uses the data volume. Stop it before retrying backup.'
        printf '%s\n' 'WARNING: Provider secrets are EXCLUDED; re-enter credentials after restore. Archive contains project code, prompts and database records; protect it. Keep all writers stopped until backup completes.' >&2
        CODE="$(cat <<'PY'
import datetime, os, pathlib, tarfile
name = 'devflow-data-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.tar.gz'
path = pathlib.Path('/backup') / name
with path.open('xb') as raw:
    try:
        os.fchmod(raw.fileno(), 0o600)
        with tarfile.open(fileobj=raw, mode='w:gz') as archive:
            archive.add('/data', arcname='data', recursive=True)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
print('Backup complete: ' + name)
PY
)"
        docker run --rm --pull=never --network none --read-only --user 0:0 --cap-drop ALL --cap-add DAC_OVERRIDE \
            --security-opt no-new-privileges:true \
            --mount "type=volume,source=$VOLUME,target=/data,readonly" \
            --mount "type=bind,source=$OUTPUT,target=/backup" \
            --entrypoint python "$IMAGE" -c "$CODE"
        printf '%s\n' 'Stack remains stopped. Database and workspaces were archived together; no credential or control volume was mounted.'
        ;;
esac
