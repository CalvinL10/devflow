from __future__ import annotations

from collections.abc import Mapping
from difflib import unified_diff

from devflow.models import FilePatch, FilePatchSet


class StagingFileStore:
    """Pure in-memory staging: no paths, filesystem handles or write tools."""

    def __init__(self, originals: Mapping[str, str]):
        self.originals = dict(originals)

    def stage(self, patch: FilePatchSet) -> dict[str, str]:
        patch = FilePatchSet.model_validate(patch.model_dump())
        result = dict(self.originals)
        for file in patch.files:
            if self.originals.get(file.path) != file.original:
                raise ValueError(f"original contents do not match: {file.path}")
            aliases = [p for p in result if p.casefold() == file.path.casefold()]
            if aliases and file.path not in aliases:
                raise ValueError("case alias in patch path")
            if any(p.startswith(file.path + "/") or file.path.startswith(p + "/") for p in result):
                raise ValueError("patch file conflicts with a directory")
            if file.modified is None:
                result.pop(file.path)
            else:
                result[file.path] = file.modified
        return result

    @staticmethod
    def diff(patch: FilePatchSet) -> str:
        patch = FilePatchSet.model_validate(patch.model_dump())
        return "".join(
            line
            for file in patch.files
            for line in StagingFileStore._file_diff(file)
        )

    @staticmethod
    def _file_diff(file: FilePatch) -> list[str]:
        fromfile = f"a/{file.path}" if file.original is not None else "/dev/null"
        tofile = f"b/{file.path}" if file.modified is not None else "/dev/null"
        lines = list(
            unified_diff(
                (file.original or "").splitlines(keepends=True),
                (file.modified or "").splitlines(keepends=True),
                fromfile=fromfile,
                tofile=tofile,
            )
        )
        if not lines:
            # Empty-file create/delete has no content hunk, but the operation must
            # remain visible to reviewers.
            return [f"--- {fromfile}\n", f"+++ {tofile}\n"]

        rendered: list[str] = []
        for index, line in enumerate(lines):
            if index < 2 or line.startswith("@@") or line.endswith("\n"):
                rendered.append(line)
                continue
            rendered.extend((f"{line}\n", "\\ No newline at end of file\n"))
        return rendered
