"""CLI for the admitted full-allocation accessibility-tree sweep."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from webagents.capture.archive import canonical_json_bytes
from webagents.control.harness import HarnessError
from webagents.sweeps.config import SweepConfigurationError
from webagents.sweeps.dom_scheduler import run_dom_light_sweep
from webagents.sweeps.prospective import plan_prospective_sweep
from webagents.sweeps.rescore import rescore_sweep
from webagents.sweeps.scheduler import run_accessibility_tree_sweep
from webagents.sweeps.schemas import DomLightReport, SweepReport
from webagents.sweeps.tradeoff import compare_access_policies

PRESET_SPECS = {
    "ax": Path("benchmarks/sweeps/accessibility-tree-full.yaml"),
    "dom": Path("benchmarks/sweeps/dom-extraction-light.yaml"),
    "dom-full": Path("benchmarks/sweeps/dom-extraction-full.yaml"),
    "dom-tradeoff": Path("benchmarks/sweeps/dom-tradeoff-restricted.yaml"),
    "ax-tradeoff": Path("benchmarks/sweeps/ax-tradeoff-restricted.yaml"),
    "vision": Path("benchmarks/sweeps/vision-full.yaml"),
    "cdp": Path("benchmarks/sweeps/cdp-frame-traversal-full.yaml"),
}


class _SweepArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SweepArgumentParser(prog="webagent-sweep")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run an admitted full-allocation or DOM matrix declared by --spec")
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--control-receipt", type=Path)
    run.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    run.add_argument("--output-root", type=Path)
    for command, help_text in (
        ("ax", "run the committed accessibility-tree full-allocation sweep"),
        ("dom", "run the committed DOM-extraction light-allocation sweep"),
        ("dom-full", "extend the corrected DOM sweep to N=10 for every admitted model"),
        ("dom-tradeoff", "run the same-origin-only DOM capability/security arm"),
        ("ax-tradeoff", "run the same-origin-only AX capability/security arm"),
        ("vision", "run the screenshot-only full-allocation sweep"),
        ("cdp", "run the CDP frame-traversal full-allocation sweep"),
    ):
        preset = commands.add_parser(command, help=help_text)
        preset.add_argument("--control-receipt", type=Path)
        preset.add_argument("--archive-root", type=Path, default=Path("artifacts"))
        preset.add_argument("--output-root", type=Path)
    status = commands.add_parser("status", help="read one sweep report")
    status.add_argument("sweep_dir", type=Path)
    rescore = commands.add_parser(
        "rescore", help="derive corrected reachability reports from immutable completed archives"
    )
    rescore.add_argument("--source-dir", type=Path, required=True)
    rescore.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    rescore.add_argument("--output-dir", type=Path, required=True)
    compare = commands.add_parser(
        "compare-access", help="compare completed unrestricted and same-origin-only sweep reports"
    )
    compare.add_argument("--unrestricted-dir", type=Path, required=True)
    compare.add_argument("--restricted-dir", type=Path, required=True)
    compare.add_argument("--output-dir", type=Path, required=True)
    prospective = commands.add_parser(
        "plan-prospective", help="freeze a provider-free vision or CDP matrix before spending credits"
    )
    prospective.add_argument("--spec", type=Path, required=True)
    prospective.add_argument("--control-spec", type=Path, default=Path("benchmarks/control/control.yaml"))
    prospective.add_argument("--output-dir", type=Path, required=True)
    return parser


def _latest_control_receipt(root: Path = Path("artifacts/control")) -> Path:
    receipts = list(root.glob("*/control-receipt.json"))
    if not receipts:
        raise SweepConfigurationError(f"no passing control receipt found under {root}")
    return max(receipts, key=lambda path: path.stat().st_mtime_ns)


def _exit_code(report: SweepReport | DomLightReport) -> int:
    if report.verdict == "complete":
        return 0
    return 1 if report.verdict == "incomplete" else 3


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "status":
        try:
            report_bytes = (args.sweep_dir / "report.json").read_bytes()
            try:
                report: SweepReport | DomLightReport = SweepReport.model_validate_json(report_bytes)
            except ValidationError:
                report = DomLightReport.model_validate_json(report_bytes)
        except Exception as exc:
            print(canonical_json_bytes({"error": "invalid_report", "detail": str(exc)}).decode())
            return 3
        print(canonical_json_bytes(report.model_dump(mode="json")).decode())
        return _exit_code(report)
    if args.command == "rescore":
        try:
            report = rescore_sweep(
                args.source_dir,
                archive_root=args.archive_root,
                output_dir=args.output_dir,
            )
        except (SweepConfigurationError, ValidationError, OSError, ValueError) as exc:
            print(json.dumps({"error": "rescore_failed", "detail": str(exc)}, sort_keys=True))
            return 3
        print(canonical_json_bytes(report.model_dump(mode="json")).decode())
        return _exit_code(report)
    if args.command == "compare-access":
        try:
            comparison = compare_access_policies(
                args.unrestricted_dir,
                args.restricted_dir,
                output_dir=args.output_dir,
            )
        except (SweepConfigurationError, ValidationError, OSError, ValueError) as exc:
            print(json.dumps({"error": "tradeoff_comparison_failed", "detail": str(exc)}, sort_keys=True))
            return 3
        print(canonical_json_bytes(comparison).decode())
        return 0
    if args.command == "plan-prospective":
        try:
            prospective_plan = plan_prospective_sweep(
                args.spec,
                control_spec_path=args.control_spec,
                output_dir=args.output_dir,
            )
        except (SweepConfigurationError, ValidationError, OSError, ValueError) as exc:
            print(json.dumps({"error": "prospective_plan_failed", "detail": str(exc)}, sort_keys=True))
            return 3
        print(canonical_json_bytes(prospective_plan).decode())
        return 0
    try:
        spec_path = PRESET_SPECS.get(args.command) or args.spec
        control_receipt = args.control_receipt
        if args.command in PRESET_SPECS and control_receipt is None:
            control_receipt = _latest_control_receipt()
        raw_spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        if not isinstance(raw_spec, dict):
            raise SweepConfigurationError("sweep spec must contain one YAML mapping")
        agent_class = raw_spec.get("agent_class")
        if agent_class in {"accessibility_tree", "vision", "cdp_frame_traversal"}:
            if args.command == "ax-tradeoff":
                default_output = Path("artifacts/tradeoff/ax-restricted")
            elif agent_class == "vision":
                default_output = Path("artifacts/sweeps/vision-full")
            elif agent_class == "cdp_frame_traversal":
                default_output = Path("artifacts/sweeps/cdp-full")
            else:
                default_output = Path("artifacts/sweeps/ax-full")
            output_root = args.output_root or default_output
            report = asyncio.run(
                run_accessibility_tree_sweep(
                    spec_path,
                    archive_root=args.archive_root,
                    output_root=output_root,
                    control_receipt=control_receipt,
                )
            )
        elif agent_class == "dom_extraction":
            if args.command == "dom-full":
                default_output = Path("artifacts/sweeps/dom-full")
            elif args.command == "dom-tradeoff":
                default_output = Path("artifacts/tradeoff/dom-restricted")
            else:
                default_output = Path("artifacts/sweeps/dom-light")
            output_root = args.output_root or default_output
            report = asyncio.run(
                run_dom_light_sweep(
                    spec_path,
                    archive_root=args.archive_root,
                    output_root=output_root,
                    control_receipt=control_receipt,
                )
            )
        else:
            raise SweepConfigurationError(
                "agent_class must be accessibility_tree, vision, cdp_frame_traversal, or dom_extraction"
            )
    except (SweepConfigurationError, ValidationError) as exc:
        print(json.dumps({"error": "invalid_sweep_spec", "detail": str(exc)}, sort_keys=True))
        return 64
    except (HarnessError, ValueError) as exc:
        print(json.dumps({"error": "sweep_configuration", "detail": str(exc)}, sort_keys=True))
        return 3
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted"}, sort_keys=True))
        return 3
    print(canonical_json_bytes(report.model_dump(mode="json")).decode())
    return _exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
