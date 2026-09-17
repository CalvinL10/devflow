from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from devflow.coordinator import RunCoordinator
from devflow.errors import DevFlowError
from devflow.events import (
    CursorError,
    EventStoreError,
    EventStream,
    EventStreamResponse,
    parse_cursor,
)
from devflow.models import (
    CreateRunRequest,
    DecisionKind,
    DecisionRequest,
    ErrorDetail,
    ErrorResponse,
    FilePatchSet,
    HealthResponse,
    ResumeRequest,
)

logger = logging.getLogger("devflow.api")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("devflow").setLevel(logging.INFO)


def create_app(
    database_path: Path | str | None = None, *, workspace_root: Path | str | None = None,
    provider=None, runner=None,
) -> FastAPI:
    configure_logging()
    resolved_database = Path(
        database_path or os.environ.get("DEVFLOW_DATABASE_PATH", "/var/lib/devflow/devflow.sqlite")
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            _app.state.coordinator = RunCoordinator(
                resolved_database, workspace_root, provider=provider, runner=runner
            )
            _app.state.events = EventStream(_app.state.coordinator.database)
        except Exception:
            logger.exception("database initialization failed path=%s", resolved_database)
            raise
        logger.info("database initialized path=%s", resolved_database)
        yield

    app = FastAPI(title="DevFlow", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        started_at = perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = (perf_counter() - started_at) * 1000
            logger.info(
                "request completed method=%s path=%s status=%s duration_ms=%.2f",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
            )

    @app.exception_handler(DevFlowError)
    async def handle_domain_error(_request: Request, error: DevFlowError) -> JSONResponse:
        logger.warning("domain conflict code=%s message=%s", error.code, error)
        payload = ErrorResponse(
            error=ErrorDetail(code=error.code, message=error.public_message)
        )
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        logger.exception("unhandled request error", exc_info=error)
        payload = ErrorResponse(
            error=ErrorDetail(code="internal_error", message="internal server error")
        )
        return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.post("/api/runs", status_code=201)
    def create_run(body: CreateRunRequest, request: Request) -> dict:
        return request.app.state.coordinator.start(
            run_id=f"run-{uuid.uuid4().hex}", patch_id=f"patch-{uuid.uuid4().hex}", task=body.task,
        )

    @app.get("/api/runs/active")
    def get_active_run(request: Request) -> dict | None:
        return request.app.state.coordinator.database.active_run_snapshot()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict:
        try:
            return request.app.state.coordinator.snapshot(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request):
        try:
            # Header wins on reconnect even if the original URL has a cursor.
            raw_cursor = request.headers.get("last-event-id")
            if raw_cursor is None:
                raw_cursor = request.query_params.get("cursor")
            cursor = parse_cursor(raw_cursor)
            stream = request.app.state.events
            page = await run_in_threadpool(stream.read_page, run_id, cursor)
        except (CursorError, EventStoreError) as error:
            return JSONResponse(status_code=error.status, content=error.body)
        return EventStreamResponse(
            stream.frames(run_id, page), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _decide(run_id: str, body: DecisionRequest, request: Request, kind: DecisionKind) -> dict:
        return request.app.state.coordinator.decide(
            run_id=run_id,
            patch_revision=body.patch_revision,
            decision_id=body.decision_id,
            kind=kind,
            feedback=body.feedback,
        )

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, body: DecisionRequest, request: Request) -> dict:
        return _decide(run_id, body, request, DecisionKind.APPROVE)

    @app.post("/api/runs/{run_id}/reject")
    def reject(run_id: str, body: DecisionRequest, request: Request) -> dict:
        return _decide(run_id, body, request, DecisionKind.REJECT)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str, body: DecisionRequest, request: Request) -> dict:
        return _decide(run_id, body, request, DecisionKind.CANCEL)

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str, body: ResumeRequest, request: Request) -> dict:
        database = request.app.state.coordinator.database
        with database.connect() as connection:
            row = connection.execute("SELECT kind, patch_revision, feedback FROM decisions WHERE decision_id = ? AND run_id = ?", (body.decision_id, run_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="decision not found")
        return request.app.state.coordinator.decide(
            run_id=run_id, patch_revision=int(row["patch_revision"]),
            decision_id=body.decision_id, kind=DecisionKind(row["kind"]), feedback=row["feedback"],
        )

    @app.get("/api/runs/{run_id}/patch", response_model=FilePatchSet)
    def get_patch(run_id: str, request: Request) -> FilePatchSet:
        try:
            row = request.app.state.coordinator.database.current_patch(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        try:
            return FilePatchSet.model_validate_json(row["patch_json"])
        except ValueError as error:
            raise HTTPException(status_code=409, detail="patch has not been generated") from error

    return app


app = create_app()
