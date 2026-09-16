"""Orchestrate the complete level-0 matrix and emit a pass-only receipt."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webagents.capture.archive import canonical_json_bytes, iter_model_observations, verify_run
from webagents.control.config import (
    ControlConfigurationError,
    ControlSpecError,
    load_all_task_revisions,
    load_control_spec,
)
from webagents.control.harness import FixtureHarness, HarnessError
from webagents.control.matrix import expand_matrix
from webagents.control.receipt import implementation_identity
from webagents.control.schemas import (
    CellResult,
    ControlReceipt,
    ControlSpec,
    ControlValidationReport,
    FailureClassification,
    MatrixRow,
    ResolvedTask,
    TaskIdentity,
    TaskResult,
    TrialResult,
)
from webagents.io import append_jsonl, atomic_write, sha256_file
from webagents.providers.config import ProviderConfigurationError, validate_provider_environment
from webagents.runtime import generate_run_id, project_root
from webagents.schemas import RUN_ID_PATTERN, RunManifest, RunTranscript

Runner = Callable[..., Awaitable[RunTranscript]]
def _default_runners() -> dict[str, Runner]:
    from webagents.ax.runner import run_ax_agent
    from webagents.cdp.runner import run_cdp_agent
    from webagents.dom.runner import run_dom_agent
    from webagents.vision.runner import run_vision_agent

    return {"dom": run_dom_agent, "ax": run_ax_agent, "vision": run_vision_agent, "cdp": run_cdp_agent}


def _preflight_credentials(spec: ControlSpec) -> None:
    try:
        validate_provider_environment(model.runner_model for model in spec.models)
    except ProviderConfigurationError as exc:
        raise ControlConfigurationError(f"invalid frozen model roster environment: {exc}") from exc


def _resolved_spec_payload(
    spec: ControlSpec,
    all_tasks: dict[tuple[str, int], ResolvedTask],
) -> dict[str, Any]:
    return {
        "control": spec.model_dump(mode="json"),
        "task_revisions": [
            task.model_dump(mode="json") for _, task in sorted(all_tasks.items(), key=lambda item: item[0])
        ],
    }


def _run_id(validation_id: str, row: MatrixRow, attempt: int) -> str:
    validation_digest = hashlib.sha256(validation_id.encode("utf-8")).hexdigest()[:12]
    return f"L0-{validation_digest}-{row.logical_trial_id}-A{attempt}"


def _result_base(row: MatrixRow, attempt: int, started_at: datetime) -> dict[str, Any]:
    return {
        "logical_trial_id": row.logical_trial_id,
        "class_id": row.class_id,
        "task_id": row.task_id,
        "task_revision": row.task_revision,
        "model_id": row.model_id,
        "repeat": row.repeat,
        "infrastructure_attempt": attempt,
        "started_at": started_at,
    }


async def _execute_once(
    *,
    validation_id: str,
    row: MatrixRow,
    task: ResolvedTask,
    spec: ControlSpec,
    archive_root: Path,
    harness: FixtureHarness,
    runners: Mapping[str, Runner],
    attempt: int,
) -> TrialResult:
    started_at = datetime.now(UTC)
    base = _result_base(row, attempt, started_at)
    try:
        harness.reset(task)
    except HarnessError as exc:
        return TrialResult.model_validate(
            {
                **base,
                "classification": "FIXTURE_FAILURE",
                "checker_detail": str(exc),
                "finished_at": datetime.now(UTC),
            }
        )

    run_id = _run_id(validation_id, row, attempt)
    page_url = harness.url(task.revision.level0.page_path)
    try:
        transcript = await runners[row.runner](
            page_url,
            task.revision.level0.prompt,
            row.runner_model,
            run_id=run_id,
            max_steps=spec.policy.max_steps,
            archive_root=archive_root,
            timeout_seconds=spec.policy.timeout_seconds,
        )
    except Exception as exc:
        return TrialResult.model_validate(
            {
                **base,
                "run_id": run_id,
                "classification": "INFRASTRUCTURE_FAILURE",
                "checker_detail": f"runner raised {type(exc).__name__}: {exc}",
                "finished_at": datetime.now(UTC),
            }
        )

    run_dir = archive_root / "runs" / run_id
    diagnostics = verify_run(run_dir, require_complete=True)
    error_diagnostics = [f"{item.code}:{item.path}:{item.message}" for item in diagnostics if item.severity == "error"]
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
        "finished_at": datetime.now(UTC),
    }
    if error_diagnostics:
        return TrialResult.model_validate({**common, "classification": "EVIDENCE_FAILURE"})

    try:
        manifest_bytes = (run_dir / "manifest.json").read_bytes()
        transcript_bytes = (run_dir / "transcript.json").read_bytes()
        archived_manifest = RunManifest.model_validate_json(manifest_bytes)
        archived_transcript = RunTranscript.model_validate_json(transcript_bytes)
    except Exception as exc:
        return TrialResult.model_validate(
            {
                **common,
                "classification": "EVIDENCE_FAILURE",
                "checker_detail": f"cannot load verified archive: {exc}",
            }
        )
    identity_matches = (
        archived_manifest.run_id == run_id
        and archived_manifest.agent_class == row.class_id
        and archived_manifest.input.page_url == page_url
        and archived_manifest.input.task_prompt == task.revision.level0.prompt
        and archived_manifest.input.model == row.runner_model
        and archived_transcript.input == archived_manifest.input
    )
    hashes = {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
    }
    if not identity_matches:
        return TrialResult.model_validate(
            {
                **common,
                **hashes,
                "classification": "CONFIGURATION_FAILURE",
                "checker_detail": "run archive identity does not match the scheduled trial",
            }
        )
    if archived_transcript.status == "infrastructure_error":
        return TrialResult.model_validate(
            {
                **common,
                **hashes,
                "classification": "INFRASTRUCTURE_FAILURE",
                "checker_detail": archived_transcript.final.error,
            }
        )

    records = [
        record for record in iter_model_observations(archive_root, require_complete=True) if record.run_id == run_id
    ]
    capture_complete = bool(records) and all(record.capture_completeness == "complete" for record in records)
    marker = task.revision.level0.reachability_marker.encode("utf-8")
    if row.class_id == "vision":
        reachable = any(record.reachability is not None and record.reachability.visible for record in records)
    else:
        reachable = any(marker in record.observation_bytes for record in records)
    evidence = {"capture_complete": capture_complete, "reachable": reachable}
    if not records:
        return TrialResult.model_validate(
            {
                **common,
                **hashes,
                **evidence,
                "classification": "INFRASTRUCTURE_FAILURE",
                "checker_detail": "runner completed without a model-visible observation",
            }
        )
    if not capture_complete:
        return TrialResult.model_validate(
            {
                **common,
                **hashes,
                **evidence,
                "classification": "EVIDENCE_FAILURE",
                "checker_detail": "one or more level-0 captures are partial",
            }
        )
    if not reachable:
        return TrialResult.model_validate(
            {
                **common,
                **hashes,
                **evidence,
                "classification": "FIXTURE_FAILURE",
                "checker_detail": (
                    "the target is outside the exact screenshot viewport"
                    if row.class_id == "vision"
                    else "reachability marker is absent from exact observation bytes"
                ),
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
        return TrialResult.model_validate(
            {
                **common,
                **hashes,
                **evidence,
                "classification": "FIXTURE_FAILURE",
                "checker_detail": str(exc),
            }
        )
    passed = archived_transcript.status == "success" and archived_transcript.final.success_claimed and checker_passed
    return TrialResult.model_validate(
        {
            **common,
            **hashes,
            **evidence,
            "checker_passed": checker_passed,
            "checker_detail": checker_detail,
            "classification": "PASS" if passed else "TASK_FAILURE",
        }
    )


def _aggregate_task(
    task: ResolvedTask,
    rows: list[MatrixRow],
    final_results: dict[str, TrialResult],
    spec: ControlSpec,
    *,
    next_revision: int | None,
) -> TaskResult:
    cells: list[CellResult] = []
    for agent_class in spec.classes:
        for model in spec.models:
            cell_rows = [
                row
                for row in rows
                if row.task_id == task.revision.task_id and row.class_id == agent_class.id and row.model_id == model.id
            ]
            results = [final_results[row.logical_trial_id] for row in cell_rows]
            counts = Counter(result.classification for result in results)
            successes = counts["PASS"]
            cells.append(
                CellResult(
                    class_id=agent_class.id,
                    task_id=task.revision.task_id,
                    task_revision=task.revision.revision,
                    model_id=model.id,
                    successes=successes,
                    required_successes=spec.policy.required_successes,
                    trials=len(results),
                    admitted=successes == spec.policy.required_successes and len(results) == spec.policy.repeats,
                    classifications=dict(counts),
                )
            )
    admitted = all(cell.admitted for cell in cells)
    return TaskResult(
        task_id=task.revision.task_id,
        task_revision=task.revision.revision,
        admitted=admitted,
        next_revision=None if admitted else next_revision,
        cells=cells,
    )


def _report_markdown(report: ControlValidationReport) -> bytes:
    lines = [
        f"# Level-0 control validation {report.validation_id}",
        "",
        f"Verdict: **{report.verdict}**",
        "",
        "| Task revision | Admitted | Next revision |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {item.task_id} r{item.task_revision} | {'yes' if item.admitted else 'no'} | {item.next_revision or '—'} |"
        for item in report.task_results
    )
    lines.extend(["", f"Trials and attempts recorded: {report.trial_count}", ""])
    return "\n".join(lines).encode("utf-8")


async def validate_level0_control(
    spec_path: Path,
    *,
    archive_root: Path,
    output_root: Path,
    validation_id: str | None = None,
    runners: Mapping[str, Runner] | None = None,
    preflight_credentials: bool = True,
) -> ControlValidationReport:
    """Run the strict gate; live-provider calls occur only through this explicit API."""

    spec_path = spec_path.resolve()
    spec = load_control_spec(spec_path)
    all_tasks = load_all_task_revisions(spec_path, spec)
    if preflight_credentials:
        _preflight_credentials(spec)
    actual_validation_id = validation_id or generate_run_id()
    if not RUN_ID_PATTERN.fullmatch(actual_validation_id):
        raise ControlSpecError("validation_id contains unsafe characters or is too long")
    resolved_output_root = output_root.resolve()
    output_dir = resolved_output_root / actual_validation_id
    if output_dir.parent.resolve() != resolved_output_root:
        raise ControlConfigurationError("validation output escaped output_root")
    try:
        output_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise ControlConfigurationError(f"validation output already exists: {output_dir}") from exc
    matrix_path = output_dir / "matrix.jsonl"
    results_path = output_dir / "results.jsonl"
    atomic_write(matrix_path, b"")
    atomic_write(results_path, b"")

    resolved_payload = _resolved_spec_payload(spec, all_tasks)
    resolved_bytes = canonical_json_bytes(resolved_payload)
    atomic_write(output_dir / "resolved-spec.json", resolved_bytes)
    control_spec_sha256 = hashlib.sha256(resolved_bytes).hexdigest()
    implementation = implementation_identity(project_root())
    runner_map = runners or _default_runners()
    expected_runners = {item.runner for item in spec.classes}
    if set(runner_map) != expected_runners:
        raise ControlConfigurationError(f"runner map must contain exactly {sorted(expected_runners)}")

    active = {item.id: item.active_revision for item in spec.tasks}
    ladder_positions = {item.id: 0 for item in spec.tasks}
    task_results: list[TaskResult] = []
    all_attempts: list[TrialResult] = []
    phase = 0
    schedule_index = 0
    verdict: str = "blocked"
    blocking_classifications: set[FailureClassification] = set()

    with FixtureHarness(spec.harness, all_tasks.values()) as harness:
        for task in all_tasks.values():
            harness.preflight_task(task)

        pending_ids = set(active)
        while pending_ids:
            phase_tasks = [all_tasks[(task_id, active[task_id])] for task_id in sorted(pending_ids)]
            rows = expand_matrix(spec, phase_tasks, phase=phase, start_index=schedule_index)
            schedule_index += len(rows)
            for row in rows:
                append_jsonl(matrix_path, row.model_dump(mode="json"))

            final_results: dict[str, TrialResult] = {}
            for row in rows:
                task = all_tasks[(row.task_id, row.task_revision)]
                final: TrialResult | None = None
                for attempt in range(spec.policy.max_infrastructure_retries + 1):
                    result = await _execute_once(
                        validation_id=actual_validation_id,
                        row=row,
                        task=task,
                        spec=spec,
                        archive_root=archive_root.resolve(),
                        harness=harness,
                        runners=runner_map,
                        attempt=attempt,
                    )
                    all_attempts.append(result)
                    append_jsonl(results_path, result.model_dump(mode="json"))
                    final = result
                    if result.classification != "INFRASTRUCTURE_FAILURE":
                        break
                assert final is not None
                final_results[row.logical_trial_id] = final

            next_pending: set[str] = set()
            blocked = False
            for task in phase_tasks:
                roster = task.roster
                position = ladder_positions[roster.id]
                next_revision = (
                    roster.simplification_ladder[position] if position < len(roster.simplification_ladder) else None
                )
                task_result = _aggregate_task(
                    task,
                    rows,
                    final_results,
                    spec,
                    next_revision=next_revision,
                )
                task_results.append(task_result)
                phase_final = [final_results[row.logical_trial_id] for row in rows if row.task_id == roster.id]
                classifications: set[FailureClassification] = {result.classification for result in phase_final}
                if classifications - {"PASS", "TASK_FAILURE"}:
                    blocked = True
                    task_result.next_revision = None
                    blocking_classifications.update(classifications - {"PASS", "TASK_FAILURE"})
                elif not task_result.admitted:
                    if next_revision is None:
                        verdict = "task_failure"
                    else:
                        active[roster.id] = next_revision
                        ladder_positions[roster.id] += 1
                        next_pending.add(roster.id)
            if blocked:
                verdict = "blocked"
                break
            final_phase_results = task_results[-len(phase_tasks) :]
            if any(not result.admitted and result.next_revision is None for result in final_phase_results):
                verdict = "task_failure"
                break
            pending_ids = next_pending
            phase += 1
        else:
            verdict = "pass"

    matrix_sha256 = sha256_file(matrix_path)
    results_sha256 = sha256_file(results_path)
    passed_at = datetime.now(UTC) if verdict == "pass" else None
    report = ControlValidationReport(
        validation_id=actual_validation_id,
        verdict=verdict,
        control_spec_sha256=control_spec_sha256,
        matrix_sha256=matrix_sha256,
        results_sha256=results_sha256,
        implementation=implementation,
        active_tasks=[TaskIdentity(id=task_id, revision=revision) for task_id, revision in sorted(active.items())],
        task_results=task_results,
        blocking_classifications=sorted(blocking_classifications),
        trial_count=len(all_attempts),
        passed_at=passed_at,
    )
    atomic_write(output_dir / "report.json", canonical_json_bytes(report.model_dump(mode="json")))
    atomic_write(output_dir / "report.md", _report_markdown(report))
    if report.verdict == "pass" and report.passed_at is not None:
        receipt = ControlReceipt(
            validation_id=report.validation_id,
            control_spec_sha256=report.control_spec_sha256,
            matrix_sha256=report.matrix_sha256,
            results_sha256=report.results_sha256,
            implementation=report.implementation,
            classes=[item.id for item in spec.classes],
            models=[item.runner_model for item in spec.models],
            tasks=report.active_tasks,
            policy={"repeats": spec.policy.repeats, "required_successes": spec.policy.required_successes},
            passed_at=report.passed_at,
        )
        atomic_write(output_dir / "control-receipt.json", canonical_json_bytes(receipt.model_dump(mode="json")))
    return report
