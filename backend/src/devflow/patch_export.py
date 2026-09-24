"""Native Git patch generation without touching a source checkout or its index."""

from __future__ import annotations

from devflow.models import FilePatchSet
from devflow.project_import import MAX_TREE_BYTES, _git, _isolated_git, _validate_texts
from devflow.staging import StagingFileStore


def export_patch(originals: dict[str, str], patch: FilePatchSet, modes: dict[str, str]) -> bytes:
    """Return a git-apply-compatible byte patch, preserving exact UTF-8 bytes.

    Existing files retain their imported 100644/100755 mode; new files use
    100644. FilePatch does not gain a mode field. Missing modes default to
    100644 for callers without imported metadata; unknown/invalid modes fail.
    Git writes objects and indexes only inside a disposable repository. No
    checkout, attributes, filters, hooks, external diff or custom hashes run.
    """
    patch = FilePatchSet.model_validate(patch.model_dump())
    originals = dict(originals)
    modes = dict(modes)
    _validate_texts(originals)
    if set(modes) - set(originals) or any(m not in {"100644", "100755"} for m in modes.values()):
        raise ValueError("modes must refer to original files and be 100644 or 100755")
    modified = StagingFileStore(originals).stage(patch)
    _validate_texts(modified)
    with _isolated_git() as root:

        def tree(contents: dict[str, str]) -> str:
            _git(root, "read-tree", "--empty")
            records = []
            for path, text in sorted(contents.items()):
                oid = (
                    _git(
                        root,
                        "hash-object",
                        "-w",
                        "--stdin",
                        "--no-filters",
                        data=text.encode("utf-8"),
                    )
                    .decode()
                    .strip()
                )
                mode = modes.get(path, "100644")
                records.append(f"{mode} {oid}\t{path}".encode() + b"\0")
            if records:
                _git(root, "update-index", "-z", "--index-info", data=b"".join(records))
            return _git(root, "write-tree").decode().strip()

        before, after = tree(originals), tree(modified)
        return _git(
            root,
            "diff-tree",
            "--no-commit-id",
            "-r",
            "-p",
            "--binary",
            "--full-index",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            before,
            after,
            "--",
            limit=4 * MAX_TREE_BYTES,
        )
