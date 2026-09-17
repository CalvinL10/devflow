import pytest
from support import PassingRunner, noop_code

from devflow.docker_runner import DockerCandidateRunner
from devflow.mock_provider import DeterministicMockProvider


@pytest.fixture
def mock_runner(monkeypatch):
    monkeypatch.setattr(DockerCandidateRunner, "run", PassingRunner.run)


@pytest.fixture
def noop_coder(monkeypatch):
    monkeypatch.setattr(DeterministicMockProvider, "code", noop_code)
