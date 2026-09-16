"""CLI for strict level-0 control validation and status inspection."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from webagents.capture.archive import canonical_json_bytes
from webagents.control.config import ControlConfigurationError, ControlSpecError
from webagents.control.harness import HarnessError
from webagents.control.schemas import ControlValidationReport
from webagents.control.validator import validate_level0_control


class _ControlArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _ControlArgumentParser(prog="webagent-control")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="run the complete level-0 admission matrix")
    validate.add_argument("--spec", type=Path, default=Path("benchmarks/control/control.yaml"))
    validate.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    validate.add_argument("--output-root", type=Path, default=Path("artifacts/control"))
    validate.add_argument("--validation-id")
    status = commands.add_parser("status", help="read one validation report")
    status.add_argument("validation_dir", type=Path)
    return parser


def _validation_exit(report: ControlValidationReport) -> int:
    if report.verdict == "pass":
        return 0
    if report.verdict == "task_failure":
        return 1
    return 2 if "INFRASTRUCTURE_FAILURE" in report.blocking_classifications else 3


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "status":
        try:
            report = ControlValidationReport.model_validate_json((args.validation_dir / "report.json").read_bytes())
        except Exception as exc:
            print(canonical_json_bytes({"error": "invalid_report", "detail": str(exc)}).decode())
            return 3
        print(canonical_json_bytes(report.model_dump(mode="json")).decode())
        return _validation_exit(report)
    try:
        report = asyncio.run(
            validate_level0_control(
                args.spec,
                archive_root=args.archive_root,
                output_root=args.output_root,
                validation_id=args.validation_id,
            )
        )
    except (ControlSpecError, ValidationError) as exc:
        print(json.dumps({"error": "invalid_control_spec", "detail": str(exc)}, sort_keys=True))
        return 64
    except (ControlConfigurationError, HarnessError, ValueError) as exc:
        print(json.dumps({"error": "control_configuration", "detail": str(exc)}, sort_keys=True))
        return 3
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted"}, sort_keys=True), file=sys.stdout)
        return 3
    print(canonical_json_bytes(report.model_dump(mode="json")).decode())
    return _validation_exit(report)


if __name__ == "__main__":
    raise SystemExit(main())
