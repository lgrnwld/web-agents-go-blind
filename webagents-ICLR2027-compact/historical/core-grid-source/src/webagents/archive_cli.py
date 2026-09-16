"""Machine-readable CLI for archive integrity verification."""

from __future__ import annotations

import argparse
from pathlib import Path

from webagents.capture.archive import canonical_json_bytes, verify_run
from webagents.schemas import ARCHIVE_SCHEMA_VERSION


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webagent-archive")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="verify one run archive directory")
    verify.add_argument("run_dir", type=Path)
    verify.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="classify a missing completion marker as a warning instead of an error",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    diagnostics = verify_run(args.run_dir, require_complete=not args.allow_incomplete)
    errors = [item for item in diagnostics if item.severity == "error"]
    result = {
        "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
        "valid": not errors,
        "run_dir": str(args.run_dir.resolve()),
        "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
    }
    print(canonical_json_bytes(result).decode("utf-8"))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
