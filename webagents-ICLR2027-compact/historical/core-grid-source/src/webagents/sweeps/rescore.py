"""Versioned, provider-free rescoring of completed sweep archives."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from webagents.capture import scan_run_observations
from webagents.capture.archive import canonical_json_bytes
from webagents.io import append_jsonl, atomic_write, sha256_file
from webagents.schemas import RunTranscript
from webagents.sweeps.aggregate import (
    aggregate_cells,
    aggregate_dom_additional,
    aggregate_dom_primary,
    axis_summaries,
    dom_additional_axis_summaries,
    dom_primary_axis_summaries,
    model_confirmations,
)
from webagents.sweeps.config import SweepConfigurationError
from webagents.sweeps.reachability import (
    REACHABILITY_SCORING_POLICY,
    marker_reachable_on_task_page,
)
from webagents.sweeps.report import dom_report_markdown, report_markdown
from webagents.sweeps.schemas import (
    DomLightReceipt,
    DomLightReport,
    DomSweepMatrixRow,
    ReachabilityCorrection,
    RescoreReceipt,
    ResolvedAccessibilitySweep,
    ResolvedDomLightSweep,
    SurpriseCandidate,
    SweepMatrixRow,
    SweepReceipt,
    SweepReport,
    SweepTrialResult,
)


def _jsonl(path: Path, model: type[Any]) -> list[Any]:
    values: list[Any] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), start=1):
        try:
            values.append(model.model_validate_json(line))
        except Exception as exc:
            raise SweepConfigurationError(f"invalid {path}:{line_number}: {exc}") from exc
    return values


def _require_hash(path: Path, expected: str, label: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        raise SweepConfigurationError(f"{label} hash mismatch: expected {expected}, observed {actual}")


def _copy_source_file(source_dir: Path, output_dir: Path, name: str) -> None:
    path = source_dir / name
    if not path.is_file():
        raise SweepConfigurationError(f"completed source sweep is missing {path}")
    atomic_write(output_dir / name, path.read_bytes())


def _rescore_results(
    results: list[SweepTrialResult],
    *,
    tasks: dict[tuple[str, int], Any],
    archive_root: Path,
) -> tuple[list[SweepTrialResult], list[ReachabilityCorrection]]:
    corrected: list[SweepTrialResult] = []
    changes: list[ReachabilityCorrection] = []
    for result in results:
        if not result.counts_toward_cell:
            corrected.append(result)
            continue
        if result.run_id is None or result.reachable is None:
            raise SweepConfigurationError(f"valid result {result.logical_trial_id} lacks reachability evidence")
        run_dir = archive_root / "runs" / result.run_id
        scan = scan_run_observations(run_dir, require_complete=True)
        errors = [item for item in scan.diagnostics if item.severity == "error"]
        if errors or not scan.records:
            detail = "; ".join(f"{item.code}:{item.path}" for item in errors) or "no observations"
            raise SweepConfigurationError(f"cannot rescore verified run {result.run_id}: {detail}")
        try:
            transcript_bytes = (run_dir / "transcript.json").read_bytes()
            transcript = RunTranscript.model_validate_json(transcript_bytes)
        except Exception as exc:
            raise SweepConfigurationError(f"cannot load transcript for {result.run_id}: {exc}") from exc
        if result.transcript_sha256 and hashlib.sha256(transcript_bytes).hexdigest() != result.transcript_sha256:
            raise SweepConfigurationError(f"transcript hash changed for {result.run_id}")
        task = tasks[(result.task_id, result.task_revision)]
        marker = task.revision.level0.reachability_marker.encode("utf-8")
        reachable = marker_reachable_on_task_page(scan.records, transcript, marker)
        updated = result.model_copy(update={"reachable": reachable})
        corrected.append(updated)
        if reachable != result.reachable:
            changes.append(
                ReachabilityCorrection(
                    logical_trial_id=result.logical_trial_id,
                    run_id=result.run_id,
                    task_id=result.task_id,
                    model_id=result.model_id,
                    condition_id=result.condition_id,
                    previous_reachable=result.reachable,
                    corrected_reachable=reachable,
                )
            )
    return corrected, changes


def _final_results(results: list[SweepTrialResult]) -> dict[str, SweepTrialResult]:
    final: dict[str, SweepTrialResult] = {}
    for result in results:
        final[result.logical_trial_id] = result
    return final


def _expectation_candidate(
    *,
    sweep_id: str,
    expected: Literal["present", "absent", "open_question"],
    result: SweepTrialResult,
) -> SurpriseCandidate | None:
    if (
        not result.counts_toward_cell
        or expected == "open_question"
        or result.reachable == (expected == "present")
        or result.run_id is None
        or result.manifest_sha256 is None
    ):
        return None
    observed = "present" if result.reachable else "absent"
    return SurpriseCandidate(
        observed_at=result.finished_at,
        sweep_id=sweep_id,
        logical_trial_id=result.logical_trial_id,
        run_id=result.run_id,
        task_id=result.task_id,
        model_id=result.model_id,
        condition_id=result.condition_id,
        expectation=expected,
        observed_reachable=bool(result.reachable),
        summary=(
            f"Reachability rescored under {REACHABILITY_SCORING_POLICY}; expected marker {expected} "
            f"and observed it as {observed} on the original task page."
        ),
        manifest_sha256=result.manifest_sha256,
    )


def _write_corrections(output_dir: Path, changes: list[ReachabilityCorrection]) -> str:
    payload = {
        "schema_version": "1.0",
        "scoring_policy": REACHABILITY_SCORING_POLICY,
        "changed_trial_count": len(changes),
        "changes": [item.model_dump(mode="json") for item in changes],
    }
    data = canonical_json_bytes(payload)
    atomic_write(output_dir / "reachability-corrections.json", data)
    return hashlib.sha256(data).hexdigest()


def _write_ax_report(
    *,
    source_report: SweepReport,
    resolved: ResolvedAccessibilitySweep,
    results: list[SweepTrialResult],
    output_dir: Path,
) -> SweepReport:
    final = _final_results(results)
    values = list(final.values())
    aggregates = aggregate_cells(resolved, values, include_reused_controls=True)
    live_only = aggregate_cells(resolved, values, include_reused_controls=False)
    summaries = axis_summaries(resolved, aggregates)
    conditions = {item.id: item for item in resolved.spec.conditions}
    candidates = [
        candidate
        for result in values
        if (
            candidate := _expectation_candidate(
                sweep_id=f"{source_report.sweep_id}-reachability-v2",
                expected=conditions[result.condition_id].expected_reachability,
                result=result,
            )
        )
        is not None
    ]
    for candidate in candidates:
        append_jsonl(output_dir / "surprise-candidates.jsonl", candidate.model_dump(mode="json"))
    aggregates_payload = {
        "cells": [item.model_dump(mode="json") for item in aggregates],
        "live_only_cells": [item.model_dump(mode="json") for item in live_only],
        "axis_summaries": [item.model_dump(mode="json") for item in summaries],
        "variance_decomposition_inputs": [
            {
                "agent_class": "accessibility_tree",
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
    report = source_report.model_copy(
        update={
            "sweep_id": f"{source_report.sweep_id}-reachability-v2",
            "results_sha256": sha256_file(output_dir / "results.jsonl"),
            "aggregates": aggregates,
            "live_only_aggregates": live_only,
            "axis_summaries": summaries,
            "surprise_candidate_count": len(candidates),
            "architecture_variance_status": (
                source_report.architecture_variance_status
                + f" Reachability was rescored from immutable archives using {REACHABILITY_SCORING_POLICY}."
            ),
            "finished_at": datetime.now(UTC),
        }
    )
    report_bytes = canonical_json_bytes(report.model_dump(mode="json"))
    atomic_write(output_dir / "report.json", report_bytes)
    atomic_write(output_dir / "report.md", report_markdown(report))
    return report


def _write_dom_report(
    *,
    source_report: DomLightReport,
    resolved: ResolvedDomLightSweep,
    results: list[SweepTrialResult],
    output_dir: Path,
) -> DomLightReport:
    final = _final_results(results)
    values = list(final.values())
    primary = aggregate_dom_primary(resolved, values, include_reused_controls=True)
    live_only = aggregate_dom_primary(resolved, values, include_reused_controls=False)
    summaries = dom_primary_axis_summaries(resolved, primary)
    additional = aggregate_dom_additional(resolved, values, include_reused_controls=True)
    additional_summaries = dom_additional_axis_summaries(resolved, additional)
    confirmations = (
        model_confirmations(resolved, primary, values)
        if resolved.spec.allocation.additional_valid_trials_per_cell == 1
        else []
    )
    conditions = {item.id: item for item in resolved.spec.conditions}
    corrected_id = f"{source_report.sweep_id}-reachability-v2"
    candidates = [
        candidate
        for result in values
        if (
            candidate := _expectation_candidate(
                sweep_id=corrected_id,
                expected=conditions[result.condition_id].dom_expected_reachability,
                result=result,
            )
        )
        is not None
    ]
    by_run = {item.run_id: item for item in values if item.run_id is not None}
    for confirmation in confirmations:
        if not confirmation.divergent:
            continue
        result = by_run[confirmation.run_id]
        if result.manifest_sha256 is None:
            continue
        modal = confirmation.primary_modal_reachability
        expected = "open_question" if modal is None else ("present" if modal else "absent")
        candidates.append(
            SurpriseCandidate(
                observed_at=result.finished_at,
                sweep_id=corrected_id,
                logical_trial_id=result.logical_trial_id,
                run_id=confirmation.run_id,
                task_id=confirmation.task_id,
                model_id=confirmation.model_id,
                condition_id=confirmation.condition_id,
                expectation=expected,
                observed_reachable=confirmation.reachable,
                summary=(
                    "One-shot additional-model confirmation diverged after initial-task-page "
                    "reachability rescoring; no adaptive resampling was performed."
                ),
                manifest_sha256=result.manifest_sha256,
                trigger="model_divergence",
                primary_model_id=resolved.primary_model.id,
            )
        )
    for candidate in candidates:
        append_jsonl(output_dir / "surprise-candidates.jsonl", candidate.model_dump(mode="json"))
    primary_payload = {
        "primary_model": resolved.primary_model.model_dump(mode="json"),
        "cells": [item.model_dump(mode="json") for item in primary],
        "live_only_cells": [item.model_dump(mode="json") for item in live_only],
        "axis_summaries": [item.model_dump(mode="json") for item in summaries],
        "additional_cells": [item.model_dump(mode="json") for item in additional],
        "additional_axis_summaries": [item.model_dump(mode="json") for item in additional_summaries],
    }
    confirmation_payload = {
        "primary_model_id": resolved.primary_model.id,
        "additional_model_ids": [item.id for item in resolved.additional_models],
        "interpretation": "Each row is one preregistered run; no reliability interval is estimated.",
        "confirmations": [item.model_dump(mode="json") for item in confirmations],
    }
    atomic_write(output_dir / "primary-aggregates.json", canonical_json_bytes(primary_payload))
    atomic_write(output_dir / "model-confirmations.json", canonical_json_bytes(confirmation_payload))
    report = source_report.model_copy(
        update={
            "sweep_id": corrected_id,
            "results_sha256": sha256_file(output_dir / "results.jsonl"),
            "primary_aggregates": primary,
            "live_only_primary_aggregates": live_only,
            "primary_axis_summaries": summaries,
            "additional_aggregates": additional,
            "additional_axis_summaries": additional_summaries,
            "model_confirmations": confirmations,
            "divergent_confirmation_count": sum(item.divergent for item in confirmations),
            "surprise_candidate_count": len(candidates),
            "interpretation_limit": (
                source_report.interpretation_limit
                + f" Reachability was rescored from immutable archives using {REACHABILITY_SCORING_POLICY}."
            ),
            "finished_at": datetime.now(UTC),
        }
    )
    report_bytes = canonical_json_bytes(report.model_dump(mode="json"))
    atomic_write(output_dir / "report.json", report_bytes)
    atomic_write(output_dir / "report.md", dom_report_markdown(report))
    return report


def rescore_sweep(
    source_dir: Path,
    *,
    archive_root: Path,
    output_dir: Path,
) -> SweepReport | DomLightReport:
    """Rescore a completed AX or DOM sweep without invoking any model provider."""

    source_dir = source_dir.resolve()
    archive_root = archive_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise SweepConfigurationError(f"corrected output already exists: {output_dir}")
    report_bytes = (source_dir / "report.json").read_bytes()
    try:
        source_report: SweepReport | DomLightReport = SweepReport.model_validate_json(report_bytes)
        receipt = SweepReceipt.model_validate_json((source_dir / "sweep-receipt.json").read_bytes())
        resolved: ResolvedAccessibilitySweep | ResolvedDomLightSweep = (
            ResolvedAccessibilitySweep.model_validate_json((source_dir / "resolved-spec.json").read_bytes())
        )
        matrix_model: type[Any] = SweepMatrixRow
        agent_class = "accessibility_tree"
    except Exception:
        try:
            source_report = DomLightReport.model_validate_json(report_bytes)
            receipt = DomLightReceipt.model_validate_json((source_dir / "sweep-receipt.json").read_bytes())
            resolved = ResolvedDomLightSweep.model_validate_json((source_dir / "resolved-spec.json").read_bytes())
            matrix_model = DomSweepMatrixRow
            agent_class = "dom_extraction"
        except Exception as exc:
            raise SweepConfigurationError(f"source is not a complete AX or DOM sweep: {exc}") from exc
    if source_report.verdict != "complete" or receipt.verdict != "complete":
        raise SweepConfigurationError("only completed, receipted sweeps can be rescored")
    for name, expected in (
        ("resolved-spec.json", source_report.resolved_spec_sha256),
        ("matrix.jsonl", source_report.matrix_sha256),
        ("results.jsonl", source_report.results_sha256),
        ("exclusions.jsonl", source_report.exclusions_sha256),
        ("reused-controls.jsonl", source_report.reused_controls_sha256),
    ):
        _require_hash(source_dir / name, expected, name)
    if hashlib.sha256(report_bytes).hexdigest() != receipt.report_sha256:
        raise SweepConfigurationError("source report does not match its sweep receipt")
    _jsonl(source_dir / "matrix.jsonl", matrix_model)
    results = _jsonl(source_dir / "results.jsonl", SweepTrialResult)
    tasks = {(item.revision.task_id, item.revision.revision): item for item in resolved.tasks}
    corrected, changes = _rescore_results(results, tasks=tasks, archive_root=archive_root)

    output_dir.mkdir(parents=True)
    for name in (
        "resolved-spec.json",
        "conditions.json",
        "matrix.jsonl",
        "exclusions.jsonl",
        "reused-controls.jsonl",
    ):
        _copy_source_file(source_dir, output_dir, name)
    atomic_write(
        output_dir / "source-sweep-receipt.json",
        (source_dir / "sweep-receipt.json").read_bytes(),
    )
    atomic_write(output_dir / "surprise-candidates.jsonl", b"")
    atomic_write(output_dir / "results.jsonl", b"")
    for result in corrected:
        append_jsonl(output_dir / "results.jsonl", result.model_dump(mode="json"))
    correction_hash = _write_corrections(output_dir, changes)
    if agent_class == "accessibility_tree":
        assert isinstance(source_report, SweepReport)
        assert isinstance(resolved, ResolvedAccessibilitySweep)
        corrected_report = _write_ax_report(
            source_report=source_report,
            resolved=resolved,
            results=corrected,
            output_dir=output_dir,
        )
    else:
        assert isinstance(source_report, DomLightReport)
        assert isinstance(resolved, ResolvedDomLightSweep)
        corrected_report = _write_dom_report(
            source_report=source_report,
            resolved=resolved,
            results=corrected,
            output_dir=output_dir,
        )
    corrected_report_bytes = (output_dir / "report.json").read_bytes()
    rescore_receipt = RescoreReceipt(
        source_sweep_id=source_report.sweep_id,
        corrected_analysis_id=corrected_report.sweep_id,
        agent_class=agent_class,
        scoring_policy=REACHABILITY_SCORING_POLICY,
        source_resolved_spec_sha256=source_report.resolved_spec_sha256,
        source_matrix_sha256=source_report.matrix_sha256,
        source_results_sha256=source_report.results_sha256,
        source_report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        source_receipt_sha256=sha256_file(source_dir / "sweep-receipt.json"),
        corrected_results_sha256=sha256_file(output_dir / "results.jsonl"),
        corrected_report_sha256=hashlib.sha256(corrected_report_bytes).hexdigest(),
        correction_manifest_sha256=correction_hash,
        valid_trial_count=corrected_report.valid_trial_count,
        changed_trial_count=len(changes),
        created_at=datetime.now(UTC),
    )
    atomic_write(
        output_dir / "rescore-receipt.json",
        canonical_json_bytes(rescore_receipt.model_dump(mode="json")),
    )
    return corrected_report
