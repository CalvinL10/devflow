"""Read-only local Git imports with static dependency preview.

Only ordinary repositories with a private .git directory are supported. Linked
worktrees, submodules, sparse/split indexes and non-byte-identical (filtered or
newline-converted) checkouts may be rejected rather than invoking source config.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import tomllib
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from devflow.models import MAX_FILE_BYTES, validate_file_path

MAX_TREE_BYTES = 16 * 1024 * 1024
MAX_FILES = 1000
MAX_GIT_OUTPUT = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 6 * MAX_TREE_BYTES + 2 * MAX_GIT_OUTPUT
# Explicit, case-insensitive exclusions, at any directory depth. Exclusions are
# reported, never silently copied; they cannot be overridden by dependency choice.
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "bower_components",
        "vendor",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".cache",
        ".next",
        ".nuxt",
        ".svelte-kit",
        "dist",
        "build",
        "target",
        "coverage",
        "htmlcov",
        ".idea",
        ".vscode",
        ".aws",
        ".ssh",
        ".gnupg",
        ".terraform",
    }
)
EXCLUDED_NAMES = frozenset(
    {
        ".env",
        ".envrc",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".ds_store",
    }
)
EXCLUDED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".pyc", ".pyo")
MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "pipfile",
        "package.json",
        "cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "gemfile",
        "composer.json",
    }
)
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_EXTRA = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _excluded(path: str) -> bool:
    parts = path.casefold().split("/")
    return (
        any(part in EXCLUDED_DIRS for part in parts[:-1])
        or parts[-1] in EXCLUDED_NAMES
        or parts[-1].startswith(".env.")
        or parts[-1].endswith(EXCLUDED_SUFFIXES)
    )


def _manifest(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].casefold()
    return name in MANIFEST_NAMES or (name.startswith("requirements") and name.endswith(".txt"))


def _safe_path(path: Path) -> None:
    """Reject symbolic links, Windows reparse points, and hard-linked files."""
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
        ):
            raise ValueError(f"links are not supported: {item}")


def _read_file(path: Path, limit: int) -> bytes:
    _safe_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError(f"not a bounded regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(before, opened) or opened.st_nlink != 1:
            raise ValueError("file changed while opening")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    _safe_path(path)
    if (
        len(data) > limit
        or not os.path.samestat(after, path.lstat())
        or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
    ):
        raise ValueError("file changed or exceeded its size limit")
    return data


def _git_env() -> dict[str, str]:
    # Never inherit object/index/worktree overrides, injected config, trace paths,
    # executable overrides, credential helpers, or replacement-object controls.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "0",
            "LC_ALL": "C",
        }
    )
    return env


def _git(
    directory: Path,
    *args: str,
    env: dict[str, str] | None = None,
    data: bytes | None = None,
    limit: int = MAX_GIT_OUTPUT,
) -> bytes:
    """Bound stdout and wall time; never invoke a shell or inherit Git config."""
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.attributesFile=" + os.devnull,
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.safecrlf=false",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
        "-c",
        "protocol.allow=never",
        "-C",
        str(directory),
        *args,
    ]
    # A temporary input stream avoids pipe deadlocks without an unbounded
    # communicate() buffer. Git stdout is consumed with an explicit byte limit.
    with tempfile.TemporaryFile() as source:
        if data is not None:
            source.write(data)
        source.seek(0)
        with subprocess.Popen(
            command,
            env=env or _git_env(),
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            timer = threading.Timer(30, process.kill)
            timer.start()
            try:
                output = process.stdout.read(limit + 1)
                if len(output) > limit:
                    process.kill()
                    raise ValueError("Git output exceeds import/export limits")
                if process.wait() != 0:
                    raise ValueError(f"Git command failed: {args[0]}")
                return output
            finally:
                timer.cancel()


@contextmanager
def _isolated_git(repository: Path | None = None):
    temp_root = Path(tempfile.gettempdir()).resolve()
    if repository is not None and temp_root.is_relative_to(repository):
        raise ValueError("temporary storage must be outside the original repository")
    with tempfile.TemporaryDirectory(prefix="devflow-git-", dir=temp_root) as name:
        root = Path(name)
        _git(root, "init", "--quiet", "--template=", "--object-format=sha1")
        # Highest precedence attributes prevent conversion even when the source
        # contains malicious .gitattributes. No source filter configuration is read.
        (root / ".git" / "info").mkdir(exist_ok=True)
        (root / ".git" / "info" / "attributes").write_bytes(
            b"* -text -filter -ident -working-tree-encoding\n"
        )
        yield root


def _read_head(repository: Path) -> str:
    """Read bounded refs as data, never trust or execute source Git configuration.

    Bind mounts commonly have another owner. Resolve only ordinary loose/packed
    refs rather than disabling Git ownership checks for the mounted repository.
    """
    git_dir = repository / ".git"
    value = _read_file(git_dir / "HEAD", 4096).decode("ascii").strip()
    if value.startswith("ref: "):
        ref = value[5:]
        parts = ref.split("/")
        if (not ref.startswith("refs/") or len(parts) < 3
                or any(not re.fullmatch(r"[A-Za-z0-9._-]+", part)
                       or part in {".", ".."} or ".." in part
                       or part.endswith(".lock") for part in parts)):
            raise ValueError("unsupported HEAD reference")
        loose = git_dir.joinpath(*parts)
        if loose.exists():
            value = _read_file(loose, 4096).decode("ascii").strip()
        else:
            packed = git_dir / "packed-refs"
            value = ""
            if packed.exists():
                for line in _read_file(packed, MAX_GIT_OUTPUT).decode("ascii").splitlines():
                    if line.startswith(("#", "^")) or not line:
                        continue
                    oid, name = line.split(" ", 1)
                    if name == ref:
                        if value:
                            raise ValueError("duplicate packed HEAD reference")
                        value = oid
    if not _COMMIT.fullmatch(value):
        raise ValueError("HEAD is not a supported direct commit reference")
    return value


def _validate_paths(paths) -> None:
    if len(paths) > MAX_FILES:
        raise ValueError("tree exceeds 1000 files")
    folded = set()
    for path in paths:
        if not isinstance(path, str):
            raise TypeError("file paths must be strings")
        validate_file_path(path)
        key = path.casefold()
        if key in folded:
            raise ValueError("case-aliased file paths are unsupported")
        folded.add(key)
    for path in folded:
        parts = path.split("/")
        if any("/".join(parts[:i]) in folded for i in range(1, len(parts))):
            raise ValueError("file/directory path aliases are unsupported")


def _validate_texts(files: dict[str, str]) -> None:
    _validate_paths(files)
    total = 0
    for text in files.values():
        if not isinstance(text, str) or "\x00" in text:
            raise ValueError("binary or non-text files are unsupported")
        size = len(text.encode("utf-8"))
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TREE_BYTES:
            raise ValueError("text tree exceeds 256 KiB/file or 16 MiB/tree")


class ProjectImporter:
    def __init__(self, repository: Path, imports_root: Path):
        self.repository = Path(os.path.abspath(repository))
        self.imports_root = Path(os.path.abspath(imports_root))

    def preview(self) -> dict:
        """Return commit, included/excluded paths, errors and manifest candidates."""
        return self._inspect()[0]

    def _inspect(self) -> tuple[dict, dict[str, str], dict[str, str]]:
        result = {
            "commit": None,
            "files": [],
            "excluded": [],
            "errors": [],
            "dependency_sources": [],
        }
        files, modes = {}, {}
        try:
            repo = self.repository
            _safe_path(repo)
            _safe_path(repo / ".git")
            if not repo.is_dir() or not (repo / ".git").is_dir():
                raise ValueError("expected a local repository root with a .git directory")
            # Check before even the first Git invocation: its bounded stdin
            # spool must not create temporary files in the original checkout.
            if Path(tempfile.gettempdir()).resolve().is_relative_to(repo):
                raise ValueError("temporary storage must be outside the original repository")
            if (repo / ".git" / "objects" / "info" / "alternates").exists():
                raise ValueError("alternate object stores are unsupported")
            _safe_path(repo / ".git" / "objects")
            commit = _read_head(repo)
            if not _COMMIT.fullmatch(commit):
                raise ValueError("HEAD is not a supported commit")
            result["commit"] = commit
            with _isolated_git(repo) as isolated:
                env = _git_env()
                env["GIT_OBJECT_DIRECTORY"] = str(repo / ".git" / "objects")
                if len(commit) == 64:
                    _git(isolated, "config", "core.repositoryformatversion", "1")
                    _git(isolated, "config", "extensions.objectformat", "sha256")
                if _git(isolated, "cat-file", "-t", commit, env=env).strip() != b"commit":
                    raise ValueError("HEAD must identify a commit")
                listing = _git(isolated, "ls-tree", "-rz", "--full-tree", "-l", commit, env=env)
                entries = []
                for row in listing.split(b"\0"):
                    if not row:
                        continue
                    header, raw_path = row.split(b"\t", 1)
                    path = raw_path.decode("utf-8")
                    mode, kind, oid, size = header.split()
                    entries.append((path, mode.decode(), kind, oid.decode(), size))
                _validate_paths([entry[0] for entry in entries])
                total = 0
                for path, mode, kind, oid, size in entries:
                    if kind != b"blob" or mode not in {"100644", "100755"}:
                        result["errors"].append(f"{path}: links/submodules or unsupported mode")
                        continue
                    _safe_path(repo / path)
                    if _excluded(path):
                        result["excluded"].append(path)
                        continue
                    total += int(size)
                    if total > MAX_TREE_BYTES:
                        raise ValueError("tree exceeds 16 MiB")
                    try:
                        if int(size) > MAX_FILE_BYTES:
                            raise ValueError("file exceeds 256 KiB")
                        data = _git(
                            isolated, "cat-file", "blob", oid, env=env, limit=MAX_FILE_BYTES
                        )
                        text = data.decode("utf-8")
                        if "\x00" in text:
                            raise ValueError("binary files are unsupported")
                        files[path] = text
                        modes[path] = mode
                        if _read_file(repo / path, MAX_FILE_BYTES) != data:
                            raise ValueError("working tree differs from committed bytes")
                    except (ValueError, OSError) as error:
                        result["errors"].append(f"{path}: {error}")
                _validate_texts(files)
                # Inspect a COPY of the index to detect staged changes, including
                # intent-to-add, conflicts, and staged deletions of empty files.
                index = None
                if (repo / ".git" / "index").exists():
                    index = _read_file(repo / ".git" / "index", MAX_GIT_OUTPUT)
                    (isolated / ".git" / "index").write_bytes(index)
                staged = _git(isolated, "ls-files", "--stage", "-z", env=env)
                expected = b"".join(
                    f"{mode} {oid} 0\t{path}".encode() + b"\0" for path, mode, _, oid, _ in entries
                )
                if sorted(staged.split(b"\0")) != sorted(expected.split(b"\0")):
                    result["errors"].append("repository has staged changes or an unsupported index")
                # Do not ask Git to inspect a bind-mounted work tree: ownership
                # checks are correct there and safe.directory must not be weakened.
                # Tracked bytes were already compared above. Enumerate extra files
                # ourselves with the same symlink/path checks and exclusions.
                tracked = {entries_path for entries_path, *_ in entries}
                for item in repo.rglob("*"):
                    if not item.is_file() or ".git" in item.relative_to(repo).parts:
                        continue
                    path = item.relative_to(repo).as_posix()
                    if path in tracked or _excluded(path):
                        continue
                    validate_file_path(path)
                    _safe_path(item)
                    result["errors"].append(f"{path}: untracked file")
                current_index = (
                    _read_file(repo / ".git" / "index", MAX_GIT_OUTPUT)
                    if (repo / ".git" / "index").exists()
                    else None
                )
                if current_index != index:
                    result["errors"].append("index changed during preview")
                if _read_head(repo) != commit:
                    result["errors"].append("HEAD changed during preview")
        except (ValueError, TypeError, OSError) as error:
            result["errors"].append(str(error))
        result["files"] = sorted(files)
        result["excluded"] = sorted(result["excluded"])
        from devflow.dependencies import parse_dependencies
        manifests = [path for path in result["files"] if _manifest(path)]
        result["dependency_sources"] = [path for path in manifests
            if Path(path).name == "pyproject.toml" or (
                Path(path).name.startswith("requirements") and path.endswith(".txt"))]
        result["warnings"] = [f"{path}: installation/build system is not supported or executed"
                              for path in manifests if path not in result["dependency_sources"]]
        result["dependency_details"] = []
        for source in result["dependency_sources"]:
            detail = {"source": source, "requirements": [], "extras": [], "errors": []}
            try:
                detail["requirements"] = parse_dependencies(files, source)
                if Path(source).name == "pyproject.toml":
                    document = tomllib.loads(files[source])
                    detail["extras"] = sorted(document.get("project", {}).get("optional-dependencies", {}))
            except (ValueError, TypeError):
                detail["errors"] = ["Unsupported or invalid static dependencies; use named public PyPI wheel requirements."]
            result["dependency_details"].append(detail)
        return result, files, modes

    @staticmethod
    def _extras(extras: list[str] | None) -> list[str]:
        if extras is None:
            return []
        if (
            not isinstance(extras, list)
            or len(extras) > 32
            or any(not isinstance(x, str) or not _EXTRA.fullmatch(x) for x in extras)
            or len(set(extras)) != len(extras)
        ):
            raise ValueError("extras must be at most 32 unique bounded extra names")
        return list(extras)

    def _storage(self) -> Path:
        _safe_path(self.imports_root)
        if self.imports_root.resolve().is_relative_to(self.repository.resolve()):
            raise ValueError("imports_root must be outside the original repository")
        return self.imports_root

    def create(
        self,
        expected_commit: str,
        dependency_source: str | None = None,
        extras: list[str] | None = None,
    ) -> dict:
        """Recheck a clean commit, then atomically persist an independent snapshot.

        Failures raise ValueError (invalid input/unsupported or dirty repository)
        or OSError (storage failure). Only static dependency parsing occurs; no installation is executed.
        """
        if not isinstance(expected_commit, str) or not _COMMIT.fullmatch(expected_commit):
            raise ValueError("expected_commit must be a full Git commit ID")
        extras = self._extras(extras)
        root = self._storage()
        preview, files, modes = self._inspect()
        if preview["errors"]:
            raise ValueError("; ".join(preview["errors"]))
        if preview["commit"] != expected_commit:
            raise ValueError("HEAD changed since preview")
        if dependency_source is not None and dependency_source not in preview["dependency_sources"]:
            raise ValueError("dependency_source must be an included manifest path")
        from devflow.dependencies import parse_dependencies
        try:
            parse_dependencies(files, dependency_source, extras)
        except (ValueError, TypeError):
            raise ValueError("Selected dependencies/extras are unsupported; use static public PyPI wheel requirements.") from None
        import_id = str(uuid4())
        metadata = {
            "import_id": import_id,
            "commit": expected_commit,
            "files": preview["files"],
            "excluded": preview["excluded"],
            "dependency_source": dependency_source,
            "extras": extras,
            "modes": modes,
        }
        data = json.dumps({"metadata": metadata, "contents": files}, ensure_ascii=True).encode(
            "utf-8"
        )
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot exceeds storage limits")
        root.mkdir(parents=True, exist_ok=True)
        _safe_path(root)
        temporary = root / ("." + import_id + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
            _safe_path(root)
            os.replace(temporary, root / (import_id + ".json"))
        finally:
            if temporary.exists():
                temporary.unlink()
        return metadata

    def _snapshot(self, import_id: str) -> dict:
        try:
            if not isinstance(import_id, str) or str(UUID(import_id)) != import_id:
                raise ValueError("noncanonical UUID")
        except (ValueError, AttributeError) as error:
            raise ValueError("import_id must be a canonical UUID") from error
        path = self._storage() / (import_id + ".json")
        try:
            snapshot = json.loads(_read_file(path, MAX_SNAPSHOT_BYTES))
            metadata, contents = snapshot["metadata"], snapshot["contents"]
            if not isinstance(contents, dict) or not isinstance(metadata, dict):
                raise TypeError("invalid snapshot objects")
            _validate_texts(contents)
            if (
                metadata["import_id"] != import_id
                or not isinstance(metadata["commit"], str)
                or not _COMMIT.fullmatch(metadata["commit"])
                or metadata["files"] != sorted(contents)
                or not isinstance(metadata["modes"], dict)
                or set(metadata["modes"]) != set(contents)
                or any(mode not in {"100644", "100755"} for mode in metadata["modes"].values())
            ):
                raise ValueError("invalid snapshot metadata")
            self._extras(metadata["extras"])
            if not isinstance(metadata["excluded"], list):
                raise TypeError("invalid excluded paths")
            _validate_paths(metadata["excluded"])
            if any(not _excluded(p) or p in contents for p in metadata["excluded"]):
                raise ValueError("invalid excluded paths")
            if any(_excluded(p) for p in contents):
                raise ValueError("snapshot contains excluded files")
            dependency = metadata["dependency_source"]
            if dependency is not None and (dependency not in contents or not _manifest(dependency)):
                raise ValueError("invalid dependency source")
            return snapshot
        except (KeyError, TypeError, UnicodeError, RecursionError) as error:
            raise ValueError("invalid import snapshot") from error

    def load(self, import_id: str) -> dict:
        """Load validated metadata, including the committed modes map."""
        return self._snapshot(import_id)["metadata"]

    def files(self, import_id: str) -> dict[str, str]:
        """Load a validated path-to-text snapshot without consulting the repository."""
        return self._snapshot(import_id)["contents"]

