import json
import subprocess

import pytest

from devflow.models import MAX_FILE_BYTES
from devflow.project_import import ProjectImporter, _git_env


def git(repo, *args, data=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=data, check=True, capture_output=True, env=_git_env()
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
