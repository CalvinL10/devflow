from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from support import seed_approval_evidence

from devflow.database import Database
from devflow.errors import (
    ActiveRunConflict,
    IdempotencyConflict,
    InvalidRunTransition,
    RevisionConflict,
)
from devflow.models import DecisionKind, RunStatus


def create_run(database: Database, run_id: str = "run-1") -> None:
    database.create_run(
        run_id=run_id,
        thread_id=run_id,
        patch_id=f"patch-{run_id}",
        patch_revision=1,
        candidate_dir=f"/var/lib/devflow/runs/{run_id}/candidate",
        patch={"files": []},
    )
    seed_approval_evidence(database, run_id)


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "devflow.sqlite")
    value.initialize()
    return value


def test_initialize_creates_a_read_write_database(tmp_path) -> None:
    database = Database(tmp_path / "nested" / "devflow.sqlite")
    database.initialize()
    database.initialize()

    with database.transaction() as connection:
        connection.execute(
            "UPDATE workspaces SET current_revision = 1 WHERE id = 'default'"
        )

    with database.connect() as connection:
        workspace = connection.execute(
            "SELECT current_revision FROM workspaces WHERE id = 'default'"
        ).fetchone()

    assert database.path.is_file()
    assert workspace is not None
    assert workspace["current_revision"] == 1


def test_single_active_run_and_monotonic_events(database: Database) -> None:
    create_run(database)
    with pytest.raises(ActiveRunConflict):
        create_run(database, "run-2")
    database.mark_awaiting_approval("run-1")
    events = database.list_events("run-1")
    assert [event["seq"] for event in events] == [1, 2, 3, 4, 5]


def test_decision_id_is_idempotent_and_payload_is_immutable(database: Database) -> None:
    create_run(database)
    database.mark_awaiting_approval("run-1")
    first, created = database.record_decision(
        decision_id="decision-1",
        run_id="run-1",
        patch_revision=1,
        kind=DecisionKind.REJECT,
        feedback="not this change",
    )
    repeated, repeated_created = database.record_decision(
        decision_id="decision-1",
        run_id="run-1",
        patch_revision=1,
        kind=DecisionKind.REJECT,
        feedback="not this change",
    )
    assert created is True
    assert repeated_created is False
    assert repeated == first
    assert database.get_run("run-1")["status"] == RunStatus.REJECTED.value

    with pytest.raises(IdempotencyConflict):
        database.record_decision(
            decision_id="decision-1",
            run_id="run-1",
            patch_revision=1,
            kind=DecisionKind.APPROVE,
            feedback=None,
        )


def test_cancel_is_rejected_after_apply_starts(database: Database) -> None:
    create_run(database)
    database.mark_awaiting_approval("run-1")
    database.record_decision(
        decision_id="decision-1",
        run_id="run-1",
        patch_revision=1,
        kind=DecisionKind.APPROVE,
        feedback=None,
    )
    assert database.get_run("run-1")["status"] == RunStatus.APPLYING.value
    with pytest.raises(InvalidRunTransition, match="before apply"):
        database.cancel_run("run-1")


def test_cancel_before_apply_is_terminal(database: Database) -> None:
    create_run(database)
    database.cancel_run("run-1")
    assert database.get_run("run-1")["status"] == RunStatus.CANCELLED.value


def test_reject_decision_cannot_finish_as_complete(database: Database) -> None:
    create_run(database)
    database.mark_awaiting_approval("run-1")
    database.record_decision(
        decision_id="decision-1",
        run_id="run-1",
        patch_revision=1,
        kind=DecisionKind.REJECT,
        feedback=None,
    )
    assert (
        database.claim_decision_resume(
            decision_id="decision-1", owner_id="owner-1", lease_seconds=30
        )
        == "acquired"
    )

    with pytest.raises(InvalidRunTransition, match="reject decision"):
        database.finish_decision(
            "decision-1", RunStatus.COMPLETE, owner_id="owner-1"
        )

    assert database.get_run("run-1")["status"] == RunStatus.REJECTED.value
    assert [event["type"] for event in database.list_events("run-1")].count(
        "run.completed"
    ) == 0


def test_expired_decision_claim_can_be_recovered_and_finishes_once(
    database: Database,
) -> None:
    create_run(database)
    database.mark_awaiting_approval("run-1")
    database.record_decision(
        decision_id="decision-1",
        run_id="run-1",
        patch_revision=1,
        kind=DecisionKind.APPROVE,
        feedback=None,
    )
    assert (
        database.claim_decision_resume(
            decision_id="decision-1", owner_id="abandoned", lease_seconds=30
        )
        == "acquired"
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE decision_resume_claims SET lease_expires_at = ?
            WHERE decision_id = ?
            """,
            ("2000-01-01T00:00:00+00:00", "decision-1"),
        )

    assert (
        database.claim_decision_resume(
            decision_id="decision-1", owner_id="recovery", lease_seconds=30
        )
        == "acquired"
    )
    with pytest.raises(InvalidRunTransition, match="owned"):
        database.finish_decision(
            "decision-1", RunStatus.COMPLETE, owner_id="abandoned"
        )
    assert database.finish_decision(
        "decision-1", RunStatus.COMPLETE, owner_id="recovery"
    )
    assert not database.finish_decision(
        "decision-1", RunStatus.COMPLETE, owner_id="recovery"
    )
    assert [event["type"] for event in database.list_events("run-1")].count(
        "run.completed"
    ) == 1


def test_expired_decision_claim_cannot_renew_or_finish(database: Database) -> None:
    create_run(database)
    database.mark_awaiting_approval("run-1")
    database.record_decision(
        decision_id="decision-1",
        run_id="run-1",
        patch_revision=1,
        kind=DecisionKind.APPROVE,
        feedback=None,
    )
    assert (
        database.claim_decision_resume(
            decision_id="decision-1", owner_id="expired", lease_seconds=30
        )
        == "acquired"
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE decision_resume_claims SET lease_expires_at = ?
            WHERE decision_id = ?
            """,
            ("2000-01-01T00:00:00+00:00", "decision-1"),
        )

    assert not database.renew_decision_resume_claim(
        decision_id="decision-1", owner_id="expired", lease_seconds=30
    )
    assert not database.fail_decision(
        "decision-1", reason="lost lease", owner_id="expired"
    )
    assert database.get_run("run-1")["status"] == RunStatus.APPLYING.value
    with pytest.raises(InvalidRunTransition, match="expired"):
        database.finish_decision(
            "decision-1", RunStatus.COMPLETE, owner_id="expired"
        )


def test_stale_workspace_revision_rejects_decision(database: Database) -> None:
    create_run(database)
    database.mark_awaiting_approval("run-1")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE workspaces SET current_revision = 1 WHERE id = 'default'"
        )
    with pytest.raises(RevisionConflict, match="stale"):
        database.record_decision(
            decision_id="decision-1",
            run_id="run-1",
            patch_revision=1,
            kind=DecisionKind.APPROVE,
            feedback=None,
        )


def test_domain_foreign_keys_are_enabled(database: Database) -> None:
    with database.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
                INSERT INTO run_events(run_id, seq, type, payload_json, created_at)
                VALUES ('missing', 1, 'invalid', '{}', 'now')
                """
        )


@pytest.mark.parametrize("legacy_status", ["RUNNING", "CANCELLED"])
def test_incompatible_old_database_is_rejected_before_schema_or_data_changes(tmp_path, legacy_status):
    # Real schema from 43b3767 (the parent of Round 4), not a synthetic constraint.
    schema = (Path(__file__).parent / "fixtures" / "pre_round4_schema.sql").read_text(encoding="utf-8")
    database = Database(tmp_path / "old.sqlite")
    with database.connect() as connection:
        connection.executescript(schema)
        connection.execute("INSERT INTO workspaces VALUES ('default', 0, 'then', 'then')")
        connection.execute(
            "INSERT INTO runs(id, thread_id, workspace_id, base_workspace_revision, status, created_at, updated_at) "
            "VALUES ('old-run', 'old-thread', 'default', 0, ?, 'then', 'then')", (legacy_status,),
        )
        before = list(connection.iterdump())
    with pytest.raises(RuntimeError, match="incompatible.*database"):
        database.initialize()
    with database.connect() as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_additive_beta_migration_preserves_tasks_checkpoints_and_decisions(database):
    create_run(database)
    database.mark_awaiting_approval("run-1")
    database.record_decision(decision_id="retained-decision", run_id="run-1", patch_revision=1,
                             kind=DecisionKind.REJECT, feedback="retain me")
    database.save_checkpoint_ref(run_id="run-1", thread_id="run-1", checkpoint_ns="", checkpoint_id="saved")
    tables = ["runs", "patches", "run_artifacts", "run_events", "run_checkpoint_refs", "decisions"]
    with database.connect() as connection:
        connection.execute("DROP TABLE run_context")
        connection.execute("PRAGMA user_version = 0")
        before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                  for table in tables}
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in tables} == before
        assert connection.execute("SELECT * FROM run_context").fetchall() == []


def test_newer_database_version_refused_without_downgrade(database):
    with database.connect() as connection:
        connection.execute("PRAGMA user_version = 2")
    with pytest.raises(RuntimeError, match="newer"):
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
