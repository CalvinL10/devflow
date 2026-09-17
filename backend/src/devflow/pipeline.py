from __future__ import annotations

from types import MappingProxyType

from devflow.database import Database
from devflow.models import (
    CheckReport,
    FilePatchSet,
    ReviewReport,
    RunStatus,
    TaskPlan,
    WorkflowState,
)
from devflow.staging import StagingFileStore
from devflow.workspace import ManagedWorkspace


class RunPipeline:
    def __init__(self, database: Database, workspace: ManagedWorkspace, provider, runner):
        self.database, self.workspace = database, workspace
        self.provider, self.runner = provider, runner

    def node(self, name: str):
        def execute(state: WorkflowState):
            self.database.append_node_event(state.run_id, name, "node.started", state.patch_revision)
            try:
                result = getattr(self, name)(state)
            except Exception:
                self.database.append_node_event(
                    state.run_id, name, "node.failed", state.patch_revision
                )
                raise
            self.database.append_node_event(
                state.run_id, name,
                "node.failed" if result.get("status") == RunStatus.FAILED else "node.completed",
                state.patch_revision,
            )
            return result
        return execute

    def plan(self, state: WorkflowState):
        plan = TaskPlan.model_validate(self.provider.plan(state.task).model_dump())
        self.database.save_artifact(state.run_id, "plan", plan.model_dump(mode="json"))
        return {"plan": plan.model_dump(mode="json")}

    def code(self, state: WorkflowState):
        originals = self.workspace.read_revision(state.base_workspace_revision)
        patch = FilePatchSet.model_validate(self.provider.code(
            state.plan.model_copy(deep=True), MappingProxyType(originals),
            run_id=state.run_id, patch_revision=state.patch_revision,
            base_workspace_revision=state.base_workspace_revision,
        ).model_dump())
        if (patch.run_id, patch.patch_revision, patch.base_workspace_revision) != (
            state.run_id, state.patch_revision, state.base_workspace_revision
        ):
            raise ValueError("Coder returned a patch for a different run or revision")
        StagingFileStore(originals).stage(patch)
        self.database.save_generated_patch(patch)
        return {"patch": patch.model_dump(mode="json")}

    def materialize(self, state: WorkflowState):
        # Recover the exact staged payload from SQLite, not mutable provider memory.
        row = self.database.get_patch(state.run_id, state.patch_revision)
        patch = FilePatchSet.model_validate_json(row["patch_json"])
        if patch != state.patch:
            raise ValueError("checkpoint patch and stored patch disagree")
        self.workspace.materialize_candidate(
            run_id=state.run_id, base_revision=state.base_workspace_revision, patch=patch
        )
        return {}

    def lint_test(self, state: WorkflowState):
        lint = self.runner.run(self.workspace, state.run_id, "lint")
        test = self.runner.run(self.workspace, state.run_id, "test")
        report = CheckReport(
            run_id=state.run_id, patch_revision=state.patch_revision,
            lint=lint, test=test, passed=lint.passed and test.passed,
        )
        self.database.save_artifact(state.run_id, "check_report", report.model_dump(mode="json"))
        return {
            "check_report": report.model_dump(mode="json"),
            "status": RunStatus.RUNNING if report.passed else RunStatus.FAILED,
        }

    def review(self, state: WorkflowState):
        report = ReviewReport.model_validate(self.provider.review(
            state.plan.model_copy(deep=True), state.patch.model_copy(deep=True),
            state.check_report.model_copy(deep=True),
        ).model_dump())
        if (report.run_id, report.patch_revision) != (state.run_id, state.patch_revision):
            raise ValueError("Reviewer returned a report for a different run or revision")
        self.database.save_artifact(state.run_id, "review_report", report.model_dump(mode="json"))
        passed = report.recommendation == "approve" and not any(
            finding.severity == "error" for finding in report.findings
        )
        return {"review_report": report.model_dump(mode="json"),
                "status": RunStatus.RUNNING if passed else RunStatus.FAILED}
