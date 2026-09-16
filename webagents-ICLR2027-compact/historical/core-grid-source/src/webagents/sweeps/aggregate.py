"""Statistical summaries for full-allocation structural sweeps."""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import fmean

from webagents.control.schemas import ModelSpec, ResolvedTask
from webagents.sweeps.schemas import (
    AxisSummary,
    CellAggregate,
    ModelConfirmation,
    ResolvedAccessibilitySweep,
    ResolvedDomLightSweep,
    StructuralCondition,
    SweepAxis,
    SweepTrialResult,
    WilsonInterval,
)

_Z_95 = 1.959963984540054


def wilson_interval(successes: int, trials: int) -> WilsonInterval:
    if successes < 0 or trials < 0 or successes > trials:
        raise ValueError("Wilson interval requires 0 <= successes <= trials")
    if trials == 0:
        return WilsonInterval(successes=0, trials=0)
    rate = successes / trials
    z2 = _Z_95**2
    denominator = 1 + z2 / trials
    center = (rate + z2 / (2 * trials)) / denominator
    margin = _Z_95 * math.sqrt(rate * (1 - rate) / trials + z2 / (4 * trials**2)) / denominator
    return WilsonInterval(
        successes=successes,
        trials=trials,
        rate=rate,
        lower=max(0.0, center - margin),
        upper=min(1.0, center + margin),
    )


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def aggregate_cells(
    resolved: ResolvedAccessibilitySweep,
    results: list[SweepTrialResult],
    *,
    include_reused_controls: bool,
) -> list[CellAggregate]:
    return _aggregate_selected_cells(
        tasks=resolved.tasks,
        models=resolved.models,
        conditions=resolved.spec.conditions,
        required_by_model={model.id: resolved.spec.allocation.valid_trials_per_cell for model in resolved.models},
        results=results,
        include_reused_controls=include_reused_controls,
    )


def aggregate_dom_primary(
    resolved: ResolvedDomLightSweep,
    results: list[SweepTrialResult],
    *,
    include_reused_controls: bool,
) -> list[CellAggregate]:
    return _aggregate_selected_cells(
        tasks=resolved.tasks,
        models=[resolved.primary_model],
        conditions=resolved.spec.conditions,
        required_by_model={resolved.primary_model.id: resolved.spec.allocation.primary_valid_trials_per_cell},
        results=results,
        include_reused_controls=include_reused_controls,
    )


def aggregate_dom_additional(
    resolved: ResolvedDomLightSweep,
    results: list[SweepTrialResult],
    *,
    include_reused_controls: bool,
) -> list[CellAggregate]:
    """Aggregate fully repeated additional-model cells when the allocation is N=10."""

    return _aggregate_selected_cells(
        tasks=resolved.tasks,
        models=resolved.additional_models,
        conditions=resolved.spec.conditions,
        required_by_model={
            model.id: resolved.spec.allocation.additional_valid_trials_per_cell
            for model in resolved.additional_models
        },
        results=results,
        include_reused_controls=include_reused_controls,
    )


def _aggregate_selected_cells(
    *,
    tasks: list[ResolvedTask],
    models: list[ModelSpec],
    conditions: list[StructuralCondition],
    required_by_model: dict[str, int],
    results: list[SweepTrialResult],
    include_reused_controls: bool,
) -> list[CellAggregate]:
    grouped: dict[tuple[str, int, str, str], list[SweepTrialResult]] = defaultdict(list)
    for result in results:
        if not result.counts_toward_cell:
            continue
        if not include_reused_controls and result.source == "control_reuse":
            continue
        grouped[(result.task_id, result.task_revision, result.model_id, result.condition_id)].append(result)

    aggregates: list[CellAggregate] = []
    for task in tasks:
        for model in models:
            required = required_by_model[model.id]
            for condition in conditions:
                key = (task.revision.task_id, task.revision.revision, model.id, condition.id)
                cell = grouped.get(key, [])
                reachable = sum(result.reachable is True for result in cell)
                successful = sum(result.task_success is True for result in cell)
                blindness = sum(result.self_reported_blindness is True for result in cell)
                joint = {
                    "reachable_success": sum(
                        result.reachable is True and result.task_success is True for result in cell
                    ),
                    "reachable_failure": sum(
                        result.reachable is True and result.task_success is False for result in cell
                    ),
                    "unreachable_success": sum(
                        result.reachable is False and result.task_success is True for result in cell
                    ),
                    "unreachable_failure": sum(
                        result.reachable is False and result.task_success is False for result in cell
                    ),
                }
                elapsed = [(result.finished_at - result.started_at).total_seconds() for result in cell]
                costs = [result.provider_cost_usd for result in cell if result.provider_cost_usd is not None]
                aggregates.append(
                    CellAggregate(
                        task_id=key[0],
                        task_revision=key[1],
                        model_id=key[2],
                        condition_id=key[3],
                        valid_trials=len(cell),
                        required_trials=required,
                        complete=len(cell) == required,
                        reachability=wilson_interval(reachable, len(cell)),
                        task_success=wilson_interval(successful, len(cell)),
                        self_reported_blindness=wilson_interval(blindness, len(cell)),
                        joint_outcomes=joint,
                        mean_input_tokens=_mean([float(result.input_tokens) for result in cell]),
                        mean_output_tokens=_mean([float(result.output_tokens) for result in cell]),
                        mean_actions=_mean([float(result.actions) for result in cell]),
                        mean_elapsed_seconds=_mean(elapsed),
                        mean_provider_cost_usd=_mean([float(value) for value in costs]),
                        reused_control_trials=sum(result.source == "control_reuse" for result in cell),
                    )
                )
    return aggregates


def _auc(rates: list[float | None]) -> float | None:
    if len(rates) < 2 or any(rate is None for rate in rates):
        return None
    numeric = [float(rate) for rate in rates if rate is not None]
    return sum((left + right) / 2 for left, right in zip(numeric, numeric[1:])) / (len(numeric) - 1)


def axis_summaries(
    resolved: ResolvedAccessibilitySweep,
    aggregates: list[CellAggregate],
) -> list[AxisSummary]:
    return _axis_summaries_for(
        tasks=resolved.tasks,
        models=resolved.models,
        axes=resolved.spec.axes,
        aggregates=aggregates,
    )


def dom_primary_axis_summaries(
    resolved: ResolvedDomLightSweep,
    aggregates: list[CellAggregate],
) -> list[AxisSummary]:
    return _axis_summaries_for(
        tasks=resolved.tasks,
        models=[resolved.primary_model],
        axes=resolved.spec.axes,
        aggregates=aggregates,
    )


def dom_additional_axis_summaries(
    resolved: ResolvedDomLightSweep,
    aggregates: list[CellAggregate],
) -> list[AxisSummary]:
    return _axis_summaries_for(
        tasks=resolved.tasks,
        models=resolved.additional_models,
        axes=resolved.spec.axes,
        aggregates=aggregates,
    )


def _axis_summaries_for(
    *,
    tasks: list[ResolvedTask],
    models: list[ModelSpec],
    axes: list[SweepAxis],
    aggregates: list[CellAggregate],
) -> list[AxisSummary]:
    by_key = {(item.task_id, item.task_revision, item.model_id, item.condition_id): item for item in aggregates}
    summaries: list[AxisSummary] = []
    for task in tasks:
        for model in models:
            for axis in axes:
                keys = [
                    (task.revision.task_id, task.revision.revision, model.id, level.condition_id)
                    for level in axis.levels
                ]
                cells = [by_key[key] for key in keys]
                reachability_rates = [cell.reachability.rate for cell in cells]
                success_rates = [cell.task_success.rate for cell in cells]
                numeric_reachability = [rate for rate in reachability_rates if rate is not None]
                mean_drop = None
                maximum_drop = None
                if len(numeric_reachability) == len(reachability_rates) and len(numeric_reachability) > 1:
                    baseline = numeric_reachability[0]
                    mean_drop = baseline - fmean(numeric_reachability[1:])
                    maximum_drop = max(
                        left - right for left, right in zip(numeric_reachability, numeric_reachability[1:])
                    )
                summaries.append(
                    AxisSummary(
                        task_id=task.revision.task_id,
                        task_revision=task.revision.revision,
                        model_id=model.id,
                        axis_id=axis.id,
                        ordered_levels=[level.id for level in axis.levels],
                        condition_ids=[level.condition_id for level in axis.levels],
                        reachability_rates=reachability_rates,
                        success_rates=success_rates,
                        reachability_auc=_auc(reachability_rates),
                        success_auc=_auc(success_rates),
                        mean_reachability_drop=mean_drop,
                        maximum_adjacent_reachability_drop=maximum_drop,
                    )
                )
    return summaries


def _modal(rate: float) -> bool | None:
    if rate > 0.5:
        return True
    if rate < 0.5:
        return False
    return None


def model_confirmations(
    resolved: ResolvedDomLightSweep,
    primary_aggregates: list[CellAggregate],
    results: list[SweepTrialResult],
) -> list[ModelConfirmation]:
    primary_by_cell = {(item.task_id, item.task_revision, item.condition_id): item for item in primary_aggregates}
    confirmations: list[ModelConfirmation] = []
    for result in results:
        if not result.counts_toward_cell or result.allocation_role != "confirmation":
            continue
        if (
            result.run_id is None
            or result.reachable is None
            or result.task_success is None
            or result.self_reported_blindness is None
        ):
            continue
        primary = primary_by_cell[(result.task_id, result.task_revision, result.condition_id)]
        if primary.reachability.rate is None or primary.task_success.rate is None:
            continue
        modal_reachability = _modal(primary.reachability.rate)
        modal_success = _modal(primary.task_success.rate)
        reachability_agrees = None if modal_reachability is None else result.reachable == modal_reachability
        success_agrees = None if modal_success is None else result.task_success == modal_success
        confirmations.append(
            ModelConfirmation(
                task_id=result.task_id,
                task_revision=result.task_revision,
                model_id=result.model_id,
                condition_id=result.condition_id,
                run_id=result.run_id,
                reachable=result.reachable,
                task_success=result.task_success,
                self_reported_blindness=result.self_reported_blindness,
                primary_reachability_rate=primary.reachability.rate,
                primary_success_rate=primary.task_success.rate,
                primary_modal_reachability=modal_reachability,
                primary_modal_success=modal_success,
                reachability_agrees=reachability_agrees,
                success_agrees=success_agrees,
                divergent=reachability_agrees is False or success_agrees is False,
            )
        )
    return sorted(
        confirmations,
        key=lambda item: (item.task_id, item.task_revision, item.model_id, item.condition_id),
    )
