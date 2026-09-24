import json
import os
import subprocess

import pytest

from devflow.models import MAX_FILE_BYTES
from devflow.project_import import ProjectImporter, _git_env


def git(repo, *args, data=None):
    # Fixture commits must finish without detached maintenance changing .git later.
    # Keep the full source-state assertion; do not ignore transient files.
    return subprocess.run(
        ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-C", str(repo), *args], input=data, check=True, capture_output=True, env=_git_env()
    ).stdout


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet", "--template=")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "core.filemode", "false")
    (repo / "hello.py").write_bytes(b"print('hi')\r\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'example'\n[project.optional-dependencies]\ntest = []\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def importer(repo, tmp_path):
    return ProjectImporter(repo, tmp_path / "imports")


def commit(repo):
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "update")


def source_state(repo):
    return {
        str(p.relative_to(repo)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in repo.rglob("*")
        if p.is_file()
    }


def test_roundtrip_and_no_source_writes(repository, tmp_path):
    instance = importer(repository, tmp_path)
    before = source_state(repository)
    preview = instance.preview()
    assert preview["errors"] == []
    assert preview["files"] == ["hello.py", "pyproject.toml"]
    assert preview["dependency_sources"] == ["pyproject.toml"]
    metadata = instance.create(preview["commit"], "pyproject.toml", ["test"])
    assert instance.load(metadata["import_id"]) == metadata
    assert instance.files(metadata["import_id"])["hello.py"].endswith("\r\n")
    assert metadata["modes"]["hello.py"] == "100644"
    assert source_state(repository) == before
    (repository / "hello.py").write_text("changed")
    assert instance.files(metadata["import_id"])["hello.py"] == "print('hi')\r\n"


@pytest.mark.parametrize("change", ["working", "staged", "untracked", "deleted", "assume", "skip"])
def test_dirty_repositories_rejected(repository, tmp_path, change):
    if change == "untracked":
        (repository / "new.txt").write_text("new")
    elif change == "deleted":
        (repository / "hello.py").unlink()
    else:
        (repository / "hello.py").write_text("changed")
        if change == "staged":
            git(repository, "add", ".")
        if change in {"assume", "skip"}:
            git(
                repository,
                "update-index",
                "--assume-unchanged" if change == "assume" else "--skip-worktree",
                "hello.py",
            )
    instance = importer(repository, tmp_path)
    before = source_state(repository)
    assert instance.preview()["errors"]
    with pytest.raises(ValueError):
        instance.create(git(repository, "rev-parse", "HEAD").decode().strip())
    assert source_state(repository) == before


def test_exclusions_and_dependency_choice(repository, tmp_path):
    for name in [
        ".env",
        ".env.local",
        ".npmrc",
        "secret.pem",
        "node_modules/pkg/a.js",
        "dist/app.js",
    ]:
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("secret or generated")
    commit(repository)
    instance = importer(repository, tmp_path)
    preview = instance.preview()
    assert preview["errors"] == []
    assert len(preview["excluded"]) == 6
    for kwargs in [{"dependency_source": ".env"}, {"extras": ["../bad"]}]:
        with pytest.raises(ValueError):
            instance.create(preview["commit"], **kwargs)
    metadata = instance.create(preview["commit"])
    assert instance.files(metadata["import_id"]).keys() == {"hello.py", "pyproject.toml"}


@pytest.mark.parametrize(
    "data",
    [b"a\0b", b"\xff", b"a" * (MAX_FILE_BYTES + 1)],
    ids=["nul", "invalid-utf8", "oversized"],
)
def test_unsupported_contents_preview_error(repository, tmp_path, data):
    (repository / "bad.dat").write_bytes(data)
    commit(repository)
    assert importer(repository, tmp_path).preview()["errors"]


@pytest.mark.parametrize(
    "mode,path",
    [("120000", "link"), ("100644", "../escape"), ("100644", "CON"), ("100644", "bad\\path")],
)
def test_unsafe_commit_entries(repository, tmp_path, mode, path):
    oid = git(repository, "hash-object", "-w", "--stdin", data=b"target").strip()
    # mktree permits malicious paths that update-index or the filesystem refuses.
    record = mode.encode() + b" blob " + oid + b"\t" + path.encode() + b"\0"
    try:
        tree = git(repository, "mktree", "-z", data=record).strip()
    except subprocess.CalledProcessError:
        pytest.skip("Git refuses this malformed tree before import")
    head = git(repository, "commit-tree", tree.decode(), "-m", "unsafe").strip()
    git(repository, "update-ref", "HEAD", head.decode())
    assert importer(repository, tmp_path).preview()["errors"]


def test_stale_commit_and_storage_boundary(repository, tmp_path):
    instance = importer(repository, tmp_path)
    old = instance.preview()["commit"]
    (repository / "hello.py").write_text("new")
    commit(repository)
    with pytest.raises(ValueError, match="HEAD changed"):
        instance.create(old)
    with pytest.raises(ValueError, match="outside"):
        ProjectImporter(repository, repository / "imports").create(old)
    assert not (repository / "imports").exists()


@pytest.mark.parametrize(
    "bad_id", ["../escape", "", "a" * 36, "{00000000-0000-0000-0000-000000000000}"]
)
def test_bad_ids(repository, tmp_path, bad_id):
    with pytest.raises(ValueError):
        importer(repository, tmp_path).load(bad_id)


def test_snapshot_tampering(repository, tmp_path):
    instance = importer(repository, tmp_path)
    metadata = instance.create(instance.preview()["commit"])
    path = instance.imports_root / (metadata["import_id"] + ".json")
    saved = json.loads(path.read_bytes())
    saved["contents"]["../escape"] = "bad"
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError):
        instance.files(metadata["import_id"])


def test_no_filters_hooks_or_inherited_git_overrides(repository, tmp_path, monkeypatch):
    marker = tmp_path / "executed"
    (repository / ".gitattributes").write_text("*.py filter=evil\n")
    commit(repository)
    command = f"echo executed > '{marker.as_posix()}'"
    git(repository, "config", "filter.evil.clean", command)
    git(repository, "config", "filter.evil.smudge", command)
    git(repository, "config", "core.fsmonitor", command)
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "wrong-index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", command)
    before = source_state(repository)
    instance = importer(repository, tmp_path)
    preview = instance.preview()
    assert preview["errors"] == []
    instance.create(preview["commit"])
    assert not marker.exists()
    assert not (tmp_path / "wrong-index").exists()
    assert source_state(repository) == before


def test_empty_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet", "--template=")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "empty",
    )
    instance = importer(repo, tmp_path)
    assert instance.preview()["errors"] == []
    assert instance.create(instance.preview()["commit"])["files"] == []


def test_file_count_limit(repository, tmp_path):
    for number in range(999):
        (repository / f"file-{number:04d}").write_bytes(b"")
    commit(repository)
    preview = importer(repository, tmp_path).preview()
    assert any("1000" in error for error in preview["errors"])


def test_tree_size_limit(repository, tmp_path):
    for number in range(65):
        (repository / f"file-{number:04d}").write_bytes(b"x" * MAX_FILE_BYTES)
    commit(repository)
    preview = importer(repository, tmp_path).preview()
    assert any("16 MiB" in error for error in preview["errors"])


def test_exact_file_limit_and_executable_mode(repository, tmp_path):
    path = repository / "hello.py"
    path.write_bytes(b"x" * MAX_FILE_BYTES)
    path.chmod(0o755)
    git(repository, "add", ".")
    git(repository, "update-index", "--chmod=+x", "hello.py")
    git(repository, "commit", "--quiet", "-m", "executable")
    instance = importer(repository, tmp_path)
    preview = instance.preview()
    if os.name == "nt":
        assert any("mode verification is unsupported" in error for error in preview["errors"])
        return
    assert preview["errors"] == []
    metadata = instance.create(preview["commit"])
    assert metadata["modes"]["hello.py"] == "100755"
    assert len(instance.files(metadata["import_id"])["hello.py"]) == MAX_FILE_BYTES


def test_hardlinked_snapshot_rejected(repository, tmp_path):
    instance = importer(repository, tmp_path)
    metadata = instance.create(instance.preview()["commit"])
    path = instance.imports_root / (metadata["import_id"] + ".json")
    (tmp_path / "hardlink").hardlink_to(path)
    with pytest.raises(ValueError, match="links"):
        instance.load(metadata["import_id"])


def test_index_stat_cache_not_refreshed(repository, tmp_path):
    # Same bytes with different file stat metadata is a clean checkout, but
    # would ordinarily trigger a status/index refresh in the original repo.
    path = repository / "hello.py"
    path.write_bytes(path.read_bytes())
    before = source_state(repository)
    instance = importer(repository, tmp_path)
    preview = instance.preview()
    assert preview["errors"] == []
    instance.create(preview["commit"])
    assert source_state(repository) == before


def test_no_commit_is_preview_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet", "--template=")
    assert importer(repo, tmp_path).preview()["errors"]


def test_invalid_snapshot_mode(repository, tmp_path):
    instance = importer(repository, tmp_path)
    metadata = instance.create(instance.preview()["commit"])
    path = instance.imports_root / (metadata["import_id"] + ".json")
    saved = json.loads(path.read_bytes())
    saved["metadata"]["modes"]["hello.py"] = "120000"
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError):
        instance.load(metadata["import_id"])


def test_temp_storage_cannot_touch_original(repository, tmp_path, monkeypatch):
    import tempfile

    before = source_state(repository)
    monkeypatch.setattr(tempfile, "tempdir", str(repository))
    preview = importer(repository, tmp_path).preview()
    assert any("temporary storage" in error for error in preview["errors"])
    assert source_state(repository) == before


def test_symlinked_storage_rejected(repository, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this host")
    instance = ProjectImporter(repository, linked)
    with pytest.raises(ValueError, match="links"):
        instance.create(instance.preview()["commit"])
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("head_mode", ["loose", "packed", "detached"])
def test_source_git_config_never_loaded(repository, tmp_path, monkeypatch, head_mode):
    import devflow.project_import as module
    expected = git(repository, "rev-parse", "HEAD").decode().strip()
    if head_mode == "packed":
        git(repository, "pack-refs", "--all", "--prune")
    elif head_mode == "detached":
        git(repository, "checkout", "--detach", "HEAD")
    original = module._git
    def guarded(directory, *args, **kwargs):
        assert directory != repository, "must never run Git with source configuration"
        return original(directory, *args, **kwargs)
    monkeypatch.setattr(module, "_git", guarded)
    preview = importer(repository, tmp_path).preview()
    assert preview["errors"] == []
    assert preview["commit"] == expected


@pytest.mark.parametrize("path", [".env", "dist/output.bin"])
@pytest.mark.parametrize("change", ["bytes", "deleted", "assume", "skip", "staged"])
def test_excluded_tracked_files_must_be_clean(repository, tmp_path, path, change):
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"original\0binary")
    commit(repository)
    assert importer(repository, tmp_path).preview()["errors"] == []
    if change == "deleted":
        target.unlink()
    else:
        target.write_bytes(b"changed")
        if change == "staged":
            git(repository, "add", "--", path)
        elif change in {"assume", "skip"}:
            git(repository, "update-index", "--assume-unchanged" if change == "assume"
                else "--skip-worktree", path)
    before = source_state(repository)
    assert importer(repository, tmp_path).preview()["errors"]
    assert source_state(repository) == before


@pytest.mark.skipif(os.name == "nt", reason="native Windows has no executable bit")
@pytest.mark.parametrize("name", ["hello.py", ".env"])
@pytest.mark.parametrize("mode", [0o755, 0o777])
def test_real_or_synthetic_executable_mismatch_is_explicit(repository, tmp_path, name, mode):
    target = repository / name
    if not target.exists():
        target.write_bytes(b"secret")
        commit(repository)
    target.chmod(mode)
    # Source core.filemode=false cannot conceal a changed bit.
    errors = importer(repository, tmp_path).preview()["errors"]
    assert any("executable mode differs" in error for error in errors)


def test_bounded_ignore_walk_and_negations(repository, tmp_path):
    (repository / ".gitignore").write_text("ignored/\n*.log\n!keep.log\n")
    (repository / "sub").mkdir()
    (repository / "sub" / ".gitignore").write_text("*.tmp\n!keep.tmp\n")
    commit(repository)
    (repository / "ignored").mkdir()
    (repository / "ignored" / "anything").write_text("ignored")
    (repository / "debug.log").write_text("ignored")
    (repository / "sub" / "debug.tmp").write_text("ignored")
    assert importer(repository, tmp_path).preview()["errors"] == []
    (repository / "keep.log").write_text("untracked")
    (repository / "sub" / "keep.tmp").write_text("untracked")
    errors = importer(repository, tmp_path).preview()["errors"]
    assert any("keep.log: untracked" in error for error in errors)
    assert any("sub/keep.tmp: untracked" in error for error in errors)


def test_info_exclude_but_not_source_config_excludes(repository, tmp_path):
    (repository / ".git" / "info").mkdir(exist_ok=True)
    (repository / ".git" / "info" / "exclude").write_text("local.tmp\n")
    (repository / "local.tmp").write_text("ignored")
    outside = tmp_path / "global-ignore"
    outside.write_text("hidden.tmp\n")
    git(repository, "config", "core.excludesFile", str(outside))
    (repository / "hidden.tmp").write_text("must not trust source config")
    errors = importer(repository, tmp_path).preview()["errors"]
    assert errors == ["hidden.tmp: untracked file"]


def test_scan_is_bounded_even_for_empty_directories(repository, tmp_path, monkeypatch):
    import devflow.project_import as module
    monkeypatch.setattr(module, "MAX_SCAN_ENTRIES", 8)
    for index in range(12):
        (repository / f"dir-{index}").mkdir()
    assert any("scan exceeds" in error for error in importer(repository, tmp_path).preview()["errors"])


def test_excluded_and_ignored_directories_are_not_descended(repository, tmp_path, monkeypatch):
    import devflow.project_import as module
    (repository / ".gitignore").write_text("ignored/\n")
    commit(repository)
    for name in ["node_modules", "ignored"]:
        directory = repository / name
        directory.mkdir()
        for index in range(20):
            (directory / str(index)).mkdir()
    monkeypatch.setattr(module, "MAX_SCAN_ENTRIES", 8)
    assert importer(repository, tmp_path).preview()["errors"] == []


def test_untracked_directory_symlink_is_not_followed(repository, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (repository / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert any("links" in error for error in importer(repository, tmp_path).preview()["errors"])


def test_ignored_tracked_file_cannot_hide_dirty_bytes(repository, tmp_path):
    (repository / "tracked.log").write_bytes(b"committed")
    commit(repository)
    (repository / ".gitignore").write_text("*.log\n")
    commit(repository)
    (repository / "tracked.log").write_bytes(b"changed")
    errors = importer(repository, tmp_path).preview()["errors"]
    assert any("tracked.log: working tree differs" in error for error in errors)


def test_excluded_tracked_size_is_bounded(repository, tmp_path):
    (repository / ".env").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    commit(repository)
    errors = importer(repository, tmp_path).preview()["errors"]
    assert any(".env: file exceeds" in error for error in errors)

@pytest.mark.parametrize("change", ["intent", "empty_delete", "mode"])
def test_index_only_changes_rejected_without_source_refresh(repository, tmp_path, change):
    if change == "intent":
        (repository / "new-empty").write_bytes(b"")
        git(repository, "add", "--intent-to-add", "new-empty")
    elif change == "empty_delete":
        (repository / "empty").write_bytes(b"")
        commit(repository)
        git(repository, "rm", "--cached", "empty")
    else:
        git(repository, "update-index", "--chmod=+x", "hello.py")
    before = source_state(repository)
    errors = importer(repository, tmp_path).preview()["errors"]
    assert any("staged changes" in error for error in errors)
    assert source_state(repository) == before


def test_oversized_ignore_file_is_rejected(repository, tmp_path):
    (repository / ".git" / "info").mkdir(exist_ok=True)
    (repository / ".git" / "info" / "exclude").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    before = source_state(repository)
    assert importer(repository, tmp_path).preview()["errors"]
    assert source_state(repository) == before
