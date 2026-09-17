from devflow.candidate_runner import action_command
from devflow.models import CheckReport, CommandResult, FilePatchSet, ReviewReport


class PassingRunner:
    """Explicit test double: no claim that lint or pytest actually ran."""

    def run(self, workspace, run_id, action):
        workspace.require_materialized_candidate(run_id, workspace.candidate_path(run_id))
        return CommandResult(
            passed=True, command=action_command(action), stdout="test double", stderr="",
            duration_ms=0, exit_code=0,
        )


def noop_code(self, plan, originals, **identity):
    """Retain the old no-op approval-resume proof without claiming file publication."""
    return FilePatchSet(**identity, files=[])


def seed_approval_evidence(database, run_id):
    row = database.current_patch(run_id)
    patch = FilePatchSet(
        run_id=run_id, patch_revision=row["patch_revision"],
        base_workspace_revision=row["base_workspace_revision"], files=[],
    )
    database.save_generated_patch(patch)
    results = {action: CommandResult(
        passed=True, command=action_command(action), stdout="test double", stderr="",
        duration_ms=0, exit_code=0,
    ) for action in ("lint", "test")}
    checks = CheckReport(run_id=run_id, patch_revision=patch.patch_revision, passed=True, **results)
    review = ReviewReport(
        run_id=run_id, patch_revision=patch.patch_revision, summary="test double",
        findings=[], recommendation="approve",
    )
    database.save_artifact(run_id, "check_report", checks.model_dump(mode="json"))
    database.save_artifact(run_id, "review_report", review.model_dump(mode="json"))
