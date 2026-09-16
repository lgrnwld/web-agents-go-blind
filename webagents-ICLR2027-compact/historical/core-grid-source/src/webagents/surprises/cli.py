"""Machine-readable command-line interface for framework-surprise evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from webagents.capture.archive import canonical_json_bytes
from webagents.surprises.log import (
    SurpriseLogError,
    add_sweep_candidate,
    adjudicate_surprise,
    correct_surprise,
    verify_surprise_log,
)
from webagents.surprises.render import render_surprise_views

DEFAULT_ROOT = Path("evidence/framework-surprises")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="webagent-surprises")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="promote one verified sweep candidate into the canonical log")
    add.add_argument("--expectation", required=True)
    add.add_argument("--sweep-dir", type=Path, required=True)
    add.add_argument("--run-dir", type=Path, required=True)
    add.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    add.add_argument("--summary", required=True)
    add.add_argument("--reporter", default="local-researcher")
    add.add_argument("--registry", type=Path, default=DEFAULT_ROOT / "expectations.yaml")
    add.add_argument("--log", type=Path, default=DEFAULT_ROOT / "events.jsonl")

    adjudicate = commands.add_parser("adjudicate", help="append an adjudication linked to a candidate")
    adjudicate.add_argument("candidate_id")
    adjudicate.add_argument(
        "--status",
        required=True,
        choices=(
            "confirmed",
            "not_reproduced",
            "fixture_issue",
            "evidence_issue",
            "model_specific",
            "implementation_change",
            "awaiting_followup",
        ),
    )
    adjudicate.add_argument("--reproduction", type=Path)
    adjudicate.add_argument("--source-sha256")
    adjudicate.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    adjudicate.add_argument("--summary", required=True)
    adjudicate.add_argument("--reporter", default="local-researcher")
    adjudicate.add_argument(
        "--paper-disposition",
        default="pending",
        choices=("pending", "results", "limitations", "results_and_limitations", "not_applicable"),
    )
    adjudicate.add_argument("--registry", type=Path, default=DEFAULT_ROOT / "expectations.yaml")
    adjudicate.add_argument("--log", type=Path, default=DEFAULT_ROOT / "events.jsonl")
    adjudicate.add_argument("--reproductions-dir", type=Path, default=DEFAULT_ROOT / "reproductions")

    correct = commands.add_parser("correct", help="append a correction without rewriting the target event")
    correct.add_argument("event_id")
    correct.add_argument("--summary", required=True)
    correct.add_argument("--reporter", default="local-researcher")
    correct.add_argument(
        "--paper-disposition",
        required=True,
        choices=("pending", "results", "limitations", "results_and_limitations", "not_applicable"),
    )
    correct.add_argument("--registry", type=Path, default=DEFAULT_ROOT / "expectations.yaml")
    correct.add_argument("--log", type=Path, default=DEFAULT_ROOT / "events.jsonl")

    render = commands.add_parser("render", help="regenerate deterministic Markdown and CSV views")
    render.add_argument("--log", type=Path, default=DEFAULT_ROOT / "events.jsonl")
    render.add_argument("--output-dir", type=Path, default=DEFAULT_ROOT / "generated")

    verify = commands.add_parser("verify", help="verify event chaining, identities, links, and evidence hashes")
    verify.add_argument("--log", type=Path, default=DEFAULT_ROOT / "events.jsonl")
    verify.add_argument("--registry", type=Path, default=DEFAULT_ROOT / "expectations.yaml")
    verify.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "add":
            event = add_sweep_candidate(
                expectation_id=args.expectation,
                sweep_dir=args.sweep_dir,
                run_dir=args.run_dir,
                archive_root=args.archive_root,
                registry_path=args.registry,
                log_path=args.log,
                summary=args.summary,
                reporter=args.reporter,
            )
            print(canonical_json_bytes(event.model_dump(mode="json")).decode())
            return 0
        if args.command == "adjudicate":
            event = adjudicate_surprise(
                args.candidate_id,
                status=args.status,
                log_path=args.log,
                registry_path=args.registry,
                archive_root=args.archive_root,
                reproductions_dir=args.reproductions_dir,
                reporter=args.reporter,
                summary=args.summary,
                reproduction_run_dir=args.reproduction,
                paper_disposition=args.paper_disposition,
                source_sha256=args.source_sha256,
            )
            print(canonical_json_bytes(event.model_dump(mode="json")).decode())
            return 0
        if args.command == "correct":
            event = correct_surprise(
                args.event_id,
                log_path=args.log,
                registry_path=args.registry,
                reporter=args.reporter,
                summary=args.summary,
                paper_disposition=args.paper_disposition,
            )
            print(canonical_json_bytes(event.model_dump(mode="json")).decode())
            return 0
        if args.command == "render":
            render_surprise_views(args.log, output_dir=args.output_dir)
            print(json.dumps({"rendered": str(args.output_dir)}, sort_keys=True))
            return 0
        report = verify_surprise_log(
            args.log,
            archive_root=args.archive_root,
            registry_path=args.registry,
        )
        for diagnostic in report.diagnostics:
            print(canonical_json_bytes(diagnostic.model_dump(mode="json")).decode(), file=sys.stderr)
        print(canonical_json_bytes(report.model_dump(mode="json")).decode())
        return 0 if report.valid else 1
    except (SurpriseLogError, ValidationError, ValueError) as exc:
        print(json.dumps({"error": "surprise_log", "detail": str(exc)}, sort_keys=True), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted"}, sort_keys=True), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
