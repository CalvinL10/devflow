from __future__ import annotations

import os
import subprocess

import pytest

from devflow.coordinator import RunCoordinator
from devflow.database import Database
from devflow.errors import RevisionConflict
from devflow.models import MAX_FILE_BYTES
from devflow.workspace import CandidateBoundaryError, ManagedWorkspace

pytestmark = pytest.mark.usefixtures("mock_runner")


def create_junction(link, target) -> None:
    if os.name != "nt":
        pytest.skip("junctions are Windows-specific")
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation is not available: {completed.stderr}")


def test_candidate_is_materialized_from_managed_revision(tmp_path) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    database.create_run(
        run_id="run-1",
        thread_id="run-1",
        patch_id="patch-1",
        patch_revision=1,
        candidate_dir=str(tmp_path / "managed" / "candidates" / "run-1"),
        patch={"files": []},
    )
    workspace = ManagedWorkspace(tmp_path / "managed", database)
    workspace.initialize()
    published = workspace.revision_path(0) / "tracked.txt"
    published.write_text("published", encoding="utf-8")

    candidate = workspace.materialize_candidate(run_id="run-1", base_revision=0)
    (candidate / "tracked.txt").write_text("candidate", encoding="utf-8")

    assert published.read_text(encoding="utf-8") == "published"
    assert (candidate / "tracked.txt").read_text(encoding="utf-8") == "candidate"


def test_candidate_run_id_cannot_escape_root(tmp_path) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    workspace = ManagedWorkspace(tmp_path / "managed", database)
    with pytest.raises(CandidateBoundaryError):
        workspace.candidate_path("../escape")


def test_symlink_in_revision_is_rejected(tmp_path) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    database.create_run(
        run_id="run-1",
        thread_id="run-1",
        patch_id="patch-1",
        patch_revision=1,
        candidate_dir=str(tmp_path / "managed" / "candidates" / "run-1"),
        patch={"files": []},
    )
    workspace = ManagedWorkspace(tmp_path / "managed", database)
    workspace.initialize()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = workspace.revision_path(0) / "escape.txt"
    try:
        os.symlink(outside, link)
    except OSError as error:
        pytest.skip(f"symlink creation is not available: {error}")
    with pytest.raises(CandidateBoundaryError, match="symbolic link"):
        workspace.materialize_candidate(run_id="run-1", base_revision=0)


def test_hardlink_in_revision_is_rejected(tmp_path) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    database.create_run(
        run_id="run-1",
        thread_id="run-1",
        patch_id="patch-1",
        patch_revision=1,
        candidate_dir=str(tmp_path / "managed" / "candidates" / "run-1"),
        patch={"files": []},
    )
    workspace = ManagedWorkspace(tmp_path / "managed", database)
    workspace.initialize()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        os.link(outside, workspace.revision_path(0) / "escape.txt")
    except OSError as error:
        pytest.skip(f"hardlink creation is not available: {error}")

    with pytest.raises(CandidateBoundaryError, match="hard-linked"):
        workspace.materialize_candidate(run_id="run-1", base_revision=0)
    assert outside.read_text(encoding="utf-8") == "outside"


def test_oversized_revision_file_is_rejected_without_candidate(tmp_path) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    database.create_run(
        run_id="run-1",
        thread_id="run-1",
        patch_id="patch-1",
        patch_revision=1,
        candidate_dir=str(tmp_path / "managed" / "candidates" / "run-1"),
        patch={"files": []},
    )
    workspace = ManagedWorkspace(tmp_path / "managed", database)
    workspace.initialize()
    (workspace.revision_path(0) / "large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))

    with pytest.raises(CandidateBoundaryError, match="bounded|size"):
        workspace.materialize_candidate(run_id="run-1", base_revision=0)
    assert not workspace.candidate_path("run-1").exists()


def test_stale_workspace_revision_is_rejected_before_materialization(tmp_path) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    database.create_run(
        run_id="run-1",
        thread_id="run-1",
        patch_id="patch-1",
        patch_revision=1,
        candidate_dir=str(tmp_path / "managed" / "candidates" / "run-1"),
        patch={"files": []},
    )
    workspace = ManagedWorkspace(tmp_path / "managed", database)
    workspace.initialize()
    with database.transaction() as connection:
        connection.execute("UPDATE workspaces SET current_revision = 1 WHERE id = 'default'")

    with pytest.raises(RevisionConflict, match="head moved"):
        workspace.materialize_candidate(run_id="run-1", base_revision=0)
    assert not workspace.candidate_path("run-1").exists()


def test_coordinator_rejects_arbitrary_candidate_directory(tmp_path) -> None:
    coordinator = RunCoordinator(tmp_path / "devflow.sqlite")

    with pytest.raises(CandidateBoundaryError, match="managed candidate"):
        coordinator.start(
            run_id="run-1",
            patch_id="patch-1",
            candidate_dir=str(tmp_path / "outside"),
        )

    with pytest.raises(KeyError):
        coordinator.database.get_run("run-1")


def test_coordinator_materializes_and_persists_managed_candidate(tmp_path) -> None:
    coordinator = RunCoordinator(tmp_path / "devflow.sqlite")
    published = coordinator.workspace.revision_path(0) / "tracked.txt"
    published.write_text("published", encoding="utf-8")

    candidate = coordinator.workspace.candidate_path("run-1")
    snapshot = coordinator.start(
        run_id="run-1", patch_id="patch-1", candidate_dir=str(candidate)
    )

    patch = coordinator.database.get_patch("run-1", 1)
    assert snapshot["status"] == "AWAITING_APPROVAL"
    assert patch["candidate_dir"] == str(candidate)
    assert (candidate / "tracked.txt").read_text(encoding="utf-8") == "published"


@pytest.mark.parametrize("managed_child", ["revisions", "candidates"])
def test_managed_roots_cannot_be_junctions(tmp_path, managed_child: str) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / f"outside-{managed_child}"
    outside.mkdir()
    create_junction(managed / managed_child, outside)

    workspace = ManagedWorkspace(managed, database)
    with pytest.raises(CandidateBoundaryError, match="reparse point"):
        workspace.initialize()


def test_workspace_root_cannot_have_a_junction_ancestor(tmp_path) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    outside = tmp_path / "outside-root"
    outside.mkdir()
    alias = tmp_path / "alias"
    create_junction(alias, outside)

    workspace = ManagedWorkspace(alias / "managed", database)
    with pytest.raises(CandidateBoundaryError, match="reparse point"):
        workspace.initialize()

    assert not (outside / "managed").exists()


def test_workspace_root_cannot_have_a_symlink_ancestor(tmp_path) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    outside = tmp_path / "outside-symlink-root"
    outside.mkdir()
    alias = tmp_path / "symlink-alias"
    try:
        os.symlink(outside, alias, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation is not available: {error}")

    workspace = ManagedWorkspace(alias / "managed", database)
    with pytest.raises(CandidateBoundaryError, match="symbolic link"):
        workspace.initialize()

    assert not (outside / "managed").exists()


@pytest.mark.parametrize("managed_child", ["revisions", "candidates"])
def test_managed_roots_cannot_be_directory_symlinks(
    tmp_path, managed_child: str
) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / f"outside-symlink-{managed_child}"
    outside.mkdir()
    try:
        os.symlink(outside, managed / managed_child, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation is not available: {error}")

    workspace = ManagedWorkspace(managed, database)
    with pytest.raises(CandidateBoundaryError, match="symbolic link"):
        workspace.initialize()


def test_junction_inside_revision_is_rejected(tmp_path) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    database.create_run(
        run_id="run-1",
        thread_id="run-1",
        patch_id="patch-1",
        patch_revision=1,
        candidate_dir=str(tmp_path / "managed" / "candidates" / "run-1"),
        patch={"files": []},
    )
    workspace = ManagedWorkspace(tmp_path / "managed", database)
    workspace.initialize()
    outside = tmp_path / "outside-revision"
    outside.mkdir()
    (outside / "outside.txt").write_text("outside", encoding="utf-8")
    create_junction(workspace.revision_path(0) / "linked", outside)

    with pytest.raises(CandidateBoundaryError, match="reparse point"):
        workspace.materialize_candidate(run_id="run-1", base_revision=0)


def test_link_swap_between_validation_and_copy_is_rejected(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    database.create_run(
        run_id="run-1",
        thread_id="run-1",
        patch_id="patch-1",
        patch_revision=1,
        candidate_dir=str(tmp_path / "managed" / "candidates" / "run-1"),
        patch={"files": []},
    )
    workspace = ManagedWorkspace(tmp_path / "managed", database)
    workspace.initialize()
    source_file = workspace.revision_path(0) / "tracked.txt"
    source_file.write_text("managed", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    original_reject_links = workspace._reject_links
    swapped = False

    def swap_after_validation(root) -> None:
        nonlocal swapped
        original_reject_links(root)
        if root == workspace.revision_path(0) and not swapped:
            swapped = True
            source_file.unlink()
            os.symlink(outside, source_file)

    monkeypatch.setattr(workspace, "_reject_links", swap_after_validation)

    with pytest.raises(CandidateBoundaryError, match="symbolic link"):
        workspace.materialize_candidate(run_id="run-1", base_revision=0)
    assert not workspace.candidate_path("run-1").exists()
    assert not workspace.candidates_root.joinpath(".run-1.materializing").exists()


def test_target_hardlink_race_does_not_overwrite_external_file(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "devflow.sqlite")
    database.initialize()
    database.create_run(
        run_id="run-1",
        thread_id="run-1",
        patch_id="patch-1",
        patch_revision=1,
        candidate_dir=str(tmp_path / "managed" / "candidates" / "run-1"),
        patch={"files": []},
    )
    workspace = ManagedWorkspace(tmp_path / "managed", database)
    workspace.initialize()
    (workspace.revision_path(0) / "tracked.txt").write_text("managed", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    original_copy = ManagedWorkspace._copy_regular_file
    linked = False

    def precreate_hardlink(cls, source, target, expected):
        nonlocal linked
        if not linked:
            try:
                os.link(outside, target)
            except OSError as error:
                pytest.skip(f"hardlink creation is not available: {error}")
            linked = True
        return original_copy(source, target, expected)

    monkeypatch.setattr(
        ManagedWorkspace, "_copy_regular_file", classmethod(precreate_hardlink)
    )

    with pytest.raises(CandidateBoundaryError, match="exclusively"):
        workspace.materialize_candidate(run_id="run-1", base_revision=0)
    assert outside.read_text(encoding="utf-8") == "secret"
    assert not workspace.candidate_path("run-1").exists()
    assert not workspace.candidates_root.joinpath(".run-1.materializing").exists()
