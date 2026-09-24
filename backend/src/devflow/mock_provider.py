from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from devflow.models import CheckReport, FilePatch, FilePatchSet, ReviewReport, TaskPlan


@dataclass(frozen=True)
class DeterministicMockProvider:
    """The only provider enabled in the round-0 baseline."""

    name: str = "mock"

    def plan(self, task: str) -> TaskPlan:
        return TaskPlan(
            goal=" ".join(task.split()),
            steps=["Generate a deterministic task module", "Add a test", "Check and review candidate"],
            files_to_modify=["devflow_task.py", "test_devflow_task.py"],
            risks=["Mock demonstration only; does not implement arbitrary coding requests"],
        )

    def code(
        self, plan: TaskPlan, originals: Mapping[str, str], *,
        run_id: str, patch_revision: int, base_workspace_revision: int,
    ) -> FilePatchSet:
        # This role receives text and identity only, never a workspace or runner.
        contents = {
            "devflow_task.py": f"def task_goal():\n    return {plan.goal!r}\n",
            "test_devflow_task.py": (
                "from devflow_task import task_goal\n\n\n"
                f"def test_task_goal():\n    assert task_goal() == {plan.goal!r}\n"
            ),
        }
        return FilePatchSet(
            run_id=run_id, patch_revision=patch_revision,
            base_workspace_revision=base_workspace_revision,
            files=[FilePatch(path=path, original=originals.get(path), modified=value)
                   for path, value in contents.items() if originals.get(path) != value],
        )

    def review(self, plan: TaskPlan, patch: FilePatchSet, checks: CheckReport) -> ReviewReport:
        return ReviewReport(
            run_id=patch.run_id, patch_revision=patch.patch_revision,
            summary="Deterministic mock review; human approval still required. " + " ".join(plan.risks),
            findings=[], recommendation="approve" if checks.passed else "reject",
        )

    def proposal(self, task: str) -> dict[str, object]:
        normalized = " ".join(task.split())
        return {
            "goal": normalized,
            "steps": ["inspect", "prepare candidate", "request approval"],
            "files": [],
            "review_recommendation": "approve",
        }
