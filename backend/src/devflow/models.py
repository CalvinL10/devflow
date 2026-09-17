from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_FILE_BYTES = 256 * 1024
MAX_PATCH_FILES = 100


def validate_file_path(value: str) -> str:
    # Portable relative syntax, including rejection of Windows aliases on Linux.
    parts = value.split("/")
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
        *(f"com{i}" for i in "¹²³"),
        *(f"lpt{i}" for i in "¹²³"),
    }
    if (
        len(value) > 240 or len(parts) > 20
        or any(c in value for c in '\\:*?"<>|')
        or any(ord(c) < 32 for c in value)
        or any(not p or p in {".", ".."} or p.endswith((" ", ".")) for p in parts)
        or any(p.split(".")[0].casefold() in reserved for p in parts)
        or any(p.casefold() == ".git" for p in parts)
    ):
        raise ValueError("file path must be a safe relative POSIX path")
    return value


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=10000)
    steps: list[str] = Field(min_length=1)
    files_to_modify: list[str] = Field(max_length=MAX_PATCH_FILES)
    risks: list[str]

    @field_validator("files_to_modify")
    @classmethod
    def valid_paths(cls, paths: list[str]) -> list[str]:
        return [validate_file_path(path) for path in paths]


class FilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    original: str | None
    modified: str | None

    _valid_path = field_validator("path")(validate_file_path)

    @model_validator(mode="after")
    def valid_contents(self):
        if self.original == self.modified:
            raise ValueError("patch must change file contents")
        for value in (self.original, self.modified):
            if value is not None and ("\x00" in value or len(value.encode("utf-8")) > MAX_FILE_BYTES):
                raise ValueError("patch accepts bounded UTF-8 text files only")
        return self


class FilePatchSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1)
    patch_revision: int = Field(ge=1)
    base_workspace_revision: int = Field(ge=0)
    files: list[FilePatch] = Field(max_length=MAX_PATCH_FILES)

    @model_validator(mode="after")
    def unique_paths(self):
        paths = [file.path.casefold() for file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate patch paths")
        if any(a.startswith(b + "/") for a in paths for b in paths if a != b):
            raise ValueError("patch paths cannot contain another patch path")
        return self


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["info", "warning", "error"]
    message: str = Field(min_length=1)
    path: str | None = None


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    patch_revision: int = Field(ge=1)
    summary: str = Field(min_length=1)
    findings: list[ReviewFinding]
    recommendation: Literal["approve", "reject", "revise"]


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=10000)

    @field_validator("task")
    @classmethod
    def nonblank_task(cls, task: str) -> str:
        if not task.strip():
            raise ValueError("task cannot be blank")
        return task.strip()


class RunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPLYING = "APPLYING"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    CANCELLED = "CANCELED"
    FAILED = "FAILED"


class DecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    CANCEL = "cancel"


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    runtime: Literal["single-instance-sqlite"] = "single-instance-sqlite"
    provider: Literal["mock"] = "mock"


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail


class WorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    thread_id: str
    patch_id: str
    patch_revision: int = Field(ge=1)
    base_workspace_revision: int = Field(ge=0)
    provider: Literal["mock"] = "mock"
    decision: DecisionKind | None = None
    status: RunStatus = RunStatus.RUNNING
    task: str = "Demonstrate the deterministic coding workflow"
    plan: TaskPlan | None = None
    patch: FilePatchSet | None = None
    check_report: CheckReport | None = None
    review_report: ReviewReport | None = None


class ApprovalResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1, max_length=128)
    kind: DecisionKind


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1, max_length=128)
    patch_revision: int = Field(ge=1)
    feedback: str | None = Field(default=None, max_length=10000)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1, max_length=128)


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    command: list[str]
    stdout: str
    stderr: str
    duration_ms: int = Field(ge=0)
    exit_code: int | None
    timed_out: bool = False

    @model_validator(mode="after")
    def consistent_result(self):
        if self.passed != (self.exit_code == 0 and not self.timed_out):
            raise ValueError("passed must agree with exit code and timeout")
        return self


class CheckReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    patch_revision: int = Field(ge=1)
    lint: CommandResult
    test: CommandResult
    passed: bool

    @model_validator(mode="after")
    def consistent_result(self):
        if self.passed != (self.lint.passed and self.test.passed):
            raise ValueError("passed must agree with both lint and test results")
        return self


WorkflowState.model_rebuild()
