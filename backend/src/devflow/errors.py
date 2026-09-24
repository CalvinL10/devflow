class DevFlowError(Exception):
    """Base error for expected DevFlow domain conflicts."""

    code = "devflow_error"
    public_message = "the request could not be completed"


class ActiveRunConflict(DevFlowError):
    code = "active_run_conflict"
    public_message = "another run is already active"


class IdempotencyConflict(DevFlowError):
    code = "idempotency_conflict"
    public_message = "the idempotency key conflicts with an existing request"


class InvalidRunTransition(DevFlowError):
    code = "invalid_run_transition"
    public_message = "the run state does not allow this operation"


class RevisionConflict(DevFlowError):
    code = "revision_conflict"
    public_message = "the workspace or patch revision conflicts with this request"


class DependencyPreparationError(DevFlowError):
    code = "dependency_preparation_error"
    public_message = "Supported wheel dependencies could not be prepared; verify public PyPI access and dependency compatibility."
