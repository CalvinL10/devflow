from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from devflow.models import ApprovalResume, DecisionKind, RunStatus, WorkflowState


def _await_approval(state: WorkflowState) -> dict[str, Any]:
    if (
        state.check_report is None or not state.check_report.passed
        or state.review_report is None or state.review_report.recommendation != "approve"
        or any(f.severity == "error" for f in state.review_report.findings)
        or any((r.run_id, r.patch_revision) != (state.run_id, state.patch_revision)
               for r in (state.check_report, state.review_report))
    ):
        raise ValueError("approval requires successful checks and review for this patch")
    resume = ApprovalResume.model_validate(
        interrupt(
            {
                "run_id": state.run_id,
                "patch_id": state.patch_id,
                "patch_revision": state.patch_revision,
                "base_workspace_revision": state.base_workspace_revision,
            }
        )
    )
    return {
        "decision": resume.kind,
        "status": (
            RunStatus.APPLYING if resume.kind is DecisionKind.APPROVE
            else RunStatus.REJECTED if resume.kind is DecisionKind.REJECT
            else RunStatus.CANCELED
        ),
    }


def _route_after_decision(state: WorkflowState) -> str:
    return {DecisionKind.APPROVE: "apply", DecisionKind.REJECT: "rejected", DecisionKind.CANCEL: "cancelled"}[state.decision]


def _apply(state: WorkflowState, publish: Callable[[WorkflowState], None] | None = None) -> dict[str, Any]:
    if publish is not None:
        publish(state)
    return {"status": RunStatus.COMPLETE}


def _rejected(state: WorkflowState) -> dict[str, Any]:
    return {"status": RunStatus.REJECTED}


def build_graph(
    checkpointer: SqliteSaver,
    pipeline=None,
    *,
    resume_guard: Callable[[], None] | None = None,
    publish: Callable[[WorkflowState], None] | None = None,
):
    builder = StateGraph(WorkflowState)
    def unavailable(_state):
        raise RuntimeError("starting the graph requires the run pipeline")

    stages = ["plan", "code", "materialize", "lint_test", "review"]
    for stage in stages:
        builder.add_node(stage, pipeline.node(stage) if pipeline is not None else unavailable)
    builder.add_node("await_approval", _await_approval)
    def guarded_apply(state: WorkflowState) -> dict[str, Any]:
        if resume_guard is not None:
            resume_guard()
        result = _apply(state, publish)
        if resume_guard is not None:
            resume_guard()
        return result

    builder.add_node("apply", guarded_apply)
    builder.add_node("rejected", _rejected)
    builder.add_node("cancelled", lambda _state: {"status": RunStatus.CANCELED})
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "code")
    builder.add_edge("code", "materialize")
    builder.add_edge("materialize", "lint_test")
    builder.add_conditional_edges(
        "lint_test", lambda state: "failed" if state.status == RunStatus.FAILED else "review",
        {"failed": END, "review": "review"},
    )
    builder.add_conditional_edges(
        "review", lambda state: "failed" if state.status == RunStatus.FAILED else "approval",
        {"failed": END, "approval": "await_approval"},
    )
    builder.add_conditional_edges(
        "await_approval",
        _route_after_decision,
        {"apply": "apply", "rejected": "rejected", "cancelled": "cancelled"},
    )
    builder.add_edge("apply", END)
    builder.add_edge("rejected", END)
    builder.add_edge("cancelled", END)
    return builder.compile(checkpointer=checkpointer)


def open_graph(database_path: Path):
    return SqliteSaver.from_conn_string(str(database_path.resolve()))


def invoke_start(graph, state: WorkflowState, thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    return graph.invoke(state.model_dump(mode="json"), config=config)


def invoke_resume(graph, *, thread_id: str, decision_id: str, kind: DecisionKind):
    config = {"configurable": {"thread_id": thread_id}}
    return graph.invoke(
        Command(resume={"decision_id": decision_id, "kind": kind.value}),
        config=config,
    )
