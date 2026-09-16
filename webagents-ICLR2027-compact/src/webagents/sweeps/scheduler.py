"""Execute an admitted full-allocation architecture matrix."""

from __future__ import annotations

import hashlib
import json
import re
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
from webagents.sweeps.aggregate import aggregate_cells, axis_summaries
from webagents.sweeps.config import ResolvedSweepBundle, SweepConfigurationError, resolve_sweep_spec
from webagents.sweeps.harness import StructuralFixtureHarness
from webagents.sweeps.matrix import expand_full_matrix
from webagents.sweeps.reachability import marker_reachable_on_task_page
from webagents.sweeps.report import report_markdown
from webagents.sweeps.schemas import (
    ResolvedAccessibilitySweep,
    StructuralCondition,
    SurpriseCandidate,
    SweepClassification,
    SweepMatrixRow,
    SweepReceipt,
    SweepReport,
    SweepTrialResult,
)

Runner = Callable[..., Awaitable[RunTranscript]]

_BLINDNESS_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bcannot (?:access|see|inspect|reach|find)\b",
        r"\bcan't (?:access|see|inspect|reach|find)\b",
        r"\bunable to (?:access|see|inspect|reach|find)\b",
        r"\bnot visible\b",
        r"\bunreachable\b",
    )
)


def _default_runner(runner_name: str) -> Runner:
    if runner_name == "ax":
        from webagents.ax.runner import run_ax_agent

        return run_ax_agent
    if runner_name == "vision":
        from webagents.vision.runner import run_vision_agent

        return run_vision_agent
    if runner_name == "cdp":
        from webagents.cdp.runner import run_cdp_agent

        return run_cdp_agent
    raise SweepConfigurationError(f"unsupported full-sweep runner: {runner_name}")


def _preflight_credentials(resolved: ResolvedAccessibilitySweep) -> None:
    try:
        validate_provider_environment(model.runner_model for model in resolved.models)
    except ProviderConfigurationError as exc:
        raise SweepConfigurationError(f"invalid frozen model roster environment: {exc}") from exc


def _run_id(sweep_id: str, row: SweepMatrixRow, attempt: int) -> str:
    prefix = {"ax": "AX5", "vision": "VIS", "cdp": "CDP"}[row.runner]
    value = f"{prefix}-{sweep_id[:24]}-{row.logical_trial_id}-A{attempt}"
    if not RUN_ID_PATTERN.fullmatch(value):
        raise SweepConfigurationError("generated sweep run ID is invalid")
    return value


def _task_map(resolved: ResolvedAccessibilitySweep) -> dict[tuple[str, int], ResolvedTask]:
    return {(task.revision.task_id, task.revision.revision): task for task in resolved.tasks}


def _condition_map(resolved: ResolvedAccessibilitySweep) -> dict[str, StructuralCondition]:
    return {condition.id: condition for condition in resolved.spec.conditions}


def _blindness(transcript: RunTranscript) -> bool:
    text = "\n".join(
        value
        for value in (
            transcript.final.answer,
            transcript.final.error,
            *(step.model_response.text for step in transcript.steps if step.model_response is not None),
        )
        if value
    )
    return any(pattern.search(text) is not None for pattern in _BLINDNESS_PATTERNS)


def _provider_cost(transcript: RunTranscript) -> float | None:
    total = 0.0
    found = False
    for step in transcript.steps:
        if step.model_response is None:
            continue
        metadata = step.model_response.provider_metadata
        for key in ("cost_usd", "provider_cost_usd", "cost"):
            value = metadata.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
                total += float(value)
                found = True
                break
    return total if found else None


def _base_result(row: SweepMatrixRow, attempt: int, started_at: datetime) -> dict[str, Any]:
    return {
        "logical_trial_id": row.logical_trial_id,
        "task_id": row.task_id,
        "task_revision": row.task_revision,
        "model_id": row.model_id,
        "condition_id": row.condition_id,
        "repeat": row.repeat,
        "infrastructure_attempt": attempt,
        "started_at": started_at,
    }


async def _execute_once(
    *,
    resolved: ResolvedAccessibilitySweep,
    row: SweepMatrixRow,
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
        and manifest.agent_class == row.agent_class
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
                "checker_detail": "run archive identity does not match the scheduled sweep trial",
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
    if row.agent_class == "vision":
        task_page_steps = {
            step.step
            for step in archived_transcript.steps
            if not step.url.rstrip("/").endswith("/submit")
        }
        reachable = any(
            record.step in task_page_steps
            and record.reachability is not None
            and record.reachability.visible
            for record in records
        )
    else:
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
                "checker_detail": "runner completed without a model-visible observation",
            }
        )
    if not capture_complete:
        return SweepTrialResult.model_validate(
            {
                **common,
                **hashes,
                **evidence,
                "classification": "EVIDENCE_FAILURE",
                "checker_detail": f"one or more {row.agent_class} captures are partial",
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


def _jsonl_models(path: Path, model: type[Any]) -> list[Any]:
    values: list[Any] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), start=1):
        try:
            values.append(model.model_validate_json(line))
        except Exception as exc:
            raise SweepConfigurationError(f"invalid {path}:{line_number}: {exc}") from exc
    return values


def _eligible_control_reuse(
    bundle: ResolvedSweepBundle,
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
    for result in results:
        if (
            result.classification != "PASS"
            or result.class_id != resolved.spec.agent_class
            or result.run_id is None
            or result.reachable is not True
            or result.capture_complete is not True
            or result.archive_valid is not True
        ):
            continue
        control_row = matrix_by_id.get(result.logical_trial_id)
        task = active.get((result.task_id, result.task_revision))
        if control_row is None or task is None or control_row.task_definition_sha256 != task.definition_sha256:
            continue
        run_dir = archive_root / "runs" / result.run_id
        scan = scan_run_observations(run_dir, require_complete=True)
        if any(item.severity == "error" for item in scan.diagnostics) or not scan.records:
            continue
        try:
            manifest = RunManifest.model_validate_json((run_dir / "manifest.json").read_bytes())
        except Exception:
            continue
        model = next((item for item in resolved.models if item.id == result.model_id), None)
        if (
            model is None
            or manifest.agent_class != resolved.spec.agent_class
            or manifest.input.task_prompt != task.revision.level0.prompt
            or manifest.input.model != model.runner_model
        ):
            continue
        candidates[(result.task_id, result.task_revision, result.model_id)].append(result)
    for values in candidates.values():
        values.sort(key=lambda item: (item.repeat, item.infrastructure_attempt, item.run_id or ""))
    return candidates


def _reused_result(row: SweepMatrixRow, control: ControlTrialResult) -> SweepTrialResult:
    assert control.run_id is not None
    return SweepTrialResult(
        logical_trial_id=row.logical_trial_id,
        source="control_reuse",
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
        archive_diagnostics=[],
        reachable=True,
        task_success=True,
        self_reported_blindness=False,
        capture_complete=True,
        checker_detail=f"eligible Plan 4 control reuse: {control.checker_detail}",
        manifest_sha256=control.manifest_sha256,
        transcript_sha256=control.transcript_sha256,
        input_tokens=control.input_tokens,
        output_tokens=control.output_tokens,
        actions=control.actions,
        started_at=control.started_at,
        finished_at=control.finished_at,
    )


def _surprise_candidate(
    resolved: ResolvedAccessibilitySweep,
    row: SweepMatrixRow,
    result: SweepTrialResult,
) -> SurpriseCandidate | None:
    condition = _condition_map(resolved)[row.condition_id]
    expected = condition.expected_reachability
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
        summary=(
            f"Expected marker {expected}; exact complete {resolved.spec.agent_class} observation "
            f"recorded it as {observed}."
        ),
        manifest_sha256=result.manifest_sha256,
    )


async def execute_resolved_sweep(
    bundle: ResolvedSweepBundle,
    *,
    archive_root: Path,
    output_root: Path,
    runner: Runner | None = None,
    preflight_credentials: bool = True,
) -> SweepReport:
    resolved = bundle.resolved
    if preflight_credentials:
        _preflight_credentials(resolved)
    actual_archive_root = archive_root.resolve()
    actual_output_root = output_root.resolve()
    output_dir = actual_output_root / resolved.spec.sweep_id
    if output_dir.parent.resolve() != actual_output_root:
        raise SweepConfigurationError("sweep output escaped output_root")
    try:
        output_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise SweepConfigurationError(f"sweep output already exists: {output_dir}") from exc

    paths = {
        name: output_dir / name
        for name in (
            "matrix.jsonl",
            "results.jsonl",
            "reused-controls.jsonl",
            "exclusions.jsonl",
            "surprise-candidates.jsonl",
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
        raise SweepConfigurationError("resolved condition inventory hash mismatch")

    matrix = expand_full_matrix(resolved)
    for row in matrix:
        append_jsonl(paths["matrix.jsonl"], row.model_dump(mode="json"))
    reused = _eligible_control_reuse(bundle, actual_archive_root)
    reuse_offsets: dict[tuple[str, int, str], int] = defaultdict(int)
    task_by_id = _task_map(resolved)
    condition_by_id = _condition_map(resolved)
    all_results: list[SweepTrialResult] = []
    final_results: dict[str, SweepTrialResult] = {}
    blocking: set[SweepClassification] = set()
    surprise_count = 0
    started_at = datetime.now(UTC)
    runner_impl = runner or _default_runner(resolved.spec.runner)

    with StructuralFixtureHarness(resolved.spec.harness, resolved.tasks, resolved.spec.conditions) as harness:
        harness.preflight()
        for row in matrix:
            task = task_by_id[(row.task_id, row.task_revision)]
            condition = condition_by_id[row.condition_id]
            reuse_key = (row.task_id, row.task_revision, row.model_id)
            reuse_values = reused.get(reuse_key, [])
            offset = reuse_offsets[reuse_key]
            if row.condition_id == "level-0" and offset < len(reuse_values):
                result = _reused_result(row, reuse_values[offset])
                reuse_offsets[reuse_key] += 1
                all_results.append(result)
                final_results[row.logical_trial_id] = result
                append_jsonl(paths["results.jsonl"], result.model_dump(mode="json"))
                append_jsonl(paths["reused-controls.jsonl"], result.model_dump(mode="json"))
                continue

            final: SweepTrialResult | None = None
            for attempt in range(resolved.spec.allocation.max_infrastructure_retries + 1):
                result = await _execute_once(
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
            candidate = _surprise_candidate(resolved, row, final)
            if candidate is not None:
                append_jsonl(paths["surprise-candidates.jsonl"], candidate.model_dump(mode="json"))
                surprise_count += 1

    aggregates = aggregate_cells(resolved, list(final_results.values()), include_reused_controls=True)
    live_only = aggregate_cells(resolved, list(final_results.values()), include_reused_controls=False)
    summaries = axis_summaries(resolved, aggregates)
    complete_cells = sum(item.complete for item in aggregates)
    required_cells = len(aggregates)
    if complete_cells == required_cells and not blocking:
        verdict = "complete"
    elif blocking - {"INFRASTRUCTURE_FAILURE"}:
        verdict = "blocked"
    else:
        verdict = "incomplete"

    aggregates_payload = {
        "cells": [item.model_dump(mode="json") for item in aggregates],
        "live_only_cells": [item.model_dump(mode="json") for item in live_only],
        "axis_summaries": [item.model_dump(mode="json") for item in summaries],
        "variance_decomposition_inputs": [
            {
                "agent_class": resolved.spec.agent_class,
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
    report = SweepReport(
        sweep_id=resolved.spec.sweep_id,
        verdict=verdict,
        resolved_spec_sha256=hashlib.sha256(resolved_bytes).hexdigest(),
        conditions_sha256=resolved.conditions_sha256,
        matrix_sha256=sha256_file(paths["matrix.jsonl"]),
        results_sha256=sha256_file(paths["results.jsonl"]),
        exclusions_sha256=sha256_file(paths["exclusions.jsonl"]),
        reused_controls_sha256=sha256_file(paths["reused-controls.jsonl"]),
        required_cells=required_cells,
        complete_cells=complete_cells,
        valid_trial_count=sum(result.counts_toward_cell for result in final_results.values()),
        attempt_count=len(all_results),
        blocking_classifications=sorted(blocking),
        aggregates=aggregates,
        live_only_aggregates=live_only,
        axis_summaries=summaries,
        architecture_variance_status=(
            f"This {resolved.spec.agent_class}-only sweep emits model-stratified variance inputs. "
            "Architecture-attributable variance is "
            "not estimable until an identical-condition second architecture sweep is joined."
        ),
        surprise_candidate_count=surprise_count,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
    report_bytes = canonical_json_bytes(report.model_dump(mode="json"))
    atomic_write(output_dir / "report.json", report_bytes)
    atomic_write(output_dir / "report.md", report_markdown(report))
    if report.verdict == "complete":
        receipt = SweepReceipt(
            sweep_id=report.sweep_id,
            resolved_spec_sha256=report.resolved_spec_sha256,
            conditions_sha256=report.conditions_sha256,
            matrix_sha256=report.matrix_sha256,
            results_sha256=report.results_sha256,
            exclusions_sha256=report.exclusions_sha256,
            reused_controls_sha256=report.reused_controls_sha256,
            report_sha256=hashlib.sha256(report_bytes).hexdigest(),
            model_roster=[item.runner_model for item in resolved.models],
            tasks=[f"{item.revision.task_id}:r{item.revision.revision}" for item in resolved.tasks],
            condition_ids=[item.id for item in resolved.spec.conditions],
            completed_at=report.finished_at,
        )
        atomic_write(output_dir / "sweep-receipt.json", canonical_json_bytes(receipt.model_dump(mode="json")))
    return report


async def run_accessibility_tree_sweep(
    spec_path: Path,
    *,
    archive_root: Path,
    output_root: Path,
    control_receipt: Path | None = None,
    runner: Runner | None = None,
    preflight_credentials: bool = True,
) -> SweepReport:
    """Resolve control admission and execute a full AX/vision/CDP allocation."""

    bundle = resolve_sweep_spec(spec_path, control_receipt_override=control_receipt)
    return await execute_resolved_sweep(
        bundle,
        archive_root=archive_root,
        output_root=output_root,
        runner=runner,
        preflight_credentials=preflight_credentials,
    )
