"""CLI dispatch for the four independent architecture runners."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from webagents.ax.runner import _run_ax_agent
from webagents.cdp.runner import _run_cdp_agent
from webagents.dom.runner import DEFAULT_MAX_STEPS, DEFAULT_TIMEOUT_SECONDS, _run_dom_agent
from webagents.vision.runner import _run_vision_agent

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_TIMEOUT = 2
EXIT_INFRASTRUCTURE = 3
EXIT_USAGE = 64


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webagent-run")
    subparsers = parser.add_subparsers(dest="agent_class", required=True)
    for command, help_text in (
        ("dom", "run the serialized-DOM agent"),
        ("ax", "run the raw accessibility-tree agent"),
        ("vision", "run the screenshot-only coordinate agent"),
        ("cdp", "run the explicit CDP DOMSnapshot/frame-traversal agent"),
    ):
        runner = subparsers.add_parser(command, help=help_text)
        runner.add_argument("--url", required=True)
        runner.add_argument("--task", required=True)
        runner.add_argument("--model", required=True)
        runner.add_argument("--run-id")
        runner.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
        runner.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
        runner.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    return parser


def _exit_code(status: str) -> int:
    return {
        "success": EXIT_SUCCESS,
        "failure": EXIT_FAILURE,
        "timeout": EXIT_TIMEOUT,
        "infrastructure_error": EXIT_INFRASTRUCTURE,
    }[status]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")
    args = _parser().parse_args(argv)
    try:
        implementation = {
            "dom": _run_dom_agent,
            "ax": _run_ax_agent,
            "vision": _run_vision_agent,
            "cdp": _run_cdp_agent,
        }[args.agent_class]
        transcript = asyncio.run(
            implementation(
                args.url,
                args.task,
                args.model,
                run_id=args.run_id,
                max_steps=args.max_steps,
                archive_root=args.archive_root,
                timeout_seconds=args.timeout_seconds,
            )
        )
    except (ValidationError, ValueError) as exc:
        print(json.dumps({"error": "invalid_input", "detail": str(exc)}, sort_keys=True))
        return EXIT_USAGE
    print(transcript.model_dump_json())
    return _exit_code(transcript.status)


if __name__ == "__main__":
    raise SystemExit(main())
