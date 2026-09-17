from __future__ import annotations

import pytest
from pydantic import ValidationError

from devflow.models import RunStatus, WorkflowState


def test_workflow_state_accepts_the_supported_mock_runtime() -> None:
    state = WorkflowState(
        run_id="run-1",
        thread_id="thread-1",
        patch_id="patch-1",
        patch_revision=1,
        base_workspace_revision=0,
    )

    assert state.provider == "mock"
    assert state.status is RunStatus.RUNNING


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("patch_revision", 0),
        ("base_workspace_revision", -1),
    ],
)
def test_workflow_state_rejects_invalid_revisions(field: str, value: int) -> None:
    values = {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "patch_id": "patch-1",
        "patch_revision": 1,
        "base_workspace_revision": 0,
    }
    values[field] = value

    with pytest.raises(ValidationError):
        WorkflowState.model_validate(values)


def test_workflow_state_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        WorkflowState.model_validate(
            {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "patch_id": "patch-1",
                "patch_revision": 1,
                "base_workspace_revision": 0,
                "untrusted_extra": "rejected",
            }
        )
