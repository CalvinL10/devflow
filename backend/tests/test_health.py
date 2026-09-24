import logging
from functools import partial

from fastapi.testclient import TestClient

from devflow.errors import ActiveRunConflict
from devflow.main import create_app as _create_app

create_app = partial(_create_app, asynchronous=False, mode="mock", security_enabled=False)



def test_health_reports_local_baseline(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "devflow.sqlite")) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "runtime": "single-instance-sqlite",
        "provider": "mock",
    }


def test_health_request_is_logged(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="devflow.api")

    with TestClient(create_app(tmp_path / "devflow.sqlite")) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert "request completed method=GET path=/api/health status=200" in caplog.text


def test_domain_errors_use_stable_response_model(tmp_path) -> None:
    app = create_app(tmp_path / "devflow.sqlite")

    @app.get("/test/domain-error")
    def domain_error() -> None:
        raise ActiveRunConflict("internal run identifier must not be public")

    with TestClient(app) as client:
        response = client.get("/test/domain-error")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "active_run_conflict",
            "message": "another run is already active",
        }
    }


def test_unexpected_errors_do_not_expose_internal_details(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="devflow.api")
    app = create_app(tmp_path / "devflow.sqlite")

    @app.get("/test/unexpected-error")
    def unexpected_error() -> None:
        raise RuntimeError("database password must stay private")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test/unexpected-error")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "internal server error",
        }
    }
    assert "request completed method=GET path=/test/unexpected-error status=500" in caplog.text
