from pathlib import Path

import pytest
import yaml


def test_frontend_ci_installs_its_own_backend_before_e2e() -> None:
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"
    if not workflow_path.exists():
        pytest.skip("CI workflow is not included in the backend runner image")
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    job = workflow["jobs"]["frontend"]
    steps = job["steps"]
    e2e = next(i for i, step in enumerate(steps) if step.get("run") == "npm run test:e2e")
    preparation = steps[:e2e]
    assert any(step.get("uses", "").startswith("actions/setup-python@")
               and str(step.get("with", {}).get("python-version")) == "3.11"
               for step in preparation), "frontend job must provision Python 3.11 itself"
    assert any(step.get("uses", "").startswith("astral-sh/setup-uv@") for step in preparation)
    assert any(step.get("run") == "uv sync --locked --python 3.11"
               and step.get("working-directory", job["defaults"]["run"]["working-directory"]) == "backend"
               for step in preparation), "E2E needs backend/.venv and installed backend dependencies"


def test_only_frontend_port_is_bound_to_loopback_by_default() -> None:
    compose_path = Path(__file__).parents[2] / "compose.yaml"
    if not compose_path.exists():
        pytest.skip("compose.yaml is not included in the backend runner image")
    compose = compose_path.read_text(encoding="utf-8")

    services = yaml.safe_load(compose)["services"]
    assert not services["backend"].get("ports")
    assert services["frontend"]["ports"] == ["127.0.0.1:3000:3000"]
    assert "devflow-secrets:/var/lib/devflow-secrets" in services["backend"]["volumes"]
    assert all("devflow-secrets" not in str(volume)
               for volume in services["runner-dispatcher"]["volumes"])


def test_compose_dispatcher_owns_docker_socket_not_backend() -> None:
    compose_path = Path(__file__).parents[2] / "compose.yaml"
    if not compose_path.exists():
        pytest.skip("compose.yaml is not included in the backend runner image")
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]

    backend = services["backend"]
    dispatcher = services["runner-dispatcher"]
    assert backend["environment"]["DEVFLOW_RUNNER_SOCKET"] == "/run/devflow/runner.sock"
    assert not any("docker.sock" in volume for volume in backend["volumes"])
    assert any("docker.sock" in volume for volume in dispatcher["volumes"])
    assert dispatcher["network_mode"] == "none"
    assert dispatcher["read_only"] is True
