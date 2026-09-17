from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from support import PassingRunner

from devflow.coordinator import RunCoordinator
from devflow.errors import InvalidRunTransition, RevisionConflict
from devflow.models import DecisionKind, FilePatchSet
from devflow.workspace import CandidateBoundaryError


@pytest.mark.parametrize("renewal_error", [False, True])
@pytest.mark.parametrize("recovery", ["restart", "retry"])
@pytest.mark.parametrize("candidate_missing", [False, True])
def test_committed_publication_survives_renewal_loss(tmp_path, monkeypatch, renewal_error, recovery, candidate_missing):
    path = tmp_path / "state.sqlite"
    coordinator = RunCoordinator(path, runner=PassingRunner())
    coordinator.start(run_id="run-published", patch_id="patch-published")
    request = {"run_id": "run-published", "patch_revision": 1,
               "decision_id": "published", "kind": DecisionKind.APPROVE}
    original = coordinator.database.renew_decision_resume_claim

    def lose_renewal_after_publication(**kwargs):
        if coordinator.database.publication_for_decision("published") is not None:
            if renewal_error:
                raise sqlite3.OperationalError("injected renewal failure")
            return False
        return original(**kwargs)

    monkeypatch.setattr(coordinator.database, "renew_decision_resume_claim", lose_renewal_after_publication)
    with pytest.raises(InvalidRunTransition, match="lease renewal failed"):
        coordinator.decide(**request)
    assert coordinator.snapshot("run-published")["workspace_revision"] == 1
    assert coordinator.workspace.read_revision(1)
    assert coordinator.snapshot("run-published")["status"] == "APPLYING"
    assert coordinator.database.list_unfinished_decisions("run-published")
    monkeypatch.setattr(coordinator.database, "renew_decision_resume_claim", original)
    if candidate_missing:
        candidate = coordinator.workspace.candidate_path("run-published")
        candidate.rename(candidate.with_name("saved-candidate"))
    if recovery == "restart":
        coordinator = RunCoordinator(path, runner=PassingRunner())
    else:
        coordinator.decide(**request)
    assert coordinator.snapshot("run-published")["status"] == "COMPLETE"
    assert coordinator.decide(**request)["status"] == "COMPLETE"
    events = [row["type"] for row in coordinator.database.list_events("run-published")]
    assert events.count("workspace.published") == 1
    assert events.count("run.completed") == 1
    assert "run.failed" not in events
    assert coordinator.database.list_unfinished_decisions("run-published") == []


@pytest.mark.parametrize("window", ["partial-stage", "complete-stage", "legacy-stage", "renamed"])
def test_process_exit_publication_recovers_and_frees_active_slot(tmp_path, window):
    path = tmp_path / "crash.sqlite"
    child = r"""
import os, sys
sys.path.insert(0, sys.argv[3])
from support import PassingRunner
from devflow.coordinator import RunCoordinator
from devflow.models import DecisionKind
c = RunCoordinator(sys.argv[1], runner=PassingRunner())
c.start(run_id="run-crash", patch_id="patch-crash")
write = c.workspace._write_tree
finalize = c.database.finalize_publication
def crash_write(root, files):
    if sys.argv[2] == "partial-stage":
        files = dict(list(files.items())[:1])
    write(root, files)
    if sys.argv[2] == "legacy-stage":
        root.rename(root.with_name(".00000001.crash-d.publishing"))
    os._exit(73)
def crash_finalize(**kwargs):
    publish = kwargs["publish"]
    def crash_after_rename():
        publish()
        os._exit(74)
    kwargs["publish"] = crash_after_rename
    return finalize(**kwargs)
if sys.argv[2] == "renamed":
    c.database.finalize_publication = crash_finalize
else:
    c.workspace._write_tree = crash_write
c.decide(run_id="run-crash", patch_revision=1, decision_id="crash-d", kind=DecisionKind.APPROVE)
"""
    result = subprocess.run(
        [sys.executable, "-c", child, str(path), window, str(Path(__file__).parent)],
        env=os.environ.copy(), capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == (74 if window == "renamed" else 73), result.stderr
    # Only the isolated test DB's abandoned lease is expired; no wall-clock race.
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE decision_resume_claims SET lease_expires_at = '2000-01-01T00:00:00+00:00'")
    coordinator = RunCoordinator(path, runner=PassingRunner())
    assert coordinator.snapshot("run-crash")["status"] == "COMPLETE"
    assert coordinator.snapshot("run-crash")["workspace_revision"] == 1
    patch = FilePatchSet.model_validate_json(coordinator.database.current_patch("run-crash")["patch_json"])
    assert len(patch.files) > 1
    assert coordinator.workspace.read_revision(1) == {f.path: f.modified for f in patch.files}
    assert list(coordinator.workspace.revisions_root.glob("*.publishing")) == []
    assert coordinator.database.list_unfinished_decisions("run-crash") == []
    events = [r["type"] for r in coordinator.database.list_events("run-crash")]
    assert events.count("workspace.published") == events.count("run.completed") == 1
    assert coordinator.start(run_id="run-next", patch_id="patch-next")["status"] == "AWAITING_APPROVAL"


def pending_publication(tmp_path):
    coordinator = RunCoordinator(tmp_path / "state.sqlite", runner=PassingRunner())
    coordinator.start(run_id="run-staging", patch_id="patch-staging")
    coordinator.database.record_decision(
        run_id="run-staging", patch_revision=1, decision_id="stage-d",
        kind=DecisionKind.APPROVE, feedback=None,
    )
    assert coordinator.database.claim_decision_resume(
        decision_id="stage-d", owner_id="owner", lease_seconds=30,
    ) == "acquired"
    patch = FilePatchSet.model_validate_json(coordinator.database.current_patch("run-staging")["patch_json"])
    staging = coordinator.workspace.revisions_root / ".00000001.run-run-staging.publishing"
    return coordinator, patch, staging


def test_stale_head_is_rejected_before_revision_becomes_visible(tmp_path, monkeypatch):
    coordinator, patch, _staging = pending_publication(tmp_path)
    target = coordinator.workspace.revision_path(1)
    original = coordinator.database.finalize_publication

    def advance_head_before_finalization(**kwargs):
        with coordinator.database.transaction() as connection:
            connection.execute(
                "UPDATE workspaces SET current_revision = 2 WHERE id = 'default'"
            )
        return original(**kwargs)

    monkeypatch.setattr(
        coordinator.database, "finalize_publication", advance_head_before_finalization
    )

    with pytest.raises(RevisionConflict, match="head moved"):
        coordinator.workspace.publish_patch(
            decision_id="stage-d",
            owner_id="owner",
            run_id="run-staging",
            patch=patch,
        )

    assert not target.exists()
    assert coordinator.database.publication_for_decision("stage-d") is None


@pytest.mark.parametrize("claim", ["expired", "other-owner"])
def test_staging_is_not_removed_without_a_valid_owned_lease(tmp_path, claim):
    coordinator, patch, staging = pending_publication(tmp_path)
    staging.mkdir()
    (staging / "partial.txt").write_text("partial", encoding="utf-8")
    with coordinator.database.transaction() as connection:
        if claim == "expired":
            connection.execute("UPDATE decision_resume_claims SET lease_expires_at = '2000-01-01T00:00:00+00:00'")
        else:
            connection.execute("UPDATE decision_resume_claims SET owner_id = 'other'")
    with pytest.raises(InvalidRunTransition, match="lease"):
        coordinator.workspace.publish_patch(decision_id="stage-d", owner_id="owner", run_id="run-staging", patch=patch)
    assert (staging / "partial.txt").read_text(encoding="utf-8") == "partial"
    assert not coordinator.workspace.revision_path(1).exists()
    assert coordinator.snapshot("run-staging")["workspace_revision"] == 0


@pytest.mark.parametrize("link_kind", ["root", "nested", "hardlink", "not-directory"])
def test_unsafe_staging_is_not_followed_or_deleted(tmp_path, link_kind):
    coordinator, patch, staging = pending_publication(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    try:
        if link_kind == "root":
            os.symlink(outside, staging, target_is_directory=True)
        elif link_kind == "not-directory":
            staging.write_text("not a directory", encoding="utf-8")
        else:
            staging.mkdir()
            if link_kind == "nested":
                os.symlink(outside, staging / "linked", target_is_directory=True)
            else:
                os.link(sentinel, staging / "linked.txt")
    except OSError as error:
        pytest.skip(f"link creation unavailable: {error}")
    with pytest.raises(CandidateBoundaryError):
        coordinator.workspace.publish_patch(decision_id="stage-d", owner_id="owner", run_id="run-staging", patch=patch)
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert os.path.lexists(staging)
    assert not coordinator.workspace.revision_path(1).exists()
    assert coordinator.snapshot("run-staging")["workspace_revision"] == 0


def test_already_recorded_path_shaped_decision_can_recover(tmp_path):
    path = tmp_path / "state.sqlite"
    coordinator = RunCoordinator(path, runner=PassingRunner())
    coordinator.start(run_id="run-legacy-key", patch_id="patch-legacy-key")
    coordinator.database.record_decision(
        run_id="run-legacy-key", patch_revision=1, decision_id="team/choice",
        kind=DecisionKind.APPROVE, feedback=None,
    )
    coordinator = RunCoordinator(path, runner=PassingRunner())
    assert coordinator.snapshot("run-legacy-key")["status"] == "COMPLETE"
    assert coordinator.database.publication_for_decision("team/choice")["revision"] == 1


def test_staging_cleanup_rechecks_lease_after_tree_validation(tmp_path, monkeypatch):
    coordinator, patch, staging = pending_publication(tmp_path)
    staging.mkdir()
    marker = staging / "partial.txt"
    marker.write_text("partial", encoding="utf-8")
    original = coordinator.workspace._read_text_tree

    def expire_during_validation(root):
        result = original(root)
        if root == staging:
            with coordinator.database.transaction() as connection:
                connection.execute("UPDATE decision_resume_claims SET lease_expires_at = '2000-01-01T00:00:00+00:00'")
        return result

    monkeypatch.setattr(coordinator.workspace, "_read_text_tree", expire_during_validation)
    with pytest.raises(InvalidRunTransition, match="lease"):
        coordinator.workspace.publish_patch(decision_id="stage-d", owner_id="owner", run_id="run-staging", patch=patch)
    assert marker.read_text(encoding="utf-8") == "partial"
    assert not coordinator.workspace.revision_path(1).exists()
