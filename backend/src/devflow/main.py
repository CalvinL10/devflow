from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
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
    StartRunRequest,
)

logger = logging.getLogger("devflow.api")


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit: str = Field(min_length=1, max_length=64)
    dependency_source: str | None = None
    extras: list[str] = Field(default_factory=list, max_length=20)


class ProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=4096)
    allow_local_http: bool = False


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("devflow").setLevel(logging.INFO)


def create_app(
    database_path: Path | str | None = None, *, workspace_root: Path | str | None = None,
    provider=None, runner=None, asynchronous=True, mode=None, project_path=None,
    settings_root=None, security_enabled=None,
) -> FastAPI:
    configure_logging()
    resolved_database = Path(
        database_path or os.environ.get("DEVFLOW_DATABASE_PATH", "/var/lib/devflow/devflow.sqlite")
    )

    mode = mode or os.environ.get("DEVFLOW_LLM_PROVIDER", "chat_completions")
    if mode not in {"mock", "chat_completions"}:
        raise ValueError("unsupported provider mode")
    settings_root = Path(settings_root or os.environ.get("DEVFLOW_PROVIDER_SETTINGS", resolved_database.parent / "secrets"))
    project_path = project_path or os.environ.get("DEVFLOW_PROJECT_PATH")
    if security_enabled is None:
        security_enabled = os.environ.get("DEVFLOW_SECURITY_ENABLED", "1") == "1"
    public_origin = os.environ.get("DEVFLOW_PUBLIC_ORIGIN", "http://127.0.0.1:3000")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            _app.state.coordinator = RunCoordinator(
                resolved_database, workspace_root, provider=provider, runner=runner, recover=not asynchronous
            )
            _app.state.events = EventStream(_app.state.coordinator.database)
            from devflow.project_import import ProjectImporter
            from devflow.provider import ProviderSettingsStore
            from devflow.supervisor import RunSupervisor
            _app.state.settings = ProviderSettingsStore(settings_root)
            _app.state.importer = ProjectImporter(Path(project_path), resolved_database.parent / "imports") if project_path else None
            _app.state.supervisor = RunSupervisor(
                _app.state.coordinator, settings_root, mode, _app.state.importer, provider, runner,
            )
            if asynchronous:
                _app.state.supervisor.recover()
        except Exception:
            logger.exception("database initialization failed path=%s", resolved_database)
            raise
        logger.info("database initialized path=%s", resolved_database)
        yield
        _app.state.supervisor.close()

    app = FastAPI(title="DevFlow", version="0.2.0-beta.1", lifespan=lifespan)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if security_enabled:
            allowed_hosts = {"127.0.0.1", "localhost", "backend", urlsplit(public_origin).hostname}
            if request.url.hostname not in allowed_hosts:
                return JSONResponse(status_code=403, content={"error": {"code": "host_denied", "message": "Untrusted host"}})
            origin = request.headers.get("origin")
            if origin is not None and origin != public_origin:
                return JSONResponse(status_code=403, content={"error": {"code": "origin_denied", "message": "Untrusted origin"}})
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("x-devflow-request") != "1":
                    return JSONResponse(status_code=403, content={"error": {"code": "csrf_denied", "message": "Missing local request header"}})
                if request.headers.get("sec-fetch-site") == "cross-site":
                    return JSONResponse(status_code=403, content={"error": {"code": "csrf_denied", "message": "Cross-site request denied"}})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

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

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, error: RequestValidationError):
        # Pydantic's default response includes raw input (possibly an API Key).
        return JSONResponse(status_code=422, content={"error": {
            "code": "invalid_request", "message": "Request fields failed validation",
            "fields": [list(item["loc"]) for item in error.errors()],
        }})

    @app.exception_handler(DevFlowError)
    async def handle_domain_error(_request: Request, error: DevFlowError) -> JSONResponse:
        logger.warning("domain conflict code=%s", error.code)
        payload = ErrorResponse(
            error=ErrorDetail(code=error.code, message=error.public_message)
        )
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        logger.error("unhandled request error type=%s", type(error).__name__)
        payload = ErrorResponse(
            error=ErrorDetail(code="internal_error", message="internal server error")
        )
        return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(provider=mode)

    def safe_call(operation):
        try:
            return operation()
        except OSError:
            raise HTTPException(status_code=422, detail="Local storage or Git operation failed.") from None
        except ValueError as error:
            # Known boundary errors must not include raw remote bodies, keys or code.
            raise HTTPException(status_code=422, detail=str(error)[:500]) from None

    @app.get("/api/settings/provider")
    def settings(request: Request):
        return request.app.state.settings.get()

    @app.put("/api/settings/provider")
    def save_settings(body: ProviderRequest, request: Request):
        supervisor = request.app.state.supervisor
        with supervisor.lock:
            supervisor.require_idle()
            safe_call(lambda: request.app.state.settings.save(**body.model_dump()))
            return request.app.state.settings.get()

    @app.delete("/api/settings/provider")
    def clear_settings(request: Request):
        with request.app.state.supervisor.lock:
            request.app.state.supervisor.require_idle()
            request.app.state.settings.clear()
            return request.app.state.settings.get()

    @app.post("/api/settings/provider/test")
    def test_settings(request: Request):
        with request.app.state.supervisor.lock:
            request.app.state.supervisor.require_idle()
            return safe_call(lambda: request.app.state.settings.provider().test())

    @app.get("/api/project/preview")
    def project_preview(request: Request):
        importer = request.app.state.importer
        if importer is None:
            raise HTTPException(409, "Start DevFlow with a local repository path.")
        return safe_call(importer.preview)

    @app.post("/api/imports", status_code=201)
    def import_project(body: ImportRequest, request: Request):
        with request.app.state.supervisor.lock:
            request.app.state.supervisor.require_idle()
            importer = request.app.state.importer
            if importer is None:
                raise HTTPException(409, "No repository is mounted.")
            return safe_call(lambda: importer.create(body.commit, body.dependency_source, body.extras))

    if asynchronous:
        @app.post("/api/runs", status_code=202)
        def create_run(body: StartRunRequest, request: Request):
            return safe_call(lambda: request.app.state.supervisor.submit(body.task, body.import_id, body.request_id))
    else:
        # Explicit legacy/in-process harness only; production always uses supervision.
        @app.post("/api/runs", status_code=201)
        def create_demo_run(body: CreateRunRequest, request: Request):
            return request.app.state.coordinator.start(
                run_id=f"run-{uuid.uuid4().hex}", patch_id=f"patch-{uuid.uuid4().hex}", task=body.task,
            )

    @app.get("/api/runs")
    def history(request: Request, limit: int = 20, offset: int = 0):
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "Invalid pagination")
        return request.app.state.coordinator.database.history(limit, offset)

    @app.post("/api/runs/{run_id}/stop")
    def stop(run_id: str, request: Request):
        try:
            return request.app.state.supervisor.stop(run_id)
        except KeyError:
            raise HTTPException(404, "run not found") from None

    @app.get("/api/runs/{run_id}/patch/download")
    def download_patch(run_id: str, request: Request):
        from devflow.patch_export import export_patch
        coordinator = request.app.state.coordinator
        try:
            run = coordinator.snapshot(run_id)
        except KeyError:
            raise HTTPException(404, "run not found") from None
        if run["status"] != "COMPLETE" or not run.get("import_id"):
            raise HTTPException(409, "An approved imported task is required.")
        importer = request.app.state.importer
        if importer is None:
            from devflow.project_import import ProjectImporter
            importer = ProjectImporter(Path("/project"), resolved_database.parent / "imports")
        metadata = importer.load(run["import_id"])
        patch = FilePatchSet.model_validate_json(coordinator.database.current_patch(run_id)["patch_json"])
        data = export_patch(importer.files(run["import_id"]), patch, metadata.get("modes", {}))
        return Response(data, media_type="text/x-diff", headers={
            "Content-Disposition": f'attachment; filename="{run_id}.patch"',
            "X-DevFlow-Source-Commit": metadata["commit"],
        })

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
