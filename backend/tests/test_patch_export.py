import subprocess

import pytest

from devflow.models import FilePatch, FilePatchSet
from devflow.patch_export import export_patch
from devflow.project_import import _git_env


def patch(*files):
    return FilePatchSet(
        run_id="test",
        patch_revision=1,
        base_workspace_revision=0,
        files=[FilePatch(path=p, original=a, modified=b) for p, a, b in files],
    )


def git(repo, *args, data=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=data, check=True, capture_output=True, env=_git_env()
    ).stdout


def apply_and_check(tmp_path, originals, changes, modes):
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "--quiet", "--template=")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "core.filemode", "false")
    for name, text in originals.items():
        file = repo / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(text.encode("utf-8"))
        oid = git(repo, "hash-object", "-w", "--stdin", data=text.encode()).decode().strip()
        git(repo, "update-index", "--add", "--cacheinfo", modes.get(name, "100644"), oid, name)
    exported = export_patch(originals, changes, modes)
    git(repo, "apply", "--cached", "--check", "--whitespace=nowarn", "-", data=exported)
    git(repo, "apply", "--cached", "--whitespace=nowarn", "-", data=exported)
    expected = dict(originals)
    for change in changes.files:
        if change.modified is None:
            expected.pop(change.path)
        else:
            expected[change.path] = change.modified
    indexed = git(repo, "ls-files", "--stage", "-z").split(b"\0")
    result = {}
    for row in filter(None, indexed):
        info, name = row.split(b"\t", 1)
        mode, oid, _ = info.split()
        result[name.decode()] = git(repo, "cat-file", "blob", oid.decode()).decode()
        assert mode.decode() == modes.get(name.decode(), "100644")
    assert result == expected
    # Also apply to the physical checkout, checking bytes rather than text-mode IO.
    git(repo, "apply", "--whitespace=nowarn", "-", data=exported)
    for name, text in expected.items():
        assert (repo / name).read_bytes() == text.encode("utf-8")
    for name in set(originals) - set(expected):
        assert not (repo / name).exists()
    return exported


def test_native_apply_crlf_empty_files_and_modes(tmp_path):
    originals = {
        "script.sh": "echo old\r\n",
        "empty-delete": "",
        "delete": "last line",
        "to-empty": "content\n",
        "sp ace/文.txt": "old",
    }
    changes = patch(
        ("script.sh", originals["script.sh"], "echo new\r\n"),
        ("empty-delete", "", None),
        ("empty-create", None, ""),
        ("delete", "last line", None),
        ("to-empty", "content\n", ""),
        ("sp ace/文.txt", "old", "new without newline"),
    )
    data = apply_and_check(tmp_path, originals, changes, {"script.sh": "100755"})
    assert b"100755" in data
    assert b"echo new\r\n" in data
    assert b"new file mode 100644" in data
    assert b"deleted file mode 100644" in data


@pytest.mark.parametrize(
    "before,after",
    [
        ("", "one"),
        ("one", ""),
        ("a\r\nb\r\n", "a\r\nc\r\n"),
        ("a\n", "a"),
        (None, "new\r\n"),
        ("old\r\n", None),
    ],
)
def test_git_apply_edge_cases(tmp_path, before, after):
    originals = {} if before is None else {"file": before}
    apply_and_check(tmp_path, originals, patch(("file", before, after)), {})


def test_attributes_do_not_transform_blobs(tmp_path):
    originals = {".gitattributes": "* text eol=lf filter=evil diff=evil\n", "file": "a\r\n"}
    apply_and_check(tmp_path, originals, patch(("file", "a\r\n", "b\r\n")), {})


def test_empty_patch():
    assert export_patch({"a": "text"}, patch(), {}) == b""


@pytest.mark.parametrize(
    "originals,modes,changes",
    [
        ({"a": "a"}, {}, (("a", "wrong", "new"),)),
        ({"a": "a"}, {"a": "120000"}, (("a", "a", "new"),)),
        ({"a": "a"}, {"unknown": "100755"}, (("a", "a", "new"),)),
        ({"../bad": "a"}, {}, ()),
        ({"A": "a", "a": "b"}, {}, ()),
        ({"Dir/a": "a"}, {}, (("dir", None, "b"),)),
        ({"bad": "a\0b"}, {}, ()),
    ],
)
def test_reject_invalid_export(originals, modes, changes):
    with pytest.raises(ValueError):
        export_patch(originals, patch(*changes), modes)
