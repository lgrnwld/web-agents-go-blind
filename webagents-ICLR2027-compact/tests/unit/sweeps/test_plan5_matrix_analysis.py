from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from webagents.control.config import load_all_task_revisions, load_control_spec
from webagents.control.schemas import ControlReceipt, ImplementationIdentity, ModelSpec, TaskIdentity
from webagents.sweeps.aggregate import aggregate_cells, axis_summaries, wilson_interval
from webagents.sweeps.config import load_sweep_spec
from webagents.sweeps.matrix import expand_full_matrix, validate_full_matrix
from webagents.sweeps.schemas import (
    ResolvedAccessibilitySweep,
    SweepTrialResult,
)

ROOT = Path(__file__).resolve().parents[3]
SHA = "a" * 64


def _resolved(*, two_models: bool = True) -> ResolvedAccessibilitySweep:
    spec = load_sweep_spec(ROOT / "benchmarks/sweeps/accessibility-tree-full.yaml")
    control = load_control_spec(ROOT / "benchmarks/control/control.yaml")
    tasks = load_all_task_revisions(ROOT / "benchmarks/control/control.yaml", control)
    active = [tasks[(item.id, item.active_revision)] for item in control.tasks]
    models = [control.models[0]]
    if two_models:
        models.append(
            ModelSpec(
                id="openrouter-fixture",
                runner_model="openrouter/fixture/model-v1",
                expected_provider="openrouter",
                expected_model="fixture/model-v1",
            )
        )
    implementation = ImplementationIdentity(
        source_sha256=SHA,
        transcript_schema_version="1.0",
        archive_schema_version="1.0",
        dom_framework="browser-use/test",
        dom_prompt_version="test",
        ax_framework="playwright/test",
        ax_prompt_version="test",
    )
    receipt = ControlReceipt(
        validation_id="TEST-CONTROL",
        control_spec_sha256=SHA,
        matrix_sha256=SHA,
        results_sha256=SHA,
        implementation=implementation,
        classes=["dom_extraction", "accessibility_tree"],
        models=[model.runner_model for model in models],
        tasks=[TaskIdentity(id=task.revision.task_id, revision=task.revision.revision) for task in active],
        policy={"repeats": 3, "required_successes": 3},
        passed_at=datetime.now(UTC),
    )
    return ResolvedAccessibilitySweep(
        spec=spec,
        control_receipt=receipt,
        control_receipt_sha256=SHA,
        control_spec_sha256=SHA,
        implementation=implementation,
        models=models,
        tasks=active,
        conditions_sha256=SHA,
        resolved_at=datetime.now(UTC),
    )


def _valid_result(
    *,
    task_id: str,
    task_revision: int,
    model_id: str,
    condition_id: str,
    repeat: int,
    reachable: bool,
    successful: bool,
) -> SweepTrialResult:
    started = datetime.now(UTC)
    return SweepTrialResult(
        logical_trial_id=f"trial-{task_id}-{model_id}-{condition_id}-{repeat}",
        task_id=task_id,
        task_revision=task_revision,
        model_id=model_id,
        condition_id=condition_id,
        repeat=repeat,
        infrastructure_attempt=0,
        run_id=f"run-{task_id}-{model_id}-{condition_id}-{repeat}",
        classification="VALID_OUTCOME",
        counts_toward_cell=True,
        runner_status="success" if successful else "failure",
        runner_success_claimed=successful,
        archive_valid=True,
        reachable=reachable,
        task_success=successful,
        self_reported_blindness=not reachable,
        capture_complete=True,
        input_tokens=100,
        output_tokens=10,
        actions=1,
        started_at=started,
        finished_at=started + timedelta(seconds=2),
    )


def test_frozen_inventory_and_full_matrix_are_complete_and_deterministic() -> None:
    resolved = _resolved()
    first = expand_full_matrix(resolved)
    second = expand_full_matrix(resolved)
    assert first == second
    assert len(resolved.spec.conditions) == 11
    assert len(first) == 2 * 2 * 11 * 10
    assert len({row.logical_trial_id for row in first}) == len(first)
    counts = Counter((row.task_id, row.model_id, row.condition_id) for row in first)
    assert set(counts.values()) == {10}
    assert {row.repeat for row in first} == set(range(1, 11))

    with pytest.raises(ValueError, match="expected"):
        validate_full_matrix(resolved, first[:-1])


def test_plan5_schema_refuses_underpowered_or_ambiguous_conditions() -> None:
    spec = load_sweep_spec(ROOT / "benchmarks/sweeps/accessibility-tree-full.yaml")
    underpowered = spec.model_dump(mode="json")
    underpowered["allocation"]["valid_trials_per_cell"] = 9
    with pytest.raises(ValidationError, match="exactly 10"):
        type(spec).model_validate(underpowered)

    with pytest.raises(ValidationError, match="origin and depth"):
        type(spec.conditions[0])(id="broken-frame", kind="iframe", iframe_depth=1)


def test_wilson_joint_outcomes_and_axis_summaries_are_model_stratified() -> None:
    resolved = _resolved(two_models=False)
    task = resolved.tasks[0]
    model = resolved.models[0]
    results: list[SweepTrialResult] = []
    for condition in resolved.spec.conditions:
        for repeat in range(1, 11):
            reachable = condition.id == "level-0"
            results.append(
                _valid_result(
                    task_id=task.revision.task_id,
                    task_revision=task.revision.revision,
                    model_id=model.id,
                    condition_id=condition.id,
                    repeat=repeat,
                    reachable=reachable,
                    successful=reachable,
                )
            )
    aggregates = aggregate_cells(resolved, results, include_reused_controls=True)
    baseline = next(item for item in aggregates if item.condition_id == "level-0")
    canvas = next(item for item in aggregates if item.condition_id == "rendered-canvas")
    assert baseline.reachability.rate == 1.0
    assert baseline.reachability.lower == pytest.approx(0.722467, rel=1e-5)
    assert baseline.joint_outcomes["reachable_success"] == 10
    assert canvas.reachability.rate == 0.0
    assert canvas.joint_outcomes["unreachable_failure"] == 10
    summaries = axis_summaries(resolved, aggregates)
    rendered = next(item for item in summaries if item.axis_id == "rendered-target")
    assert rendered.reachability_rates == [1.0, 0.0]
    assert rendered.reachability_auc == 0.5
    assert rendered.mean_reachability_drop == 1.0
    assert wilson_interval(0, 0).rate is None
