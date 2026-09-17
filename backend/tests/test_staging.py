from __future__ import annotations

import pytest
from pydantic import ValidationError

from devflow.models import MAX_FILE_BYTES, FilePatch, FilePatchSet
from devflow.staging import StagingFileStore


@pytest.mark.parametrize("path", [
    "/absolute", "//server/share", "../escape", "a/../escape", "C:/escape",
    "C:\\escape", "C:relative", "a\\b", "a/..\\b", "a//b", ".git/config",
    "NUL.txt", "CONIN$", "folder/CONOUT$.log", "COM¹", "LPT³.txt", "file:stream",
    "trailing. ", "a\x00b",
])
def test_unsafe_patch_paths_rejected(path):
    with pytest.raises(ValidationError):
        FilePatch(path=path, original=None, modified="text")


@pytest.mark.parametrize("content", ["\x00binary", "x" * (MAX_FILE_BYTES + 1)], ids=["binary", "oversized"])
def test_binary_and_oversized_patch_rejected(content):
    with pytest.raises(ValidationError):
        FilePatch(path="file.txt", original=None, modified=content)


@pytest.mark.parametrize("paths", [["a", "a"], ["a", "A"], ["a", "a/b"], [str(i) for i in range(101)]])
def test_duplicate_conflicting_or_excessive_patch_files_rejected(paths):
    with pytest.raises(ValidationError):
        FilePatchSet(run_id="r", patch_revision=1, base_workspace_revision=0,
                     files=[FilePatch(path=p, original=None, modified="new") for p in paths])


def test_staging_is_pure_and_produces_diff_for_create_update_delete():
    source = {"update": "before\n", "delete": "gone\n"}
    patch = FilePatchSet(run_id="r", patch_revision=1, base_workspace_revision=0, files=[
        FilePatch(path="update", original="before\n", modified="after\n"),
        FilePatch(path="delete", original="gone\n", modified=None),
        FilePatch(path="new", original=None, modified="new\n"),
    ])
    staged = StagingFileStore(source)
    assert staged.stage(patch) == {"update": "after\n", "new": "new\n"}
    assert source == {"update": "before\n", "delete": "gone\n"}
    assert staged.diff(patch) == (
        "--- a/update\n+++ b/update\n@@ -1 +1 @@\n-before\n+after\n"
        "--- a/delete\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
        "--- /dev/null\n+++ b/new\n@@ -0,0 +1 @@\n+new\n"
    )


def test_diff_marks_missing_final_newlines_and_empty_file_operations():
    patch = FilePatchSet(run_id="r", patch_revision=1, base_workspace_revision=0, files=[
        FilePatch(path="no-newline", original="before", modified="after"),
        FilePatch(path="empty-created", original=None, modified=""),
        FilePatch(path="empty-deleted", original="", modified=None),
    ])

    assert StagingFileStore.diff(patch) == (
        "--- a/no-newline\n+++ b/no-newline\n@@ -1 +1 @@\n"
        "-before\n\\ No newline at end of file\n"
        "+after\n\\ No newline at end of file\n"
        "--- /dev/null\n+++ b/empty-created\n"
        "--- a/empty-deleted\n+++ /dev/null\n"
    )


def test_diff_treats_carriage_return_without_line_feed_as_missing_final_newline():
    patch = FilePatchSet(run_id="r", patch_revision=1, base_workspace_revision=0, files=[
        FilePatch(path="cr-only", original="before\r", modified="after\r"),
    ])

    assert StagingFileStore.diff(patch) == (
        "--- a/cr-only\n+++ b/cr-only\n@@ -1 +1 @@\n"
        "-before\r\n\\ No newline at end of file\n"
        "+after\r\n\\ No newline at end of file\n"
    )


def test_wrong_original_is_rejected_without_mutating_input():
    source = {"file": "current"}
    patch = FilePatchSet(run_id="r", patch_revision=1, base_workspace_revision=0, files=[
        FilePatch(path="file", original="old", modified="new"),
    ])
    with pytest.raises(ValueError, match="original"):
        StagingFileStore(source).stage(patch)
    assert source == {"file": "current"}
