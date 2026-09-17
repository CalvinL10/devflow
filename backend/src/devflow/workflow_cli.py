from __future__ import annotations

import argparse
import json
from pathlib import Path

from devflow.coordinator import RunCoordinator
from devflow.models import DecisionKind


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Exercise the durable DevFlow approval baseline")
    commands = root.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start")
    start.add_argument("--database", type=Path, required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--patch-id", required=True)
    start.add_argument("--candidate-dir")

    decide = commands.add_parser("decide")
    decide.add_argument("--database", type=Path, required=True)
    decide.add_argument("--run-id", required=True)
    decide.add_argument("--patch-revision", type=int, required=True)
    decide.add_argument("--decision-id", required=True)
    decide.add_argument("--kind", type=DecisionKind, choices=list(DecisionKind), required=True)
    decide.add_argument("--feedback")
    return root


def main() -> None:
    args = parser().parse_args()
    coordinator = RunCoordinator(args.database)
    if args.command == "start":
        result = coordinator.start(
            run_id=args.run_id,
            patch_id=args.patch_id,
            candidate_dir=args.candidate_dir,
        )
    else:
        result = coordinator.decide(
            run_id=args.run_id,
            patch_revision=args.patch_revision,
            decision_id=args.decision_id,
            kind=args.kind,
            feedback=args.feedback,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
