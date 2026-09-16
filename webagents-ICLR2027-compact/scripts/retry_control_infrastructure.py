#!/usr/bin/env python3
"""Recover one immutable control validation by retrying only archived infrastructure timeouts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webagents.capture.archive import canonical_json_bytes, verify_run
from webagents.control.config import load_all_task_revisions, load_control_spec
from webagents.control.harness import FixtureHarness
from webagents.control.receipt import implementation_identity
from webagents.control.schemas import (
    ControlReceipt,
    ControlSpec,
    ControlValidationReport,
    FailureClassification,
    MatrixRow,
    TaskIdentity,
    TrialResult,
)
from webagents.control.validator import (
    Runner,
    _aggregate_task,
    _default_runners,
    _execute_once,
    _preflight_credentials,
    _report_markdown,
    _resolved_spec_payload,
)
from webagents.io import append_jsonl, atomic_write, sha256_file
from webagents.runtime import project_root
from webagents.schemas import RunTranscript


class RecoveryError(RuntimeError):
    """The source validation cannot be safely recovered."""


def _jsonl(path: Path, model: type[MatrixRow] | type[TrialResult]) -> list[Any]:
    values: list[Any] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            values.append(model.model_validate_json(line))
        except Exception as exc:
            raise RecoveryError(f"invalid {path.name} line {line_number}: {exc}") from exc
    return values


def _retryable_timeout(result: TrialResult, archive_root: Path) -> bool:
    if result.runner_status != "timeout" or result.run_id is None:
        return False
    run_dir = archive_root / "runs" / result.run_id
    if any(item.severity == "error" for item in verify_run(run_dir, require_complete=True)):
        return False
    try:
        transcript = RunTranscript.model_validate_json((run_dir / "transcript.json").read_bytes())
    except Exception:
        return False
    return (
        transcript.status == "timeout"
        and transcript.final.success_claimed is False
        and isinstance(transcript.final.error, str)
        and transcript.final.error.startswith("wall-clock timeout after ")
    )


def _as_infrastructure_timeout(result: TrialResult) -> TrialResult:
    if result.runner_status != "timeout":
        return result
    return result.model_copy(
        update={
            "classification": "INFRASTRUCTURE_FAILURE",
            "checker_detail": "runner wall-clock timeout; eligible for the frozen infrastructure retry policy",
        }
    )


def audit_source(
    source_dir: Path,
    *,
    spec_path: Path,
    archive_root: Path,
) -> tuple[
    ControlValidationReport,
    ControlSpec,
    dict[tuple[str, int], Any],
    list[MatrixRow],
    list[TrialResult],
    list[MatrixRow],
]:
    source_dir = source_dir.resolve()
    report = ControlValidationReport.model_validate_json((source_dir / "report.json").read_bytes())
    resolved_bytes = (source_dir / "resolved-spec.json").read_bytes()
    matrix_path = source_dir / "matrix.jsonl"
    results_path = source_dir / "results.jsonl"
    if hashlib.sha256(resolved_bytes).hexdigest() != report.control_spec_sha256:
        raise RecoveryError("source resolved-spec hash does not match its report")
    if sha256_file(matrix_path) != report.matrix_sha256 or sha256_file(results_path) != report.results_sha256:
        raise RecoveryError("source matrix/results hashes do not match its report")

    spec = load_control_spec(spec_path)
    tasks = load_all_task_revisions(spec_path, spec)
    current_resolved = canonical_json_bytes(_resolved_spec_payload(spec, tasks))
    if current_resolved != resolved_bytes:
        raise RecoveryError("current control/task specs differ from the source validation")
    if implementation_identity(project_root()) != report.implementation:
        raise RecoveryError("current hashed runner implementation differs from the source validation")

    matrix = _jsonl(matrix_path, MatrixRow)
    attempts = _jsonl(results_path, TrialResult)
    by_logical: dict[str, list[TrialResult]] = defaultdict(list)
    for result in attempts:
        by_logical[result.logical_trial_id].append(result)
    if {row.logical_trial_id for row in matrix} != set(by_logical):
        raise RecoveryError("source results do not cover exactly the source matrix")

    candidates: list[MatrixRow] = []
    for row in matrix:
        ordered = sorted(by_logical[row.logical_trial_id], key=lambda item: item.infrastructure_attempt)
        final = ordered[-1]
        if final.classification == "PASS":
            continue
        if _retryable_timeout(final, archive_root.resolve()):
            if final.infrastructure_attempt >= spec.policy.max_infrastructure_retries:
                raise RecoveryError(f"{row.logical_trial_id} exhausted the frozen infrastructure retry limit")
            candidates.append(row)
            continue
        raise RecoveryError(
            f"source contains a non-recoverable final result: {row.logical_trial_id} {final.classification}"
        )
    if not candidates:
        raise RecoveryError("source validation has no recoverable wall-clock timeout")
    return report, spec, tasks, matrix, attempts, candidates


async def recover_validation(
    source_dir: Path,
    *,
    spec_path: Path,
    archive_root: Path,
    output_root: Path,
    runners: dict[str, Runner] | None = None,
    preflight_credentials: bool = True,
) -> ControlValidationReport:
    report, spec, tasks, matrix, attempts, candidates = audit_source(
        source_dir,
        spec_path=spec_path,
        archive_root=archive_root,
    )
    if preflight_credentials:
        _preflight_credentials(spec)
    recovery_id = f"{report.validation_id}-infra-retry-v1"
    output_dir = output_root.resolve() / recovery_id
    try:
        output_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise RecoveryError(f"recovery output already exists: {output_dir}") from exc

    source_dir = source_dir.resolve()
    atomic_write(output_dir / "resolved-spec.json", (source_dir / "resolved-spec.json").read_bytes())
    atomic_write(output_dir / "matrix.jsonl", (source_dir / "matrix.jsonl").read_bytes())
    atomic_write(output_dir / "results.jsonl", (source_dir / "results.jsonl").read_bytes())
    lineage: dict[str, Any] = {
        "schema_version": "1.0",
        "source_validation_id": report.validation_id,
        "source_control_spec_sha256": report.control_spec_sha256,
        "source_matrix_sha256": report.matrix_sha256,
        "source_results_sha256": report.results_sha256,
        "correction": "runner-timeout-is-infrastructure-v1",
        "retried_logical_trial_ids": [row.logical_trial_id for row in candidates],
    }
    atomic_write(output_dir / "infrastructure-recovery.json", canonical_json_bytes(lineage))

    by_logical: dict[str, list[TrialResult]] = defaultdict(list)
    for result in attempts:
        by_logical[result.logical_trial_id].append(result)
    final_results = {
        logical_id: sorted(values, key=lambda item: item.infrastructure_attempt)[-1]
        for logical_id, values in by_logical.items()
    }
    runner_map = runners or _default_runners()
    active_tasks = [tasks[(item.id, item.revision)] for item in report.active_tasks]
    with FixtureHarness(spec.harness, tasks.values()) as harness:
        for task in tasks.values():
            harness.preflight_task(task)
        for row in candidates:
            task = tasks[(row.task_id, row.task_revision)]
            prior_attempt = max(item.infrastructure_attempt for item in by_logical[row.logical_trial_id])
            final: TrialResult | None = None
            for attempt in range(prior_attempt + 1, spec.policy.max_infrastructure_retries + 1):
                raw = await _execute_once(
                    validation_id=recovery_id,
                    row=row,
                    task=task,
                    spec=spec,
                    archive_root=archive_root.resolve(),
                    harness=harness,
                    runners=runner_map,
                    attempt=attempt,
                )
                final = _as_infrastructure_timeout(raw)
                attempts.append(final)
                append_jsonl(output_dir / "results.jsonl", final.model_dump(mode="json"))
                if final.classification != "INFRASTRUCTURE_FAILURE":
                    break
            if final is None:
                raise RecoveryError(f"no retry budget remained for {row.logical_trial_id}")
            final_results[row.logical_trial_id] = final

    task_results = [_aggregate_task(task, matrix, final_results, spec, next_revision=None) for task in active_tasks]
    final_classifications: set[FailureClassification] = {result.classification for result in final_results.values()}
    if all(item.admitted for item in task_results):
        verdict = "pass"
        blocking: list[FailureClassification] = []
    elif "INFRASTRUCTURE_FAILURE" in final_classifications:
        verdict = "blocked"
        blocking = ["INFRASTRUCTURE_FAILURE"]
    else:
        verdict = "task_failure"
        blocking = []

    passed_at = datetime.now(UTC) if verdict == "pass" else None
    recovered = ControlValidationReport(
        validation_id=recovery_id,
        verdict=verdict,
        control_spec_sha256=report.control_spec_sha256,
        matrix_sha256=sha256_file(output_dir / "matrix.jsonl"),
        results_sha256=sha256_file(output_dir / "results.jsonl"),
        implementation=report.implementation,
        active_tasks=[TaskIdentity(id=item.id, revision=item.revision) for item in report.active_tasks],
        task_results=task_results,
        blocking_classifications=blocking,
        trial_count=len(attempts),
        passed_at=passed_at,
    )
    atomic_write(output_dir / "report.json", canonical_json_bytes(recovered.model_dump(mode="json")))
    atomic_write(output_dir / "report.md", _report_markdown(recovered))
    if passed_at is not None:
        receipt = ControlReceipt(
            validation_id=recovery_id,
            control_spec_sha256=recovered.control_spec_sha256,
            matrix_sha256=recovered.matrix_sha256,
            results_sha256=recovered.results_sha256,
            implementation=recovered.implementation,
            classes=[item.id for item in spec.classes],
            models=[item.runner_model for item in spec.models],
            tasks=recovered.active_tasks,
            policy={"repeats": spec.policy.repeats, "required_successes": spec.policy.required_successes},
            passed_at=passed_at,
        )
        atomic_write(output_dir / "control-receipt.json", canonical_json_bytes(receipt.model_dump(mode="json")))
    return recovered


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("--spec", type=Path, default=Path("benchmarks/control/control.yaml"))
    parser.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/control"))
    parser.add_argument("--plan", action="store_true", help="audit and print retry candidates without API calls")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.plan:
            report, _, _, _, _, candidates = audit_source(
                args.source_dir,
                spec_path=args.spec,
                archive_root=args.archive_root,
            )
            print(
                json.dumps(
                    {
                        "source_validation_id": report.validation_id,
                        "recoverable_trials": len(candidates),
                        "logical_trial_ids": [row.logical_trial_id for row in candidates],
                        "paid_calls_made": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        recovered = asyncio.run(
            recover_validation(
                args.source_dir,
                spec_path=args.spec,
                archive_root=args.archive_root,
                output_root=args.output_root,
            )
        )
    except Exception as exc:
        print(json.dumps({"error": "control_recovery_failed", "detail": str(exc)}, sort_keys=True))
        return 2
    print(canonical_json_bytes(recovered.model_dump(mode="json")).decode("utf-8"))
    return 0 if recovered.verdict == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
