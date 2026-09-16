"""Execute the asymmetric Plan 6 DOM-extraction sweep."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webagents.capture import scan_run_observations
from webagents.capture.archive import canonical_json_bytes
from webagents.control.harness import HarnessError
from webagents.control.schemas import MatrixRow as ControlMatrixRow
from webagents.control.schemas import ResolvedTask
from webagents.control.schemas import TrialResult as ControlTrialResult
from webagents.io import append_jsonl, atomic_write, sha256_file
from webagents.providers.config import ProviderConfigurationError, validate_provider_environment
from webagents.schemas import RUN_ID_PATTERN, RunManifest, RunTranscript
from webagents.sweeps.aggregate import (
    aggregate_dom_additional,
    aggregate_dom_primary,
    dom_additional_axis_summaries,
    dom_primary_axis_summaries,
    model_confirmations,
)
from webagents.sweeps.config import (
    ResolvedDomSweepBundle,
    SweepConfigurationError,
    resolve_dom_light_spec,
)
from webagents.sweeps.harness import StructuralFixtureHarness
from webagents.sweeps.inheritance import load_inherited_dom_results
from webagents.sweeps.matrix import expand_dom_light_matrix
from webagents.sweeps.reachability import marker_reachable_on_task_page
from webagents.sweeps.report import dom_report_markdown
from webagents.sweeps.scheduler import _blindness, _jsonl_models, _provider_cost
from webagents.sweeps.schemas import (
    DomLightReceipt,
    DomLightReport,
    DomSweepMatrixRow,
    ResolvedDomLightSweep,
    StructuralCondition,
    SurpriseCandidate,
    SweepClassification,
    SweepTrialResult,
)

Runner = Callable[..., Awaitable[RunTranscript]]


def _default_runner() -> Runner:
    from webagents.dom.runner import run_dom_agent

    return run_dom_agent


def _preflight_credentials(resolved: ResolvedDomLightSweep) -> None:
    models = [resolved.primary_model, *resolved.additional_models]
    try:
        validate_provider_environment(model.runner_model for model in models)
    except ProviderConfigurationError as exc:
        raise SweepConfigurationError(f"invalid frozen model roster environment: {exc}") from exc


def _run_id(sweep_id: str, row: DomSweepMatrixRow, attempt: int) -> str:
    value = f"DOM6-{sweep_id[:23]}-{row.logical_trial_id}-A{attempt}"
    if not RUN_ID_PATTERN.fullmatch(value):
        raise SweepConfigurationError("generated DOM sweep run ID is invalid")
    return value


def _base_result(row: DomSweepMatrixRow, attempt: int, started_at: datetime) -> dict[str, Any]:
    return {
        "logical_trial_id": row.logical_trial_id,
        "allocation_role": row.allocation_role,
        "task_id": row.task_id,
        "task_revision": row.task_revision,
        "model_id": row.model_id,
        "condition_id": row.condition_id,
        "repeat": row.repeat,
        "infrastructure_attempt": attempt,
        "started_at": started_at,
    }


async def _execute_dom_once(
    *,
    resolved: ResolvedDomLightSweep,
    row: DomSweepMatrixRow,
    task: ResolvedTask,
    condition: StructuralCondition,
    archive_root: Path,
    harness: StructuralFixtureHarness,
    runner: Runner,
    attempt: int,
) -> SweepTrialResult:
    started_at = datetime.now(UTC)
    base = _base_result(row, attempt, started_at)
    try:
        harness.reset(task)
    except HarnessError as exc:
        return SweepTrialResult.model_validate(
            {
                **base,
                "classification": "FIXTURE_FAILURE",
                "checker_detail": str(exc),
                "finished_at": datetime.now(UTC),
            }
        )

    run_id = _run_id(resolved.spec.sweep_id, row, attempt)
    page_url = harness.condition_url(task, condition)
    try:
        runner_kwargs: dict[str, Any] = {
            "run_id": run_id,
            "max_steps": resolved.spec.runtime.max_steps,
            "archive_root": archive_root,
            "timeout_seconds": resolved.spec.runtime.timeout_seconds,
        }
        if row.observation_policy != "unrestricted":
            runner_kwargs["observation_policy"] = row.observation_policy
        transcript = await runner(
            page_url,
            task.revision.level0.prompt,
            row.runner_model,
            **runner_kwargs,
        )
    except Exception as exc:
        return SweepTrialResult.model_validate(
            {
                **base,
                "run_id": run_id,
                "classification": "INFRASTRUCTURE_FAILURE",
                "checker_detail": f"runner raised {type(exc).__name__}: {exc}",
                "finished_at": datetime.now(UTC),
            }
        )

    run_dir = archive_root / "runs" / run_id
    scan = scan_run_observations(run_dir, require_complete=True)
    error_diagnostics = [
        f"{item.code}:{item.path}:{item.message}" for item in scan.diagnostics if item.severity == "error"
    ]
    common = {
        **base,
        "run_id": run_id,
        "runner_status": transcript.status,
        "runner_success_claimed": transcript.final.success_claimed,
        "archive_valid": not error_diagnostics,
        "archive_diagnostics": error_diagnostics,
        "input_tokens": sum(step.usage.input_tokens for step in transcript.steps),
        "output_tokens": sum(step.usage.output_tokens for step in transcript.steps),
        "actions": sum(len(step.actions) for step in transcript.steps),
        "provider_cost_usd": _provider_cost(transcript),
        "finished_at": datetime.now(UTC),
    }
    if error_diagnostics:
        return SweepTrialResult.model_validate({**common, "classification": "EVIDENCE_FAILURE"})

    try:
        manifest_bytes = (run_dir / "manifest.json").read_bytes()
        transcript_bytes = (run_dir / "transcript.json").read_bytes()
        manifest = RunManifest.model_validate_json(manifest_bytes)
        archived_transcript = RunTranscript.model_validate_json(transcript_bytes)
    except Exception as exc:
        return SweepTrialResult.model_validate(
            {
                **common,
                "classification": "EVIDENCE_FAILURE",
                "checker_detail": f"cannot load verified archive: {exc}",
            }
        )
    hashes = {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
    }
    if not (
        manifest.run_id == run_id
        and manifest.agent_class == "dom_extraction"
        and manifest.input.page_url == page_url
        and manifest.input.task_prompt == task.revision.level0.prompt
        and manifest.input.model == row.runner_model
        and manifest.input.observation_policy == row.observation_policy
        and archived_transcript.input == manifest.input
    ):
        return SweepTrialResult.model_validate(
            {
                **common,
                **hashes,
                "classification": "CONFIGURATION_FAILURE",
                "checker_detail": "run archive identity does not match the scheduled DOM trial",
            }
        )
    if archived_transcript.status == "infrastructure_error":
        return SweepTrialResult.model_validate(
            {
                **common,
                **hashes,
                "classification": "INFRASTRUCTURE_FAILURE",
                "checker_detail": archived_transcript.final.error,
            }
        )

    records = scan.records
    missing_targets = sorted({target for record in records for target in record.missing_targets})
    capture_complete = bool(records) and all(record.capture_completeness == "complete" for record in records)
    marker = task.revision.level0.reachability_marker.encode("utf-8")
    reachable = marker_reachable_on_task_page(records, archived_transcript, marker)
    evidence = {
        "reachable": reachable,
        "capture_complete": capture_complete,
        "missing_targets": missing_targets,
    }
    if not records:
        return SweepTrialResult.model_validate(
            {
                **common,
                **hashes,
                **evidence,
                "classification": "INFRASTRUCTURE_FAILURE",
                "checker_detail": "DOM runner completed without a model-visible observation",
            }
        )
    if not capture_complete:
        return SweepTrialResult.model_validate(
            {
                **common,
                **hashes,
                **evidence,
                "classification": "EVIDENCE_FAILURE",
                "checker_detail": "one or more serialized-DOM captures are partial",
            }
        )

    checker = task.revision.level0.checker
    try:
        if checker.name == "json_endpoint_equals":
            actual = harness.read_json_checker(task)
            checker_passed = actual == checker.expected
            checker_detail = f"{checker.json_field}={actual!r}, expected={checker.expected!r}"
        else:
            actual = archived_transcript.final.answer
            checker_passed = actual is not None and actual.strip() == checker.expected
            checker_detail = f"final_answer={actual!r}, expected={checker.expected!r}"
    except HarnessError as exc:
        return SweepTrialResult.model_validate(
            {
                **common,
                **hashes,
                **evidence,
                "classification": "FIXTURE_FAILURE",
                "checker_detail": str(exc),
            }
        )
    task_success = (
        archived_transcript.status == "success" and archived_transcript.final.success_claimed and checker_passed
    )
    return SweepTrialResult.model_validate(
        {
            **common,
            **hashes,
            **evidence,
            "classification": "VALID_OUTCOME",
            "counts_toward_cell": True,
            "task_success": task_success,
            "self_reported_blindness": _blindness(archived_transcript),
            "checker_detail": checker_detail,
        }
    )


def _all_models(resolved: ResolvedDomLightSweep) -> list[Any]:
    return [resolved.primary_model, *resolved.additional_models]


def _eligible_dom_control_reuse(
    bundle: ResolvedDomSweepBundle,
    archive_root: Path,
) -> dict[tuple[str, int, str], list[ControlTrialResult]]:
    resolved = bundle.resolved
    if not resolved.spec.reuse_level0_controls:
        return {}
    resolved_control = json.loads((bundle.receipt_path.parent / "resolved-spec.json").read_bytes())["control"]
    policy = resolved_control["policy"]
    if (
        policy["max_steps"] != resolved.spec.runtime.max_steps
        or float(policy["timeout_seconds"]) != resolved.spec.runtime.timeout_seconds
    ):
        return {}
    matrix = _jsonl_models(bundle.receipt_path.parent / "matrix.jsonl", ControlMatrixRow)
    results = _jsonl_models(bundle.receipt_path.parent / "results.jsonl", ControlTrialResult)
    matrix_by_id = {row.logical_trial_id: row for row in matrix}
    candidates: dict[tuple[str, int, str], list[ControlTrialResult]] = defaultdict(list)
    active = {(task.revision.task_id, task.revision.revision): task for task in resolved.tasks}
    models = {model.id: model for model in _all_models(resolved)}
    for result in results:
        if (
            result.classification != "PASS"
            or result.class_id != "dom_extraction"
            or result.run_id is None
            or result.reachable is not True
            or result.capture_complete is not True
            or result.archive_valid is not True
        ):
            continue
        control_row = matrix_by_id.get(result.logical_trial_id)
        task = active.get((result.task_id, result.task_revision))
        model = models.get(result.model_id)
        if (
            control_row is None
            or task is None
            or model is None
            or control_row.task_definition_sha256 != task.definition_sha256
        ):
            continue
        run_dir = archive_root / "runs" / result.run_id
        scan = scan_run_observations(run_dir, require_complete=True)
        if any(item.severity == "error" for item in scan.diagnostics) or not scan.records:
            continue
        try:
            manifest = RunManifest.model_validate_json((run_dir / "manifest.json").read_bytes())
        except Exception:
            continue
        if (
            manifest.agent_class != "dom_extraction"
            or manifest.input.task_prompt != task.revision.level0.prompt
            or manifest.input.model != model.runner_model
        ):
            continue
        candidates[(result.task_id, result.task_revision, result.model_id)].append(result)
    for values in candidates.values():
        values.sort(key=lambda item: (item.repeat, item.infrastructure_attempt, item.run_id or ""))
    return candidates


def _reused_dom_result(row: DomSweepMatrixRow, control: ControlTrialResult) -> SweepTrialResult:
    assert control.run_id is not None
    return SweepTrialResult(
        logical_trial_id=row.logical_trial_id,
        source="control_reuse",
        allocation_role=row.allocation_role,
        task_id=row.task_id,
        task_revision=row.task_revision,
        model_id=row.model_id,
        condition_id=row.condition_id,
        repeat=row.repeat,
        infrastructure_attempt=control.infrastructure_attempt,
        run_id=control.run_id,
        classification="VALID_OUTCOME",
        counts_toward_cell=True,
        runner_status=control.runner_status,
        runner_success_claimed=control.runner_success_claimed,
        archive_valid=True,
        reachable=True,
        task_success=True,
        self_reported_blindness=False,
        capture_complete=True,
        checker_detail=f"eligible Plan 4 DOM control reuse: {control.checker_detail}",
        manifest_sha256=control.manifest_sha256,
        transcript_sha256=control.transcript_sha256,
        input_tokens=control.input_tokens,
        output_tokens=control.output_tokens,
        actions=control.actions,
        started_at=control.started_at,
        finished_at=control.finished_at,
    )


def _expectation_candidate(
    resolved: ResolvedDomLightSweep,
    row: DomSweepMatrixRow,
    result: SweepTrialResult,
) -> SurpriseCandidate | None:
    condition = next(item for item in resolved.spec.conditions if item.id == row.condition_id)
    expected = condition.dom_expected_reachability
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
        sweep_id=resolved.spec.sweep_id,
        logical_trial_id=row.logical_trial_id,
        run_id=result.run_id,
        task_id=row.task_id,
        model_id=row.model_id,
        condition_id=row.condition_id,
        expectation=expected,
        observed_reachable=bool(result.reachable),
        summary=f"DOM expectation was {expected}; exact serialized observation recorded {observed}.",
        manifest_sha256=result.manifest_sha256,
    )


async def execute_resolved_dom_sweep(
    bundle: ResolvedDomSweepBundle,
    *,
    archive_root: Path,
    output_root: Path,
    runner: Runner | None = None,
    preflight_credentials: bool = True,
) -> DomLightReport:
    resolved = bundle.resolved
    if preflight_credentials:
        _preflight_credentials(resolved)
    actual_archive_root = archive_root.resolve()
    actual_output_root = output_root.resolve()
    output_dir = actual_output_root / resolved.spec.sweep_id
    if output_dir.parent.resolve() != actual_output_root:
        raise SweepConfigurationError("DOM sweep output escaped output_root")
    try:
        output_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise SweepConfigurationError(f"DOM sweep output already exists: {output_dir}") from exc

    paths = {
        name: output_dir / name
        for name in (
            "matrix.jsonl",
            "results.jsonl",
            "reused-controls.jsonl",
            "exclusions.jsonl",
            "surprise-candidates.jsonl",
            "inherited-results.jsonl",
        )
    }
    for path in paths.values():
        atomic_write(path, b"")
    resolved_bytes = canonical_json_bytes(resolved.model_dump(mode="json"))
    atomic_write(output_dir / "resolved-spec.json", resolved_bytes)
    conditions_payload = {
        "conditions": [item.model_dump(mode="json") for item in resolved.spec.conditions],
        "axes": [item.model_dump(mode="json") for item in resolved.spec.axes],
    }
    atomic_write(output_dir / "conditions.json", canonical_json_bytes(conditions_payload))
    if sha256_file(output_dir / "conditions.json") != resolved.conditions_sha256:
        raise SweepConfigurationError("resolved DOM condition inventory hash mismatch")

    matrix = expand_dom_light_matrix(resolved)
    for row in matrix:
        append_jsonl(paths["matrix.jsonl"], row.model_dump(mode="json"))
    inherited: dict[str, SweepTrialResult] = {}
    inheritance_metadata: dict[str, object] | None = None
    if resolved.spec.inherit_results_from is not None:
        declared = Path(resolved.spec.inherit_results_from)
        source_dir = declared.resolve() if declared.is_absolute() else (bundle.project_root / declared).resolve()
        inherited, inheritance_metadata = load_inherited_dom_results(
            source_dir,
            resolved=resolved,
            matrix=matrix,
        )
        atomic_write(output_dir / "inheritance.json", canonical_json_bytes(inheritance_metadata))
    reused = _eligible_dom_control_reuse(bundle, actual_archive_root)
    reuse_offsets: dict[tuple[str, int, str], int] = defaultdict(int)
    task_by_id = {(task.revision.task_id, task.revision.revision): task for task in resolved.tasks}
    condition_by_id = {condition.id: condition for condition in resolved.spec.conditions}
    all_results: list[SweepTrialResult] = []
    final_results: dict[str, SweepTrialResult] = {}
    rows_by_id = {row.logical_trial_id: row for row in matrix}
    blocking: set[SweepClassification] = set()
    surprise_candidates: list[SurpriseCandidate] = []
    started_at = datetime.now(UTC)
    runner_impl = runner or _default_runner()

    with StructuralFixtureHarness(resolved.spec.harness, resolved.tasks, resolved.spec.conditions) as harness:
        harness.preflight()
        for row in matrix:
            task = task_by_id[(row.task_id, row.task_revision)]
            condition = condition_by_id[row.condition_id]
            if row.logical_trial_id in inherited:
                result = inherited[row.logical_trial_id]
                all_results.append(result)
                final_results[row.logical_trial_id] = result
                append_jsonl(paths["results.jsonl"], result.model_dump(mode="json"))
                append_jsonl(paths["inherited-results.jsonl"], result.model_dump(mode="json"))
                candidate = _expectation_candidate(resolved, row, result)
                if candidate is not None:
                    surprise_candidates.append(candidate)
                continue
            reuse_key = (row.task_id, row.task_revision, row.model_id)
            reuse_values = reused.get(reuse_key, [])
            offset = reuse_offsets[reuse_key]
            if row.condition_id == "level-0" and offset < len(reuse_values):
                result = _reused_dom_result(row, reuse_values[offset])
                reuse_offsets[reuse_key] += 1
                all_results.append(result)
                final_results[row.logical_trial_id] = result
                append_jsonl(paths["results.jsonl"], result.model_dump(mode="json"))
                append_jsonl(paths["reused-controls.jsonl"], result.model_dump(mode="json"))
                continue

            final: SweepTrialResult | None = None
            for attempt in range(resolved.spec.allocation.max_infrastructure_retries + 1):
                result = await _execute_dom_once(
                    resolved=resolved,
                    row=row,
                    task=task,
                    condition=condition,
                    archive_root=actual_archive_root,
                    harness=harness,
                    runner=runner_impl,
                    attempt=attempt,
                )
                all_results.append(result)
                append_jsonl(paths["results.jsonl"], result.model_dump(mode="json"))
                if not result.counts_toward_cell:
                    append_jsonl(paths["exclusions.jsonl"], result.model_dump(mode="json"))
                final = result
                retryable = result.classification == "INFRASTRUCTURE_FAILURE" or (
                    result.classification == "EVIDENCE_FAILURE" and result.capture_complete is False
                )
                if result.counts_toward_cell or not retryable:
                    break
            assert final is not None
            final_results[row.logical_trial_id] = final
            if not final.counts_toward_cell:
                blocking.add(final.classification)
                if final.classification in {
                    "EVIDENCE_FAILURE",
                    "FIXTURE_FAILURE",
                    "CONFIGURATION_FAILURE",
                }:
                    break
            candidate = _expectation_candidate(resolved, row, final)
            if candidate is not None:
                surprise_candidates.append(candidate)

    final_values = list(final_results.values())
    primary = aggregate_dom_primary(resolved, final_values, include_reused_controls=True)
    live_only_primary = aggregate_dom_primary(resolved, final_values, include_reused_controls=False)
    summaries = dom_primary_axis_summaries(resolved, primary)
    additional = aggregate_dom_additional(resolved, final_values, include_reused_controls=True)
    additional_summaries = dom_additional_axis_summaries(resolved, additional)
    confirmations = (
        model_confirmations(resolved, primary, final_values)
        if resolved.spec.allocation.additional_valid_trials_per_cell == 1
        else []
    )
    result_by_run = {result.run_id: result for result in final_values if result.run_id is not None}
    row_by_run = {
        result.run_id: rows_by_id[result.logical_trial_id] for result in final_values if result.run_id is not None
    }
    for confirmation in confirmations:
        if not confirmation.divergent:
            continue
        result = result_by_run[confirmation.run_id]
        row = row_by_run[confirmation.run_id]
        if result.manifest_sha256 is None:
            continue
        modal = confirmation.primary_modal_reachability
        expected = "open_question" if modal is None else ("present" if modal else "absent")
        surprise_candidates.append(
            SurpriseCandidate(
                observed_at=result.finished_at,
                sweep_id=resolved.spec.sweep_id,
                logical_trial_id=result.logical_trial_id,
                run_id=confirmation.run_id,
                task_id=confirmation.task_id,
                model_id=confirmation.model_id,
                condition_id=confirmation.condition_id,
                expectation=expected,
                observed_reachable=confirmation.reachable,
                summary=(
                    "One-shot additional-model confirmation diverged from the primary-model modal "
                    "reachability and/or success outcome; no adaptive resampling was performed."
                ),
                manifest_sha256=result.manifest_sha256,
                trigger="model_divergence",
                primary_model_id=resolved.primary_model.id,
            )
        )
    for candidate in surprise_candidates:
        append_jsonl(paths["surprise-candidates.jsonl"], candidate.model_dump(mode="json"))

    primary_complete = sum(item.complete for item in primary)
    primary_required = len(primary)
    confirmation_required = (
        len(resolved.tasks)
        * len(resolved.spec.conditions)
        * len(resolved.additional_models)
        * resolved.spec.allocation.additional_valid_trials_per_cell
    )
    confirmation_complete = sum(
        result.counts_toward_cell and result.allocation_role == "confirmation" for result in final_values
    )
    if primary_complete == primary_required and confirmation_complete == confirmation_required and not blocking:
        verdict = "complete"
    elif blocking - {"INFRASTRUCTURE_FAILURE"}:
        verdict = "blocked"
    else:
        verdict = "incomplete"

    primary_payload = {
        "primary_model": resolved.primary_model.model_dump(mode="json"),
        "cells": [item.model_dump(mode="json") for item in primary],
        "live_only_cells": [item.model_dump(mode="json") for item in live_only_primary],
        "axis_summaries": [item.model_dump(mode="json") for item in summaries],
    }
    confirmation_payload = {
        "primary_model_id": resolved.primary_model.id,
        "additional_model_ids": [item.id for item in resolved.additional_models],
        "allocation_per_cell": resolved.spec.allocation.additional_valid_trials_per_cell,
        "interpretation": (
            "Each row is one preregistered directional confirmation; no reliability interval is estimated."
            if resolved.spec.allocation.additional_valid_trials_per_cell == 1
            else "Additional-model cells are fully repeated and summarized with Wilson intervals."
        ),
        "confirmations": [item.model_dump(mode="json") for item in confirmations],
        "additional_cells": [item.model_dump(mode="json") for item in additional],
        "additional_axis_summaries": [item.model_dump(mode="json") for item in additional_summaries],
    }
    primary_bytes = canonical_json_bytes(primary_payload)
    confirmation_bytes = canonical_json_bytes(confirmation_payload)
    atomic_write(output_dir / "primary-aggregates.json", primary_bytes)
    atomic_write(output_dir / "model-confirmations.json", confirmation_bytes)

    report = DomLightReport(
        sweep_id=resolved.spec.sweep_id,
        verdict=verdict,
        primary_model_id=resolved.primary_model.id,
        additional_model_ids=[item.id for item in resolved.additional_models],
        resolved_spec_sha256=hashlib.sha256(resolved_bytes).hexdigest(),
        conditions_sha256=resolved.conditions_sha256,
        matrix_sha256=sha256_file(paths["matrix.jsonl"]),
        results_sha256=sha256_file(paths["results.jsonl"]),
        exclusions_sha256=sha256_file(paths["exclusions.jsonl"]),
        reused_controls_sha256=sha256_file(paths["reused-controls.jsonl"]),
        primary_required_cells=primary_required,
        primary_complete_cells=primary_complete,
        confirmation_required_cells=confirmation_required,
        confirmation_complete_cells=confirmation_complete,
        valid_trial_count=sum(result.counts_toward_cell for result in final_values),
        attempt_count=len(all_results),
        blocking_classifications=sorted(blocking),
        primary_aggregates=primary,
        live_only_primary_aggregates=live_only_primary,
        primary_axis_summaries=summaries,
        additional_aggregates=additional,
        additional_axis_summaries=additional_summaries,
        model_confirmations=confirmations,
        divergent_confirmation_count=sum(item.divergent for item in confirmations),
        surprise_candidate_count=len(surprise_candidates),
        inherited_trial_count=len(inherited),
        interpretation_limit=(
            "Both model arms have N=10 per task-condition cell with Wilson intervals; the crossed DOM "
            "design supports a fixed two-model robustness comparison."
            if resolved.spec.allocation.additional_valid_trials_per_cell == 10
            else "Primary-model cells have N=10 and Wilson intervals. Each additional-model cell is one "
            "directional confirmation only; confirmations are not pooled into primary estimates and "
            "cannot establish cross-model reliability."
        ),
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
    report_bytes = canonical_json_bytes(report.model_dump(mode="json"))
    atomic_write(output_dir / "report.json", report_bytes)
    atomic_write(output_dir / "report.md", dom_report_markdown(report))
    if report.verdict == "complete":
        receipt = DomLightReceipt(
            sweep_id=report.sweep_id,
            resolved_spec_sha256=report.resolved_spec_sha256,
            conditions_sha256=report.conditions_sha256,
            matrix_sha256=report.matrix_sha256,
            results_sha256=report.results_sha256,
            exclusions_sha256=report.exclusions_sha256,
            reused_controls_sha256=report.reused_controls_sha256,
            primary_aggregates_sha256=hashlib.sha256(primary_bytes).hexdigest(),
            model_confirmations_sha256=hashlib.sha256(confirmation_bytes).hexdigest(),
            report_sha256=hashlib.sha256(report_bytes).hexdigest(),
            primary_model=resolved.primary_model.runner_model,
            additional_models=[item.runner_model for item in resolved.additional_models],
            tasks=[f"{item.revision.task_id}:r{item.revision.revision}" for item in resolved.tasks],
            condition_ids=[item.id for item in resolved.spec.conditions],
            additional_valid_trials_per_cell=resolved.spec.allocation.additional_valid_trials_per_cell,
            inherited_results_sha256=(
                sha256_file(paths["inherited-results.jsonl"]) if inherited else None
            ),
            inherited_trial_count=len(inherited),
            completed_at=report.finished_at,
        )
        atomic_write(output_dir / "sweep-receipt.json", canonical_json_bytes(receipt.model_dump(mode="json")))
    return report


async def run_dom_light_sweep(
    spec_path: Path,
    *,
    archive_root: Path,
    output_root: Path,
    control_receipt: Path | None = None,
    runner: Runner | None = None,
    preflight_credentials: bool = True,
) -> DomLightReport:
    bundle = resolve_dom_light_spec(spec_path, control_receipt_override=control_receipt)
    return await execute_resolved_dom_sweep(
        bundle,
        archive_root=archive_root,
        output_root=output_root,
        runner=runner,
        preflight_credentials=preflight_credentials,
    )
