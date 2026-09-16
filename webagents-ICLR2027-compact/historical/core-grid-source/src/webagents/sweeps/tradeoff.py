"""Provider-free capability/security access-policy comparison."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webagents.capture.archive import canonical_json_bytes
from webagents.io import atomic_write, sha256_file
from webagents.sweeps.aggregate import wilson_interval
from webagents.sweeps.config import SweepConfigurationError
from webagents.sweeps.schemas import (
    CellAggregate,
    DomLightReceipt,
    DomLightReport,
    RescoreReceipt,
    SweepReceipt,
    SweepReport,
)


def _load_report(directory: Path) -> tuple[SweepReport | DomLightReport, bytes]:
    path = directory / "report.json"
    try:
        data = path.read_bytes()
        try:
            report: SweepReport | DomLightReport = SweepReport.model_validate_json(data)
        except Exception:
            report = DomLightReport.model_validate_json(data)
    except Exception as exc:
        raise SweepConfigurationError(f"cannot load completed sweep report {path}: {exc}") from exc
    if report.verdict != "complete":
        raise SweepConfigurationError(f"tradeoff input is not complete: {path}")
    if sha256_file(directory / "results.jsonl") != report.results_sha256:
        raise SweepConfigurationError(f"tradeoff input result hash mismatch: {directory}")
    report_sha256 = hashlib.sha256(data).hexdigest()
    try:
        if (directory / "rescore-receipt.json").is_file():
            receipt = RescoreReceipt.model_validate_json((directory / "rescore-receipt.json").read_bytes())
            expected_report_sha256 = receipt.corrected_report_sha256
        elif isinstance(report, SweepReport):
            receipt = SweepReceipt.model_validate_json((directory / "sweep-receipt.json").read_bytes())
            expected_report_sha256 = receipt.report_sha256
        else:
            receipt = DomLightReceipt.model_validate_json((directory / "sweep-receipt.json").read_bytes())
            expected_report_sha256 = receipt.report_sha256
    except Exception as exc:
        raise SweepConfigurationError(f"tradeoff input has no valid receipt: {directory}: {exc}") from exc
    if report_sha256 != expected_report_sha256:
        raise SweepConfigurationError(f"tradeoff input report does not match its receipt: {directory}")
    return report, data


def _cells(report: SweepReport | DomLightReport) -> list[CellAggregate]:
    if isinstance(report, SweepReport):
        return report.aggregates
    return [*report.primary_aggregates, *report.additional_aggregates]


def compare_access_policies(
    unrestricted_dir: Path,
    restricted_dir: Path,
    *,
    output_dir: Path,
) -> dict[str, Any]:
    """Compare matched unrestricted and same-origin-only cells without provider calls."""

    unrestricted_dir = unrestricted_dir.resolve()
    restricted_dir = restricted_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise SweepConfigurationError(f"tradeoff output already exists: {output_dir}")
    unrestricted, unrestricted_bytes = _load_report(unrestricted_dir)
    restricted, restricted_bytes = _load_report(restricted_dir)
    if type(unrestricted) is not type(restricted):
        raise SweepConfigurationError("tradeoff reports must describe the same architecture class")

    unrestricted_cells = {
        (item.task_id, item.task_revision, item.model_id, item.condition_id): item
        for item in _cells(unrestricted)
    }
    restricted_cells = {
        (item.task_id, item.task_revision, item.model_id, item.condition_id): item
        for item in _cells(restricted)
    }
    matched_keys = sorted(set(unrestricted_cells) & set(restricted_cells))
    if not matched_keys:
        raise SweepConfigurationError("tradeoff reports have no matched task-model-condition cells")
    restricted_conditions = {key[3] for key in restricted_cells}
    missing = sorted(key for key in restricted_cells if key not in unrestricted_cells)
    if missing:
        raise SweepConfigurationError(f"unrestricted report is missing restricted-arm cells: {missing[:5]}")

    rows: list[dict[str, Any]] = []
    reach_unrestricted = reach_restricted = success_unrestricted = success_restricted = total = 0
    baseline_rows: list[dict[str, Any]] = []
    for key in matched_keys:
        if key[3] not in restricted_conditions:
            continue
        left = unrestricted_cells[key]
        right = restricted_cells[key]
        if not left.complete or not right.complete or left.valid_trials != right.valid_trials:
            raise SweepConfigurationError(f"tradeoff cell is incomplete or unbalanced: {key}")
        n = left.valid_trials
        left_reach = left.reachability.successes
        right_reach = right.reachability.successes
        left_success = left.task_success.successes
        right_success = right.task_success.successes
        row = {
                "task_id": key[0],
                "task_revision": key[1],
                "model_id": key[2],
                "condition_id": key[3],
                "trials_per_policy": n,
                "unrestricted_reachability": left_reach / n,
                "restricted_reachability": right_reach / n,
                "reachability_delta": (left_reach - right_reach) / n,
                "unrestricted_success": left_success / n,
                "restricted_success": right_success / n,
                "success_delta": (left_success - right_success) / n,
            }
        rows.append(row)
        if key[3] == "level-0":
            baseline_rows.append(row)
        else:
            total += n
            reach_unrestricted += left_reach
            reach_restricted += right_reach
            success_unrestricted += left_success
            success_restricted += right_success
    if total == 0:
        raise SweepConfigurationError("tradeoff reports contain no matched non-control structural cells")

    architecture = "accessibility_tree" if isinstance(unrestricted, SweepReport) else "dom_extraction"
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "comparison_id": f"{architecture}-access-policy-v1",
        "architecture_class": architecture,
        "policies": ["unrestricted", "same_origin_only"],
        "matched_cell_count": len(rows),
        "structural_trials_per_policy": total,
        "baseline_cells": baseline_rows,
        "overall": {
            "unrestricted_reachability": wilson_interval(reach_unrestricted, total).model_dump(mode="json"),
            "restricted_reachability": wilson_interval(reach_restricted, total).model_dump(mode="json"),
            "reachability_delta": (reach_unrestricted - reach_restricted) / total,
            "unrestricted_success": wilson_interval(success_unrestricted, total).model_dump(mode="json"),
            "restricted_success": wilson_interval(success_restricted, total).model_dump(mode="json"),
            "success_delta": (success_unrestricted - success_restricted) / total,
        },
        "cells": rows,
        "source_reports": {
            "unrestricted": str(unrestricted_dir / "report.json"),
            "restricted": str(restricted_dir / "report.json"),
            "unrestricted_sha256": hashlib.sha256(unrestricted_bytes).hexdigest(),
            "restricted_sha256": hashlib.sha256(restricted_bytes).hexdigest(),
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    output_dir.mkdir(parents=True)
    report_bytes = canonical_json_bytes(report)
    atomic_write(output_dir / "report.json", report_bytes)
    lines = [
        f"# {architecture} access-policy tradeoff",
        "",
        f"Matched cells: {len(rows)}",
        f"Cross-origin structural trials per policy: {total}",
        f"Reachability delta (unrestricted - restricted): {report['overall']['reachability_delta']:.3f}",
        f"Success delta (unrestricted - restricted): {report['overall']['success_delta']:.3f}",
        "",
        "| Task | Model | Condition | N | Reachability delta | Success delta |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {row['task_id']} r{row['task_revision']} | {row['model_id']} | {row['condition_id']} | "
        f"{row['trials_per_policy']} | {row['reachability_delta']:.3f} | {row['success_delta']:.3f} |"
        for row in rows
    )
    atomic_write(output_dir / "report.md", "\n".join(lines).encode("utf-8"))
    receipt = {
        "schema_version": "1.0",
        "comparison_id": report["comparison_id"],
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "unrestricted_report_sha256": hashlib.sha256(unrestricted_bytes).hexdigest(),
        "restricted_report_sha256": hashlib.sha256(restricted_bytes).hexdigest(),
        "created_at": report["created_at"],
    }
    atomic_write(output_dir / "comparison-receipt.json", canonical_json_bytes(receipt))
    return report
