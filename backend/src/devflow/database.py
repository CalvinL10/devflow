from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from devflow.errors import (
    ActiveRunConflict,
    IdempotencyConflict,
    InvalidRunTransition,
    RevisionConflict,
)
from devflow.models import CheckReport, DecisionKind, FilePatchSet, ReviewReport, RunStatus

ACTIVE_STATUSES = {
    RunStatus.CREATED,
    RunStatus.RUNNING,
    RunStatus.AWAITING_APPROVAL,
    RunStatus.APPLYING,
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    run_id: str
    patch_id: str
    patch_revision: int
    kind: DecisionKind
    feedback: str | None
    result_status: RunStatus
    resume_completed_at: str | None


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as connection:
            # CREATE IF NOT EXISTS cannot upgrade the pre-Round-4 cancellation
            # CHECK constraints. Refuse that known incompatible input before any
            # schema/data changes; this slice does not perform an implicit migration.
            definitions = {
                row["name"]: row["sql"] for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('runs', 'decisions')"
                )
            }
            if (
                "runs" in definitions and "'CANCELED'" not in definitions["runs"]
                or "decisions" in definitions and "'cancel'" not in definitions["decisions"]
            ):
                raise RuntimeError(
                    "incompatible pre-Round-4 database: cancellation constraints require migration; "
                    "back up the database and workspace, then use a separate fresh database and "
                    "workspace or an explicitly reviewed migration. No automatic upgrade is supported."
                )
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("database is newer than this application; restore a matching backup")
            connection.executescript("BEGIN IMMEDIATE;\n" + schema + "\nPRAGMA user_version = 1;\nCOMMIT;")
            now = utc_now()
            connection.execute(
                """
                INSERT INTO workspaces(id, current_revision, created_at, updated_at)
                VALUES ('default', 0, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now, now),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        patch_id: str,
        patch_revision: int,
        candidate_dir: str,
        patch: dict[str, Any],
        task: str | None = None,
        workspace_id: str = "default",
        context: dict | None = None,
    ) -> sqlite3.Row:
        now = utc_now()
        try:
            with self.transaction() as connection:
                workspace = connection.execute(
                    "SELECT current_revision FROM workspaces WHERE id = ?", (workspace_id,)
                ).fetchone()
                if workspace is None:
                    raise RuntimeError("default workspace is not initialized")
                base_revision = int(workspace["current_revision"])
                connection.execute(
                    """
                    INSERT INTO runs(
                        id, thread_id, workspace_id, base_workspace_revision,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)
                    """,
                    (run_id, thread_id, workspace_id, base_revision, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO patches(
                        id, run_id, patch_revision, base_workspace_revision,
                        candidate_dir, patch_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        patch_id,
                        run_id,
                        patch_revision,
                        base_revision,
                        candidate_dir,
                        json.dumps(patch, sort_keys=True),
                        now,
                    ),
                )
                if context is not None:
                    connection.execute(
                        "INSERT INTO run_context(run_id, request_id, metadata_json) VALUES (?, ?, ?)",
                        (run_id, context.get("request_id"), json.dumps(context)),
                    )
                self._append_event(
                    connection,
                    run_id=run_id,
                    event_type="run.created",
                    node=None,
                    payload={"thread_id": thread_id, "workspace_revision": base_revision},
                )
                if task is not None:
                    connection.execute(
                        "INSERT INTO run_artifacts VALUES (?, 'task', ?, ?)",
                        (run_id, json.dumps(task), now),
                    )
        except sqlite3.IntegrityError as error:
            if "uq_runs_single_active" in str(error) or "runs.singleton_key" in str(error):
                raise ActiveRunConflict("only one active run is allowed") from error
            raise
        return self.get_run(run_id)

    def ensure_workspace(self, workspace_id: str) -> None:
        with self.transaction() as connection:
            now = utc_now()
            connection.execute(
                "INSERT INTO workspaces VALUES (?, 0, ?, ?) ON CONFLICT(id) DO NOTHING",
                (workspace_id, now, now),
            )

    def context(self, run_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM run_context WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return {}
        return {**json.loads(row["metadata_json"]), "stop_requested": bool(row["stop_requested"])}

    def find_request(self, request_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT run_id FROM run_context WHERE request_id = ?", (request_id,)).fetchone()
        return row[0] if row else None

    def update_context(self, run_id: str, **values) -> None:
        with self.transaction() as connection:
            row = connection.execute("SELECT metadata_json FROM run_context WHERE run_id = ?", (run_id,)).fetchone()
            if row:
                metadata = {**json.loads(row[0]), **values}
                connection.execute("UPDATE run_context SET metadata_json = ? WHERE run_id = ?", (json.dumps(metadata), run_id))

    def request_stop(self, run_id: str) -> bool:
        with self.transaction() as connection:
            run = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            row = connection.execute("SELECT stop_requested FROM run_context WHERE run_id = ?", (run_id,)).fetchone()
            if row and row[0]:
                return False
            if run[0] not in ("CREATED", "RUNNING"):
                raise InvalidRunTransition("stop requires a running task; use approval cancel otherwise")
            if not row:
                connection.execute("INSERT INTO run_context(run_id, metadata_json) VALUES (?, '{}')", (run_id,))
            connection.execute("UPDATE run_context SET stop_requested = 1 WHERE run_id = ?", (run_id,))
            self._append_event(connection, run_id=run_id, event_type="run.stop_requested", node=None, payload={})
            return True

    def execution_error(self, run_id: str, code: str, message: str, *, canceled: bool = False, finalize: bool = True) -> None:
        with self.transaction() as connection:
            row = connection.execute("SELECT metadata_json, stop_requested, error_json FROM run_context WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                return
            phase = json.loads(row[0]).get("phase", "start")
            error = None if canceled else json.dumps({"code": code, "message": message, "phase": phase})
            # Retain the first stage failure, rather than replace it with a generic
            # worker-exit diagnosis. Cleanup failures are actionable and take precedence.
            if row[2] and not canceled and code != "cleanup_pending":
                error = row[2]
            connection.execute("UPDATE run_context SET error_json = ? WHERE run_id = ?", (error, run_id))
            # A cancellation is finalized only by the supervisor after process/container cleanup.
            if not finalize or (row[1] and not canceled):
                return
            target = "CANCELED" if canceled else "FAILED"
            changed = connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ? AND status IN ('CREATED','RUNNING')",
                (target, utc_now(), run_id),
            ).rowcount
            if changed:
                self._append_event(connection, run_id=run_id, event_type="run.canceled" if canceled else "run.failed",
                                   node=phase, payload={} if canceled else {"code": code, "message": message})

    def history(self, limit: int, offset: int) -> dict:
        with self.connect() as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT id FROM runs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (limit + 1, offset),
            )]
        return {"runs": [self.snapshot(id) for id in ids[:limit]],
                "next_offset": offset + limit if len(ids) > limit else None}

    def get_run(self, run_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    def list_runs_with_status(self, status: RunStatus) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM runs WHERE status = ? ORDER BY created_at",
                    (status.value,),
                )
            )

    def active_run_snapshot(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM runs WHERE status IN (?, ?, ?, ?) ORDER BY created_at LIMIT 1",
                tuple(status.value for status in ACTIVE_STATUSES),
            ).fetchone()
        return self.snapshot(row["id"]) if row is not None else None

    def workspace_revision(self, workspace_id: str = "default") -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT current_revision FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return int(row["current_revision"])

    def get_patch(self, run_id: str, patch_revision: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM patches WHERE run_id = ? AND patch_revision = ?",
                (run_id, patch_revision),
            ).fetchone()
        if row is None:
            raise KeyError((run_id, patch_revision))
        return row

    def mark_awaiting_approval(self, run_id: str) -> None:
        with self.transaction() as connection:
            cancellation = connection.execute(
                "SELECT stop_requested FROM run_context WHERE run_id = ?", (run_id,)
            ).fetchone()
            if cancellation and cancellation[0]:
                raise InvalidRunTransition("run stop was requested")
            self.require_approval_evidence(run_id, connection=connection)
            self._transition(
                connection,
                run_id,
                expected={RunStatus.RUNNING},
                target=RunStatus.AWAITING_APPROVAL,
            )
            self._append_event(
                connection,
                run_id=run_id,
                event_type="run.interrupted",
                node="await_approval",
                payload={},
            )

    def current_patch(self, run_id: str) -> sqlite3.Row:
        self.get_run(run_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM patches WHERE run_id = ? ORDER BY patch_revision DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    def save_generated_patch(self, patch: FilePatchSet) -> None:
        patch = FilePatchSet.model_validate(patch.model_dump())
        with self.transaction() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (patch.run_id,)).fetchone()
            if run is None or run["status"] != "RUNNING":
                raise InvalidRunTransition("patch generation requires a running task")
            row = connection.execute(
                "SELECT * FROM patches WHERE run_id = ? ORDER BY patch_revision DESC LIMIT 1",
                (patch.run_id,),
            ).fetchone()
            if (
                row is None or row["patch_revision"] != patch.patch_revision
                or row["base_workspace_revision"] != patch.base_workspace_revision
            ):
                raise RevisionConflict("generated patch identity does not match run")
            if json.loads(row["patch_json"]) != {"files": []}:
                raise InvalidRunTransition("this patch revision is already generated")
            connection.execute(
                "UPDATE patches SET patch_json = ? WHERE id = ?",
                (patch.model_dump_json(), row["id"]),
            )
            self._append_event(
                connection, run_id=patch.run_id, event_type="patch.generated", node="code",
                payload={"patch_revision": patch.patch_revision},
            )

    def save_artifact(self, run_id: str, kind: str, payload: Any) -> None:
        with self.transaction() as connection:
            run = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None or run["status"] != "RUNNING":
                raise InvalidRunTransition("stage output requires a running task")
            connection.execute(
                "INSERT INTO run_artifacts VALUES (?, ?, ?, ?)",
                (run_id, kind, json.dumps(payload), utc_now()),
            )
            self._append_event(
                connection, run_id=run_id, event_type="artifact.saved", node=None,
                payload={"kind": kind},
            )

    def artifacts(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            return {row["kind"]: json.loads(row["payload_json"]) for row in connection.execute(
                "SELECT kind, payload_json FROM run_artifacts WHERE run_id = ?", (run_id,)
            )}

    def append_node_event(self, run_id: str, node: str, event_type: str, patch_revision: int) -> None:
        with self.transaction() as connection:
            self._append_event(
                connection, run_id=run_id, node=node, event_type=event_type,
                payload={"patch_revision": patch_revision},
            )

    def require_approval_evidence(self, run_id: str, *, connection=None) -> None:
        if connection is None:
            with self.connect() as own_connection:
                return self.require_approval_evidence(run_id, connection=own_connection)
        artifacts = {row["kind"]: row["payload_json"] for row in connection.execute(
            "SELECT kind, payload_json FROM run_artifacts WHERE run_id = ?", (run_id,)
        )}
        row = connection.execute(
            "SELECT * FROM patches WHERE run_id = ? ORDER BY patch_revision DESC LIMIT 1", (run_id,)
        ).fetchone()
        try:
            patch = FilePatchSet.model_validate_json(row["patch_json"])
            checks = CheckReport.model_validate_json(artifacts["check_report"])
            review = ReviewReport.model_validate_json(artifacts["review_report"])
            if (
                any(report.run_id != run_id or report.patch_revision != row["patch_revision"]
                    for report in (patch, checks, review))
                or patch.base_workspace_revision != row["base_workspace_revision"]
                or not checks.passed or review.recommendation != "approve"
                or any(finding.severity == "error" for finding in review.findings)
            ):
                raise ValueError("checks or review do not approve the current patch")
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidRunTransition("current patch lacks successful checks and review") from error

    def fail_run_start(self, run_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runs SET status = 'FAILED', updated_at = ?
                WHERE id = ? AND status = 'RUNNING'
                AND NOT EXISTS (SELECT 1 FROM run_context WHERE run_context.run_id = runs.id AND stop_requested = 1)
                """,
                (utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                return False
            self._append_event(
                connection,
                run_id=run_id,
                event_type="run.failed",
                node=None,
                payload={"phase": "start"},
            )
            return True

    def record_decision(
        self,
        *,
        decision_id: str,
        run_id: str,
        patch_revision: int,
        kind: DecisionKind,
        feedback: str | None,
    ) -> tuple[DecisionRecord, bool]:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            if existing is not None:
                record = self._decision_from_row(existing)
                requested = (run_id, patch_revision, kind, feedback)
                persisted = (
                    record.run_id,
                    record.patch_revision,
                    record.kind,
                    record.feedback,
                )
                if requested != persisted:
                    raise IdempotencyConflict(
                        "decision_id already exists with a different request payload"
                    )
                return record, False

            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if RunStatus(run["status"]) is not RunStatus.AWAITING_APPROVAL:
                raise InvalidRunTransition("decisions require AWAITING_APPROVAL status")

            self.require_approval_evidence(run_id, connection=connection)

            patch = connection.execute(
                "SELECT * FROM patches WHERE run_id = ? AND patch_revision = ?",
                (run_id, patch_revision),
            ).fetchone()
            if patch is None:
                raise RevisionConflict("patch revision does not exist for this run")
            latest = connection.execute(
                "SELECT MAX(patch_revision) FROM patches WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            if patch_revision != latest:
                raise RevisionConflict("decision must reference the current patch revision")
            workspace = connection.execute(
                "SELECT current_revision FROM workspaces WHERE id = ?", (run["workspace_id"],)
            ).fetchone()
            if int(patch["base_workspace_revision"]) != int(workspace["current_revision"]):
                raise RevisionConflict("patch is based on a stale workspace revision")

            target = {
                DecisionKind.APPROVE: RunStatus.APPLYING,
                DecisionKind.REJECT: RunStatus.REJECTED,
                DecisionKind.CANCEL: RunStatus.CANCELED,
            }[kind]
            now = utc_now()
            connection.execute(
                """
                INSERT INTO decisions(
                    decision_id, run_id, patch_id, patch_revision, kind,
                    feedback, result_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    patch["id"],
                    patch_revision,
                    kind.value,
                    feedback,
                    target.value,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = ?, apply_started_at = CASE WHEN ? = 'APPLYING' THEN ? ELSE NULL END,
                    updated_at = ?
                WHERE id = ?
                """,
                (target.value, target.value, now, now, run_id),
            )
            self._append_event(
                connection,
                run_id=run_id,
                event_type="decision.recorded",
                node="await_approval",
                payload={"decision_id": decision_id, "kind": kind.value},
            )
            row = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            return self._decision_from_row(row), True

    def list_unfinished_decisions(self, run_id: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM decisions WHERE run_id = ? AND resume_completed_at IS NULL ORDER BY created_at",
                    (run_id,),
                )
            )

    def fail_applying_run(self, run_id: str, *, reason: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = 'FAILED', updated_at = ? WHERE id = ? AND status = 'APPLYING'",
                (utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                return False
            self._append_event(
                connection,
                run_id=run_id,
                event_type="run.failed",
                node="apply",
                payload={"phase": "apply", "reason": reason},
            )
            return True

    def fail_decision(
        self, decision_id: str, *, reason: str, owner_id: str | None = None
    ) -> bool:
        with self.transaction() as connection:
            decision = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            if decision is None:
                raise KeyError(decision_id)
            if decision["resume_completed_at"] is not None:
                return False
            # Publication is already an effective side effect. Keep the decision
            # retryable so checkpoint/final-status recovery can finish it instead
            # of reporting that the published change failed.
            if connection.execute(
                "SELECT 1 FROM workspace_publications WHERE decision_id = ?",
                (decision_id,),
            ).fetchone() is not None:
                return False
            now = utc_now()
            if owner_id is not None:
                claim = connection.execute(
                    "SELECT owner_id, lease_expires_at FROM decision_resume_claims "
                    "WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                if (
                    claim is None
                    or claim["owner_id"] != owner_id
                    or claim["lease_expires_at"] <= now
                ):
                    return False
            connection.execute(
                "UPDATE decisions SET result_status = 'FAILED', resume_completed_at = ? WHERE decision_id = ? AND resume_completed_at IS NULL",
                (now, decision_id),
            )
            self._transition(
                connection,
                decision["run_id"],
                expected={RunStatus.APPLYING},
                target=RunStatus.FAILED,
            )
            self._append_event(
                connection,
                run_id=decision["run_id"],
                event_type="run.failed",
                node="apply",
                payload={"decision_id": decision_id, "phase": "apply", "reason": reason},
            )
            connection.execute("DELETE FROM decision_resume_claims WHERE decision_id = ?", (decision_id,))
            return True

    def claim_decision_resume(
        self, *, decision_id: str, owner_id: str, lease_seconds: int
    ) -> str:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with self.transaction() as connection:
            decision = connection.execute(
                "SELECT resume_completed_at FROM decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if decision is None:
                raise KeyError(decision_id)
            if decision["resume_completed_at"] is not None:
                return "completed"

            now = utc_now()
            expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
            existing = connection.execute(
                "SELECT owner_id FROM decision_resume_claims WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO decision_resume_claims(
                        decision_id, owner_id, lease_expires_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (decision_id, owner_id, expires_at, now),
                )
                return "acquired"

            cursor = connection.execute(
                """
                UPDATE decision_resume_claims
                SET owner_id = ?, lease_expires_at = ?, updated_at = ?
                WHERE decision_id = ?
                  AND (owner_id = ? OR lease_expires_at <= ?)
                """,
                (owner_id, expires_at, now, decision_id, owner_id, now),
            )
            return "acquired" if cursor.rowcount == 1 else "busy"

    def decision_resume_lease_remaining(self, decision_id: str) -> float:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT lease_expires_at FROM decision_resume_claims WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return 0.0
        try:
            expires_at = datetime.fromisoformat(row["lease_expires_at"])
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, (expires_at - datetime.now(UTC)).total_seconds())

    def renew_decision_resume_claim(
        self, *, decision_id: str, owner_id: str, lease_seconds: int
    ) -> bool:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = utc_now()
        expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE decision_resume_claims
                SET lease_expires_at = ?, updated_at = ?
                WHERE decision_id = ? AND owner_id = ?
                  AND lease_expires_at > ?
                  AND EXISTS (
                      SELECT 1 FROM decisions
                      WHERE decisions.decision_id = decision_resume_claims.decision_id
                        AND resume_completed_at IS NULL
                  )
                """,
                (expires_at, now, decision_id, owner_id, now),
            )
            return cursor.rowcount == 1

    def release_decision_resume_claim(self, *, decision_id: str, owner_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM decision_resume_claims WHERE decision_id = ? AND owner_id = ?",
                (decision_id, owner_id),
            )

    def require_decision_resume_claim(self, *, decision_id: str, owner_id: str) -> None:
        with self.connect() as connection:
            claim = connection.execute(
                "SELECT c.owner_id, c.lease_expires_at FROM decision_resume_claims c "
                "JOIN decisions d ON d.decision_id = c.decision_id "
                "WHERE c.decision_id = ? AND d.resume_completed_at IS NULL",
                (decision_id,),
            ).fetchone()
            if claim is None or claim["owner_id"] != owner_id or claim["lease_expires_at"] <= utc_now():
                raise InvalidRunTransition("decision resume lease expired or is not owned by this caller")

    def publication_for_decision(self, decision_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM workspace_publications WHERE decision_id = ?", (decision_id,)
            ).fetchone()

    def finalize_publication(
        self, *, decision_id: str, run_id: str, patch_revision: int,
        owner_id: str, revision: int, base_revision: int, patch_id: str,
        publish: Callable[[], None] | None = None,
    ) -> int:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT revision FROM workspace_publications WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if existing is not None:
                return int(existing["revision"])
            decision = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ? AND run_id = ?",
                (decision_id, run_id),
            ).fetchone()
            if decision is None or DecisionKind(decision["kind"]) is not DecisionKind.APPROVE:
                raise InvalidRunTransition("only an approval can publish a workspace revision")
            claim = connection.execute(
                "SELECT owner_id, lease_expires_at FROM decision_resume_claims WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if claim is None or claim["owner_id"] != owner_id or claim["lease_expires_at"] <= utc_now():
                raise InvalidRunTransition("decision resume lease expired")
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            workspace = connection.execute(
                "SELECT current_revision FROM workspaces WHERE id = ?", (run["workspace_id"],)
            ).fetchone()
            if int(workspace["current_revision"]) != base_revision:
                raise RevisionConflict("workspace head moved during publication")
            if int(decision["patch_revision"]) != patch_revision:
                raise RevisionConflict("decision patch revision does not match publication")
            now = utc_now()
            connection.execute(
                "INSERT INTO workspace_publications(decision_id, workspace_id, revision, base_revision, patch_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (decision_id, run["workspace_id"], revision, base_revision, patch_id, now),
            )
            connection.execute(
                "UPDATE workspaces SET current_revision = ?, updated_at = ? WHERE id = ?",
                (revision, now, run["workspace_id"]),
            )
            self._append_event(connection, run_id=run_id, event_type="workspace.published", node="apply", payload={"decision_id": decision_id, "revision": revision})
            if publish is not None:
                publish()
            return revision

    def finish_decision(
        self, decision_id: str, final_status: RunStatus, *, owner_id: str
    ) -> bool:
        if final_status not in {RunStatus.COMPLETE, RunStatus.REJECTED, RunStatus.CANCELED}:
            raise ValueError("a decision can only finish in COMPLETE, REJECTED, or CANCELED")
        with self.transaction() as connection:
            decision = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            if decision is None:
                raise KeyError(decision_id)
            expected_final = {
                DecisionKind.APPROVE: RunStatus.COMPLETE,
                DecisionKind.REJECT: RunStatus.REJECTED,
                DecisionKind.CANCEL: RunStatus.CANCELED,
            }[DecisionKind(decision["kind"])]
            if final_status is not expected_final:
                raise InvalidRunTransition(
                    f"{decision['kind']} decision cannot finish in {final_status.value}"
                )
            if decision["resume_completed_at"] is not None:
                if RunStatus(decision["result_status"]) is final_status:
                    return False
                raise InvalidRunTransition("decision already finished with a different status")
            now = utc_now()
            claim = connection.execute(
                """
                SELECT owner_id, lease_expires_at
                FROM decision_resume_claims WHERE decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
            if claim is None or claim["owner_id"] != owner_id:
                raise InvalidRunTransition("decision resume is not owned by this caller")
            if claim["lease_expires_at"] <= now:
                raise InvalidRunTransition("decision resume lease expired")

            cursor = connection.execute(
                """
                UPDATE decisions
                SET result_status = ?, resume_completed_at = ?
                WHERE decision_id = ? AND resume_completed_at IS NULL
                """,
                (final_status.value, now, decision_id),
            )
            if cursor.rowcount != 1:
                raise InvalidRunTransition("decision was already finished")
            expected_run_status = {
                DecisionKind.APPROVE: RunStatus.APPLYING,
                DecisionKind.REJECT: RunStatus.REJECTED,
                DecisionKind.CANCEL: RunStatus.CANCELED,
            }[DecisionKind(decision["kind"])]
            self._transition(
                connection,
                decision["run_id"],
                expected={expected_run_status},
                target=final_status,
            )
            self._append_event(
                connection,
                run_id=decision["run_id"],
                event_type=("run.completed" if final_status is RunStatus.COMPLETE
                            else "run.rejected" if final_status is RunStatus.REJECTED
                            else "run.canceled"),
                node="apply" if final_status is RunStatus.COMPLETE else "await_approval",
                payload={"decision_id": decision_id},
            )
            connection.execute(
                "DELETE FROM decision_resume_claims WHERE decision_id = ? AND owner_id = ?",
                (decision_id, owner_id),
            )
            return True

    def cancel_run(self, run_id: str) -> None:
        with self.transaction() as connection:
            row = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            status = RunStatus(row["status"])
            if status not in {
                RunStatus.CREATED,
                RunStatus.RUNNING,
                RunStatus.AWAITING_APPROVAL,
            }:
                raise InvalidRunTransition("cancel is only allowed before apply starts")
            self._transition(connection, run_id, expected={status}, target=RunStatus.CANCELED)
            self._append_event(
                connection,
                run_id=run_id,
                event_type="run.cancelled",
                node=None,
                payload={},
            )

    def save_checkpoint_ref(
        self, *, run_id: str, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO run_checkpoint_refs(
                    run_id, thread_id, checkpoint_ns, checkpoint_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    checkpoint_ns = excluded.checkpoint_ns,
                    checkpoint_id = excluded.checkpoint_id,
                    updated_at = excluded.updated_at
                """,
                (run_id, thread_id, checkpoint_ns, checkpoint_id, utc_now()),
            )

    def snapshot(self, run_id: str) -> dict[str, Any]:
        # Read fields and watermark in one transaction; a connection context
        # alone neither starts an autocommit-mode read transaction nor closes it.
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            patch = connection.execute(
                "SELECT patch_revision FROM patches WHERE run_id = ? "
                "ORDER BY patch_revision DESC LIMIT 1", (run_id,),
            ).fetchone()
            revision = connection.execute(
                "SELECT current_revision FROM workspaces WHERE id = ?", (run["workspace_id"],),
            ).fetchone()[0]
            artifacts = {row["kind"]: json.loads(row["payload_json"]) for row in connection.execute(
                "SELECT kind, payload_json FROM run_artifacts WHERE run_id = ?", (run_id,),
            )}
            seqs = [row[0] for row in connection.execute(
                "SELECT seq FROM run_events WHERE run_id = ? ORDER BY seq", (run_id,),
            )]
            pending = connection.execute(
                "SELECT decision_id, kind, patch_revision, feedback FROM decisions "
                "WHERE run_id = ? AND resume_completed_at IS NULL ORDER BY created_at LIMIT 1",
                (run_id,),
            ).fetchone()
            completed = connection.execute(
                "SELECT decision_id, kind, patch_revision, feedback FROM decisions "
                "WHERE run_id = ? AND resume_completed_at IS NOT NULL "
                "ORDER BY created_at DESC, decision_id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            pending_decision = (
                {
                    "decision_id": pending["decision_id"],
                    "kind": pending["kind"],
                    "patch_revision": pending["patch_revision"],
                    "feedback": pending["feedback"],
                }
                if pending is not None
                else None
            )
            context = connection.execute("SELECT * FROM run_context WHERE run_id = ?", (run_id,)).fetchone()
            extra = {}
            if context:
                extra = json.loads(context["metadata_json"])
                extra.pop("request_id", None)
                extra["stop_requested"] = bool(context["stop_requested"])
                extra["error"] = json.loads(context["error_json"]) if context["error_json"] else None
            return {
                **extra,
                "run_id": run["id"], "thread_id": run["thread_id"],
                "workspace_revision": revision, "status": run["status"],
                "base_workspace_revision": run["base_workspace_revision"],
                "patch_revision": patch["patch_revision"], **artifacts,
                "pending_decision": pending_decision,
                "last_decision": dict(completed) if completed is not None else None,
                "event_seqs": seqs, "latest_seq": seqs[-1] if seqs else 0,
            }

    def list_events(self, run_id: str, after_seq: int = 0) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                    (run_id, after_seq),
                )
            )

    def _transition(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        expected: set[RunStatus],
        target: RunStatus,
    ) -> None:
        placeholders = ",".join("?" for _ in expected)
        values = [target.value, utc_now(), run_id, *(status.value for status in expected)]
        cursor = connection.execute(
            f"UPDATE runs SET status = ?, updated_at = ? "
            f"WHERE id = ? AND status IN ({placeholders})",
            values,
        )
        if cursor.rowcount != 1:
            raise InvalidRunTransition(
                f"run {run_id} cannot transition to {target.value} from its current status"
            )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        node: str | None,
        payload: dict[str, Any],
    ) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        seq = int(row["next_seq"])
        connection.execute(
            """
            INSERT INTO run_events(run_id, seq, type, node, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, seq, event_type, node, json.dumps(payload, sort_keys=True), utc_now()),
        )
        return seq

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> DecisionRecord:
        return DecisionRecord(
            decision_id=row["decision_id"],
            run_id=row["run_id"],
            patch_id=row["patch_id"],
            patch_revision=int(row["patch_revision"]),
            kind=DecisionKind(row["kind"]),
            feedback=row["feedback"],
            result_status=RunStatus(row["result_status"]),
            resume_completed_at=row["resume_completed_at"],
        )
