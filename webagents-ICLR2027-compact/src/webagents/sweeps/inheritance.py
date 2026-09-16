"""Hash-bound reuse of completed corrected DOM trials in a larger allocation."""

from __future__ import annotations

import hashlib
from pathlib import Path

from webagents.capture.archive import canonical_json_bytes
from webagents.io import sha256_file
from webagents.sweeps.config import SweepConfigurationError
from webagents.sweeps.schemas import (
    DomLightReport,
    DomSweepMatrixRow,
    RescoreReceipt,
    ResolvedDomLightSweep,
    SweepTrialResult,
)


def _jsonl(path: Path, model: type[SweepTrialResult]) -> list[SweepTrialResult]:
    values: list[SweepTrialResult] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), start=1):
        try:
            values.append(model.model_validate_json(line))
        except Exception as exc:
            raise SweepConfigurationError(f"invalid inheritance source {path}:{line_number}: {exc}") from exc
    return values


def _result_key(result: SweepTrialResult) -> tuple[str, int, str, str, int]:
    return (result.task_id, result.task_revision, result.model_id, result.condition_id, result.repeat)


def _row_key(row: DomSweepMatrixRow) -> tuple[str, int, str, str, int]:
    return (row.task_id, row.task_revision, row.model_id, row.condition_id, row.repeat)


def load_inherited_dom_results(
    source_dir: Path,
    *,
    resolved: ResolvedDomLightSweep,
    matrix: list[DomSweepMatrixRow],
) -> tuple[dict[str, SweepTrialResult], dict[str, object]]:
    """Return eligible prior results keyed by the new matrix logical IDs.

    Only a completed reachability-v2 correction can seed a larger unrestricted
    allocation. The correction receipt binds the exact result and report bytes;
    semantic checks below prevent reuse across tasks, models, conditions, or
    runtime limits.
    """

    source_dir = source_dir.resolve()
    if resolved.spec.observation_policy != "unrestricted":
        raise SweepConfigurationError("restricted-policy sweeps cannot inherit unrestricted DOM results")
    try:
        source_report_bytes = (source_dir / "report.json").read_bytes()
        source_report = DomLightReport.model_validate_json(source_report_bytes)
        source_resolved = ResolvedDomLightSweep.model_validate_json((source_dir / "resolved-spec.json").read_bytes())
        receipt_bytes = (source_dir / "rescore-receipt.json").read_bytes()
        receipt = RescoreReceipt.model_validate_json(receipt_bytes)
    except Exception as exc:
        raise SweepConfigurationError(f"cannot load corrected DOM inheritance source {source_dir}: {exc}") from exc
    if source_report.verdict != "complete" or receipt.verdict != "complete":
        raise SweepConfigurationError("DOM inheritance source must be complete")
    if receipt.agent_class != "dom_extraction" or receipt.scoring_policy != "initial-task-page-marker-v2":
        raise SweepConfigurationError("DOM inheritance source must use corrected reachability-v2 scoring")
    if hashlib.sha256(source_report_bytes).hexdigest() != receipt.corrected_report_sha256:
        raise SweepConfigurationError("DOM inheritance source report does not match its rescore receipt")
    if sha256_file(source_dir / "results.jsonl") != receipt.corrected_results_sha256:
        raise SweepConfigurationError("DOM inheritance source results do not match its rescore receipt")
    if source_resolved.conditions_sha256 != resolved.conditions_sha256:
        raise SweepConfigurationError("DOM inheritance source condition inventory differs from the extension")
    if source_resolved.tasks != resolved.tasks:
        raise SweepConfigurationError("DOM inheritance source task revisions differ from the extension")
    source_models = [source_resolved.primary_model, *source_resolved.additional_models]
    target_models = [resolved.primary_model, *resolved.additional_models]
    if source_models != target_models:
        raise SweepConfigurationError("DOM inheritance source model roster differs from the extension")
    if source_resolved.spec.runtime != resolved.spec.runtime:
        raise SweepConfigurationError("DOM inheritance source runtime limits differ from the extension")
    if source_resolved.spec.observation_policy != "unrestricted":
        raise SweepConfigurationError("DOM inheritance source was not an unrestricted observation run")

    prior = _jsonl(source_dir / "results.jsonl", SweepTrialResult)
    final_by_key: dict[tuple[str, int, str, str, int], SweepTrialResult] = {}
    for result in prior:
        if result.counts_toward_cell:
            final_by_key[_result_key(result)] = result

    inherited: dict[str, SweepTrialResult] = {}
    for row in matrix:
        result = final_by_key.get(_row_key(row))
        if result is None:
            continue
        inherited[row.logical_trial_id] = result.model_copy(
            update={
                "logical_trial_id": row.logical_trial_id,
                "allocation_role": row.allocation_role,
                "source": "sweep_inheritance",
            }
        )
    metadata: dict[str, object] = {
        "schema_version": "1.0",
        "source_dir": str(source_dir),
        "source_analysis_id": source_report.sweep_id,
        "source_rescore_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "source_corrected_results_sha256": receipt.corrected_results_sha256,
        "source_corrected_report_sha256": receipt.corrected_report_sha256,
        "inherited_trial_count": len(inherited),
        "semantic_binding_sha256": hashlib.sha256(
            canonical_json_bytes(
                {
                    "conditions_sha256": resolved.conditions_sha256,
                    "tasks": [item.model_dump(mode="json") for item in resolved.tasks],
                    "models": [item.model_dump(mode="json") for item in target_models],
                    "runtime": resolved.spec.runtime.model_dump(mode="json"),
                    "observation_policy": resolved.spec.observation_policy,
                }
            )
        ).hexdigest(),
    }
    return inherited, metadata
