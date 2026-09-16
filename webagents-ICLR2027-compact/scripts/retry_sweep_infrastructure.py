#!/usr/bin/env python3
"""Recover an immutable full-allocation sweep by retrying only infrastructure gaps.

The source sweep is never modified. A recovery copies its hash-verified evidence,
appends only new attempts for logical trials whose final result is an
INFRASTRUCTURE_FAILURE, recomputes every aggregate, and emits a new receipt only
when the recovered matrix is complete.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webagents.capture.archive import canonical_json_bytes
from webagents.control.receipt import implementation_identity
from webagents.io import append_jsonl, atomic_write, sha256_file
from webagents.runtime import project_root
from webagents.sweeps.aggregate import aggregate_cells, axis_summaries
from webagents.sweeps.config import ResolvedSweepBundle, resolve_sweep_spec
from webagents.sweeps.harness import StructuralFixtureHarness
from webagents.sweeps.matrix import expand_full_matrix
from webagents.sweeps.report import report_markdown
from webagents.sweeps.scheduler import (
    _condition_map,
    _default_runner,
    _execute_once,
    _preflight_credentials,
    _surprise_candidate,
    _task_map,
)
from webagents.sweeps.schemas import (
    ResolvedAccessibilitySweep,
    SweepClassification,
    SweepMatrixRow,
    SweepReceipt,
    SweepReport,
    SweepTrialResult,
)


class RecoveryError(RuntimeError):
    """The source sweep cannot be safely or faithfully recovered."""


def _jsonl(path: Path, model: type[Any]) -> list[Any]:
    values: list[Any] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), start=1):
        try:
            values.append(model.model_validate_json(line))
        except Exception as exc:
            raise RecoveryError(f"invalid {path.name} line {line_number}: {exc}") from exc
    return values


def _canonical_jsonl(values: list[Any]) -> bytes:
    return b"".join(canonical_json_bytes(value.model_dump(mode="json")) + b"\n" for value in values)


def _without_resolved_at(value: ResolvedAccessibilitySweep) -> bytes:
    payload = value.model_dump(mode="json")
    payload.pop("resolved_at", None)
    return canonical_json_bytes(payload)


def _group_attempts(
    results: list[SweepTrialResult],
) -> dict[str, list[SweepTrialResult]]:
    grouped: dict[str, list[SweepTrialResult]] = defaultdict(list)
    for result in results:
        grouped[result.logical_trial_id].append(result)
    for attempts in grouped.values():
        attempts.sort(key=lambda item: (item.infrastructure_attempt, item.finished_at))
    return grouped


def _final_results_and_missing(
    matrix: list[SweepMatrixRow],
    results: list[SweepTrialResult],
) -> tuple[dict[str, SweepTrialResult], list[SweepMatrixRow]]:
    rows = {row.logical_trial_id: row for row in matrix}
    grouped = _group_attempts(results)
    unknown = sorted(set(grouped) - set(rows))
    if unknown:
        raise RecoveryError(f"results contain logical trials absent from the matrix: {unknown}")
    absent = sorted(set(rows) - set(grouped))
    if absent:
        raise RecoveryError(f"source results omit scheduled logical trials entirely: {absent}")

    final = {logical_id: attempts[-1] for logical_id, attempts in grouped.items()}
    missing: list[SweepMatrixRow] = []
    for row in matrix:
        result = final[row.logical_trial_id]
        if result.counts_toward_cell:
            continue
        if result.classification != "INFRASTRUCTURE_FAILURE":
            raise RecoveryError(
                f"{row.logical_trial_id} is blocked by {result.classification}, not retryable infrastructure"
            )
        missing.append(row)
    return final, missing


def _verify_result_archives(
    final_results: dict[str, SweepTrialResult], archive_root: Path
) -> None:
    for result in final_results.values():
        if not result.counts_toward_cell:
            continue
        if result.run_id is None or result.manifest_sha256 is None or result.transcript_sha256 is None:
            raise RecoveryError(f"valid result {result.logical_trial_id} lacks archive identity")
        run_dir = archive_root / "runs" / result.run_id
        manifest_path = run_dir / "manifest.json"
        transcript_path = run_dir / "transcript.json"
        if not manifest_path.is_file() or not transcript_path.is_file():
            raise RecoveryError(f"valid result archive is missing for {result.logical_trial_id}")
        if (
            sha256_file(manifest_path) != result.manifest_sha256
            or sha256_file(transcript_path) != result.transcript_sha256
        ):
            raise RecoveryError(f"valid result archive hash mismatch for {result.logical_trial_id}")


def audit_source(
    source_dir: Path,
    *,
    spec_path: Path,
    control_receipt: Path,
    archive_root: Path,
) -> tuple[
    SweepReport,
    ResolvedSweepBundle,
    list[SweepMatrixRow],
    list[SweepTrialResult],
    dict[str, SweepTrialResult],
    list[SweepMatrixRow],
]:
    source_dir = source_dir.resolve()
    archive_root = archive_root.resolve()
    report_path = source_dir / "report.json"
    report = SweepReport.model_validate_json(report_path.read_bytes())
    if report.verdict != "incomplete":
        raise RecoveryError(f"source verdict must be incomplete, got {report.verdict}")
    if set(report.blocking_classifications) - {"INFRASTRUCTURE_FAILURE"}:
        raise RecoveryError("source has a non-infrastructure blocking classification")
    if (source_dir / "sweep-receipt.json").exists():
        raise RecoveryError("an incomplete source must not have a sweep receipt")

    hashed_files = {
        "resolved-spec.json": report.resolved_spec_sha256,
        "conditions.json": report.conditions_sha256,
        "matrix.jsonl": report.matrix_sha256,
        "results.jsonl": report.results_sha256,
        "exclusions.jsonl": report.exclusions_sha256,
        "reused-controls.jsonl": report.reused_controls_sha256,
    }
    for name, expected in hashed_files.items():
        path = source_dir / name
        if not path.is_file() or sha256_file(path) != expected:
            raise RecoveryError(f"source {name} is missing or does not match its report hash")

    source_resolved = ResolvedAccessibilitySweep.model_validate_json(
        (source_dir / "resolved-spec.json").read_bytes()
    )
    bundle = resolve_sweep_spec(spec_path, control_receipt_override=control_receipt)
    if _without_resolved_at(source_resolved) != _without_resolved_at(bundle.resolved):
        raise RecoveryError("current admitted sweep identity differs from the source resolved spec")
    if implementation_identity(project_root()) != source_resolved.implementation:
        raise RecoveryError("current hashed runner implementation differs from the source sweep")

    matrix = _jsonl(source_dir / "matrix.jsonl", SweepMatrixRow)
    expected_matrix = expand_full_matrix(bundle.resolved)
    if _canonical_jsonl(matrix) != _canonical_jsonl(expected_matrix):
        raise RecoveryError("source matrix differs from the currently resolved admitted matrix")
    results = _jsonl(source_dir / "results.jsonl", SweepTrialResult)
    final, missing = _final_results_and_missing(matrix, results)
    if len(final) != len(matrix):
        raise RecoveryError("source does not have one final result per matrix row")
    if sum(item.counts_toward_cell for item in final.values()) != report.valid_trial_count:
        raise RecoveryError("source valid-trial count differs from its report")
    if not missing:
        raise RecoveryError("source has no retryable infrastructure gaps")
    _verify_result_archives(final, archive_root)

    aggregates = aggregate_cells(bundle.resolved, list(final.values()), include_reused_controls=True)
    live_only = aggregate_cells(bundle.resolved, list(final.values()), include_reused_controls=False)
    if (
        canonical_json_bytes([item.model_dump(mode="json") for item in aggregates])
        != canonical_json_bytes([item.model_dump(mode="json") for item in report.aggregates])
        or canonical_json_bytes([item.model_dump(mode="json") for item in live_only])
        != canonical_json_bytes([item.model_dump(mode="json") for item in report.live_only_aggregates])
    ):
        raise RecoveryError("source aggregates do not reproduce from its final logical results")
    return report, bundle, matrix, results, final, missing


def recovery_plan(
    source_dir: Path,
    *,
    spec_path: Path,
    control_receipt: Path,
    archive_root: Path,
    output_dir: Path,
    max_recovery_attempts: int,
    retry_delay_seconds: float,
) -> dict[str, Any]:
    report, bundle, _, _, final, missing = audit_source(
        source_dir,
        spec_path=spec_path,
        control_receipt=control_receipt,
        archive_root=archive_root,
    )
    rows = []
    for row in missing:
        prior = final[row.logical_trial_id]
        rows.append(
            {
                "logical_trial_id": row.logical_trial_id,
                "model_id": row.model_id,
                "task_id": row.task_id,
                "task_revision": row.task_revision,
                "condition_id": row.condition_id,
                "repeat": row.repeat,
                "prior_attempt": prior.infrastructure_attempt,
                "next_attempt": prior.infrastructure_attempt + 1,
                "prior_detail": prior.checker_detail,
            }
        )
    return {
        "schema_version": "1.0",
        "recovery_id": output_dir.name,
        "source_sweep_id": report.sweep_id,
        "source_dir": str(source_dir.resolve()),
        "source_report_sha256": sha256_file(source_dir.resolve() / "report.json"),
        "source_results_sha256": report.results_sha256,
        "control_receipt_sha256": bundle.resolved.control_receipt_sha256,
        "implementation": bundle.resolved.implementation.model_dump(mode="json"),
        "eligible_trial_count": len(rows),
        "max_recovery_attempts_per_trial": max_recovery_attempts,
        "retry_delay_seconds": retry_delay_seconds,
        "rows": rows,
        "launchable": True,
    }


async def recover(
    source_dir: Path,
    *,
    spec_path: Path,
    control_receipt: Path,
    archive_root: Path,
    output_dir: Path,
    max_recovery_attempts: int,
    retry_delay_seconds: float,
    inter_trial_delay_seconds: float,
) -> SweepReport:
    source_dir = source_dir.resolve()
    archive_root = archive_root.resolve()
    output_dir = output_dir.resolve()
    report, bundle, matrix, source_results, final, missing = audit_source(
        source_dir,
        spec_path=spec_path,
        control_receipt=control_receipt,
        archive_root=archive_root,
    )
    _preflight_credentials(bundle.resolved)
    try:
        output_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise RecoveryError(f"recovery output already exists: {output_dir}") from exc

    plan = recovery_plan(
        source_dir,
        spec_path=spec_path,
        control_receipt=control_receipt,
        archive_root=archive_root,
        output_dir=output_dir,
        max_recovery_attempts=max_recovery_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )
    atomic_write(output_dir / "recovery-plan.json", canonical_json_bytes(plan))
    atomic_write(output_dir / "source-report.json", (source_dir / "report.json").read_bytes())
    source_evidence_names = (
        "resolved-spec.json",
        "conditions.json",
        "matrix.jsonl",
        "results.jsonl",
        "exclusions.jsonl",
        "reused-controls.jsonl",
    )
    for name in source_evidence_names:
        atomic_write(output_dir / name, (source_dir / name).read_bytes())
    atomic_write(output_dir / "surprise-candidates.jsonl", b"")

    all_results = list(source_results)
    new_attempts: list[SweepTrialResult] = []
    task_by_id = _task_map(bundle.resolved)
    condition_by_id = _condition_map(bundle.resolved)
    runner = _default_runner(bundle.resolved.spec.runner)
    recovery_started = datetime.now(UTC)

    with StructuralFixtureHarness(
        bundle.resolved.spec.harness,
        bundle.resolved.tasks,
        bundle.resolved.spec.conditions,
    ) as harness:
        harness.preflight()
        for row_index, row in enumerate(missing):
            prior_attempt = final[row.logical_trial_id].infrastructure_attempt
            recovered: SweepTrialResult | None = None
            for offset in range(1, max_recovery_attempts + 1):
                result = await _execute_once(
                    resolved=bundle.resolved,
                    row=row,
                    task=task_by_id[(row.task_id, row.task_revision)],
                    condition=condition_by_id[row.condition_id],
                    archive_root=archive_root,
                    harness=harness,
                    runner=runner,
                    attempt=prior_attempt + offset,
                )
                all_results.append(result)
                new_attempts.append(result)
                append_jsonl(output_dir / "results.jsonl", result.model_dump(mode="json"))
                if not result.counts_toward_cell:
                    append_jsonl(output_dir / "exclusions.jsonl", result.model_dump(mode="json"))
                recovered = result
                if result.counts_toward_cell:
                    break
                if result.classification != "INFRASTRUCTURE_FAILURE":
                    break
                if offset < max_recovery_attempts and retry_delay_seconds:
                    await asyncio.sleep(retry_delay_seconds)
            assert recovered is not None
            final[row.logical_trial_id] = recovered
            if row_index + 1 < len(missing) and inter_trial_delay_seconds:
                await asyncio.sleep(inter_trial_delay_seconds)

    final_values = [final[row.logical_trial_id] for row in matrix]
    blocking: set[SweepClassification] = {
        item.classification for item in final_values if not item.counts_toward_cell
    }
    aggregates = aggregate_cells(bundle.resolved, final_values, include_reused_controls=True)
    live_only = aggregate_cells(bundle.resolved, final_values, include_reused_controls=False)
    summaries = axis_summaries(bundle.resolved, aggregates)
    complete_cells = sum(item.complete for item in aggregates)
    required_cells = len(aggregates)
    if complete_cells == required_cells and not blocking:
        verdict = "complete"
    elif blocking - {"INFRASTRUCTURE_FAILURE"}:
        verdict = "blocked"
    else:
        verdict = "incomplete"

    surprise_count = 0
    for row in matrix:
        candidate = _surprise_candidate(bundle.resolved, row, final[row.logical_trial_id])
        if candidate is not None:
            candidate = candidate.model_copy(update={"sweep_id": output_dir.name})
            append_jsonl(output_dir / "surprise-candidates.jsonl", candidate.model_dump(mode="json"))
            surprise_count += 1
    aggregates_payload = {
        "cells": [item.model_dump(mode="json") for item in aggregates],
        "live_only_cells": [item.model_dump(mode="json") for item in live_only],
        "axis_summaries": [item.model_dump(mode="json") for item in summaries],
        "variance_decomposition_inputs": [
            {
                "agent_class": bundle.resolved.spec.agent_class,
                "task_id": item.task_id,
                "task_revision": item.task_revision,
                "model_id": item.model_id,
                "condition_id": item.condition_id,
                "reachability_rate": item.reachability.rate,
                "success_rate": item.task_success.rate,
                "trials": item.valid_trials,
            }
            for item in aggregates
        ],
    }
    atomic_write(output_dir / "aggregates.json", canonical_json_bytes(aggregates_payload))
    recovered_report = SweepReport(
        sweep_id=output_dir.name,
        verdict=verdict,
        resolved_spec_sha256=sha256_file(output_dir / "resolved-spec.json"),
        conditions_sha256=sha256_file(output_dir / "conditions.json"),
        matrix_sha256=sha256_file(output_dir / "matrix.jsonl"),
        results_sha256=sha256_file(output_dir / "results.jsonl"),
        exclusions_sha256=sha256_file(output_dir / "exclusions.jsonl"),
        reused_controls_sha256=sha256_file(output_dir / "reused-controls.jsonl"),
        required_cells=required_cells,
        complete_cells=complete_cells,
        valid_trial_count=sum(item.counts_toward_cell for item in final_values),
        attempt_count=len(all_results),
        blocking_classifications=sorted(blocking),
        aggregates=aggregates,
        live_only_aggregates=live_only,
        axis_summaries=summaries,
        architecture_variance_status=(
            f"This {bundle.resolved.spec.agent_class}-only sweep emits model-stratified variance inputs. "
            "Architecture-attributable variance is not estimable until an identical-condition second "
            "architecture sweep is joined."
        ),
        surprise_candidate_count=surprise_count,
        started_at=recovery_started,
        finished_at=datetime.now(UTC),
    )
    report_bytes = canonical_json_bytes(recovered_report.model_dump(mode="json"))
    atomic_write(output_dir / "report.json", report_bytes)
    atomic_write(output_dir / "report.md", report_markdown(recovered_report))

    lineage = {
        **plan,
        "launchable": False,
        "recovery_started_at": recovery_started.isoformat().replace("+00:00", "Z"),
        "recovery_finished_at": recovered_report.finished_at.isoformat().replace("+00:00", "Z"),
        "new_attempt_count": len(new_attempts),
        "recovered_valid_trial_count": sum(item.counts_toward_cell for item in new_attempts),
        "verdict": recovered_report.verdict,
        "results_sha256": recovered_report.results_sha256,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
    }
    atomic_write(output_dir / "infrastructure-recovery.json", canonical_json_bytes(lineage))
    if recovered_report.verdict == "complete":
        receipt = SweepReceipt(
            sweep_id=recovered_report.sweep_id,
            resolved_spec_sha256=recovered_report.resolved_spec_sha256,
            conditions_sha256=recovered_report.conditions_sha256,
            matrix_sha256=recovered_report.matrix_sha256,
            results_sha256=recovered_report.results_sha256,
            exclusions_sha256=recovered_report.exclusions_sha256,
            reused_controls_sha256=recovered_report.reused_controls_sha256,
            report_sha256=hashlib.sha256(report_bytes).hexdigest(),
            model_roster=[item.runner_model for item in bundle.resolved.models],
            tasks=[f"{item.revision.task_id}:r{item.revision.revision}" for item in bundle.resolved.tasks],
            condition_ids=[item.id for item in bundle.resolved.spec.conditions],
            completed_at=recovered_report.finished_at,
        )
        atomic_write(output_dir / "sweep-receipt.json", canonical_json_bytes(receipt.model_dump(mode="json")))
    return recovered_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--control-receipt", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-recovery-attempts", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=float, default=30.0)
    parser.add_argument("--inter-trial-delay-seconds", type=float, default=15.0)
    parser.add_argument("--plan", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.max_recovery_attempts < 1:
        print(
            json.dumps(
                {"error": "invalid_recovery", "detail": "max recovery attempts must be positive"},
                sort_keys=True,
            )
        )
        return 64
    if args.retry_delay_seconds < 0 or args.inter_trial_delay_seconds < 0:
        print(json.dumps({"error": "invalid_recovery", "detail": "delays cannot be negative"}, sort_keys=True))
        return 64
    try:
        if args.plan:
            value = recovery_plan(
                args.source_dir,
                spec_path=args.spec,
                control_receipt=args.control_receipt,
                archive_root=args.archive_root,
                output_dir=args.output_dir,
                max_recovery_attempts=args.max_recovery_attempts,
                retry_delay_seconds=args.retry_delay_seconds,
            )
            print(canonical_json_bytes(value).decode())
            return 0
        report = asyncio.run(
            recover(
                args.source_dir,
                spec_path=args.spec,
                control_receipt=args.control_receipt,
                archive_root=args.archive_root,
                output_dir=args.output_dir,
                max_recovery_attempts=args.max_recovery_attempts,
                retry_delay_seconds=args.retry_delay_seconds,
                inter_trial_delay_seconds=args.inter_trial_delay_seconds,
            )
        )
    except (RecoveryError, OSError, ValueError) as exc:
        print(json.dumps({"error": "sweep_recovery_failed", "detail": str(exc)}, sort_keys=True))
        return 3
    print(canonical_json_bytes(report.model_dump(mode="json")).decode())
    return 0 if report.verdict == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
