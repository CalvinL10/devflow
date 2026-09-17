from __future__ import annotations

import os
import sys
from pathlib import Path

CLIENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = CLIENT_ROOT.parent
BACKEND_SRC = REPOSITORY_ROOT / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))

from devflow.candidate_runner import action_command
from devflow.main import create_app
from devflow.models import CommandResult


class PassingRunner:
    """E2E test double; it does not claim that Docker commands actually ran."""

    def run(self, workspace, run_id, action):
        workspace.require_materialized_candidate(run_id, workspace.candidate_path(run_id))
        return CommandResult(
            passed=True,
            command=action_command(action),
            stdout="Playwright runner test double",
            stderr="",
            duration_ms=0,
            exit_code=0,
        )


runtime = Path(os.environ.get("E2E_RUNTIME_DIR", CLIENT_ROOT / ".e2e-runtime")).resolve()
app = create_app(
    runtime / "state.sqlite",
    workspace_root=runtime / "workspace",
    runner=PassingRunner(),
)
