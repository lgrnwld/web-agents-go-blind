"""Deterministic full-allocation matrix expansion and validation."""

from __future__ import annotations

import hashlib
import random
from collections import Counter

from webagents.capture.archive import canonical_json_bytes
from webagents.sweeps.schemas import (
    DomSweepMatrixRow,
    ResolvedAccessibilitySweep,
    ResolvedDomLightSweep,
    SweepMatrixRow,
)


def expand_full_matrix(resolved: ResolvedAccessibilitySweep) -> list[SweepMatrixRow]:
    """Expand T x M x C x N and interleave repeat blocks deterministically."""

    spec = resolved.spec
    raw_by_repeat: list[list[dict[str, object]]] = []
    for repeat in range(1, spec.allocation.valid_trials_per_cell + 1):
        block: list[dict[str, object]] = []
        for task in resolved.tasks:
            for model in resolved.models:
                for condition in spec.conditions:
                    identity = {
                        "agent_class": spec.agent_class,
                        "task_id": task.revision.task_id,
                        "task_revision": task.revision.revision,
                        "model_id": model.id,
                        "condition_id": condition.id,
                        "repeat": repeat,
                        "observation_policy": spec.observation_policy,
                    }
                    logical_id = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]
                    block.append(
                        {
                            **identity,
                            "logical_trial_id": logical_id,
                            "runner": spec.runner,
                            "task_definition_sha256": task.definition_sha256,
                            "runner_model": model.runner_model,
                        }
                    )
        random.Random(spec.analysis.randomization_seed + repeat).shuffle(block)
        raw_by_repeat.append(block)

    raw = [item for block in raw_by_repeat for item in block]
    rows = [SweepMatrixRow.model_validate({**item, "schedule_index": index}) for index, item in enumerate(raw)]
    validate_full_matrix(resolved, rows)
    return rows


def validate_full_matrix(resolved: ResolvedAccessibilitySweep, rows: list[SweepMatrixRow]) -> None:
    spec = resolved.spec
    expected = len(resolved.tasks) * len(resolved.models) * len(spec.conditions) * spec.allocation.valid_trials_per_cell
    if len(rows) != expected:
        raise ValueError(f"full {spec.agent_class} matrix has {len(rows)} rows, expected {expected}")
    logical_ids = [row.logical_trial_id for row in rows]
    if len(logical_ids) != len(set(logical_ids)):
        raise ValueError(f"full {spec.agent_class} matrix contains duplicate logical trial IDs")
    schedule_indices = [row.schedule_index for row in rows]
    if sorted(schedule_indices) != list(range(expected)):
        raise ValueError("matrix schedule indices must cover one contiguous range")

    counts = Counter((row.task_id, row.task_revision, row.model_id, row.condition_id) for row in rows)
    expected_cells = len(resolved.tasks) * len(resolved.models) * len(spec.conditions)
    if len(counts) != expected_cells or set(counts.values()) != {spec.allocation.valid_trials_per_cell}:
        raise ValueError("every task-model-condition cell must have exactly ten rows")
    repeat_sets: dict[tuple[str, int, str, str], set[int]] = {}
    for row in rows:
        key = (row.task_id, row.task_revision, row.model_id, row.condition_id)
        repeat_sets.setdefault(key, set()).add(row.repeat)
    expected_repeats = set(range(1, spec.allocation.valid_trials_per_cell + 1))
    if any(repeats != expected_repeats for repeats in repeat_sets.values()):
        raise ValueError("every cell must contain repeat indices 1 through 10 exactly once")


def expand_dom_light_matrix(resolved: ResolvedDomLightSweep) -> list[DomSweepMatrixRow]:
    """Expand T x C x (10 + M - 1) with confirmations interleaved across primary repeat blocks."""

    spec = resolved.spec
    primary_repeats = spec.allocation.primary_valid_trials_per_cell
    blocks: list[list[dict[str, object]]] = [[] for _ in range(primary_repeats)]
    for repeat in range(1, primary_repeats + 1):
        for task in resolved.tasks:
            for condition in spec.conditions:
                identity = {
                    "agent_class": "dom_extraction",
                    "allocation_role": "primary",
                    "task_id": task.revision.task_id,
                    "task_revision": task.revision.revision,
                    "model_id": resolved.primary_model.id,
                    "condition_id": condition.id,
                    "repeat": repeat,
                    "observation_policy": spec.observation_policy,
                }
                blocks[repeat - 1].append(
                    {
                        **identity,
                        "logical_trial_id": hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24],
                        "runner": "dom",
                        "task_definition_sha256": task.definition_sha256,
                        "runner_model": resolved.primary_model.runner_model,
                    }
                )

    confirmations: list[dict[str, object]] = []
    for repeat in range(1, spec.allocation.additional_valid_trials_per_cell + 1):
        for task in resolved.tasks:
            for model in resolved.additional_models:
                for condition in spec.conditions:
                    identity = {
                        "agent_class": "dom_extraction",
                        "allocation_role": "confirmation",
                        "task_id": task.revision.task_id,
                        "task_revision": task.revision.revision,
                        "model_id": model.id,
                        "condition_id": condition.id,
                        "repeat": repeat,
                        "observation_policy": spec.observation_policy,
                    }
                    confirmations.append(
                        {
                            **identity,
                            "logical_trial_id": hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24],
                            "runner": "dom",
                            "task_definition_sha256": task.definition_sha256,
                            "runner_model": model.runner_model,
                        }
                    )
    random.Random(spec.analysis.randomization_seed - 1).shuffle(confirmations)
    for index, confirmation in enumerate(confirmations):
        blocks[index % primary_repeats].append(confirmation)
    for index, block in enumerate(blocks):
        random.Random(spec.analysis.randomization_seed + index + 1).shuffle(block)

    raw = [item for block in blocks for item in block]
    rows = [DomSweepMatrixRow.model_validate({**item, "schedule_index": index}) for index, item in enumerate(raw)]
    validate_dom_light_matrix(resolved, rows)
    return rows


def validate_dom_light_matrix(resolved: ResolvedDomLightSweep, rows: list[DomSweepMatrixRow]) -> None:
    spec = resolved.spec
    primary_repeats = spec.allocation.primary_valid_trials_per_cell
    confirmation_repeats = spec.allocation.additional_valid_trials_per_cell
    expected = (
        len(resolved.tasks)
        * len(spec.conditions)
        * (primary_repeats + confirmation_repeats * len(resolved.additional_models))
    )
    if len(rows) != expected:
        raise ValueError(f"DOM light matrix has {len(rows)} rows, expected {expected}")
    if len({row.logical_trial_id for row in rows}) != expected:
        raise ValueError("DOM light matrix contains duplicate logical trial IDs")
    if sorted(row.schedule_index for row in rows) != list(range(expected)):
        raise ValueError("DOM light matrix schedule indices must cover one contiguous range")

    counts = Counter(
        (row.allocation_role, row.task_id, row.task_revision, row.model_id, row.condition_id) for row in rows
    )
    primary_keys = {
        ("primary", task.revision.task_id, task.revision.revision, resolved.primary_model.id, condition.id)
        for task in resolved.tasks
        for condition in spec.conditions
    }
    confirmation_keys = {
        ("confirmation", task.revision.task_id, task.revision.revision, model.id, condition.id)
        for task in resolved.tasks
        for model in resolved.additional_models
        for condition in spec.conditions
    }
    if set(counts) != primary_keys | confirmation_keys:
        raise ValueError("DOM light matrix is missing or adds task-model-condition cells")
    if any(counts[key] != primary_repeats for key in primary_keys):
        raise ValueError("every primary-model DOM cell must contain exactly ten rows")
    if any(counts[key] != confirmation_repeats for key in confirmation_keys):
        raise ValueError("every additional-model DOM cell must contain exactly one row")
    for row in rows:
        if row.allocation_role == "primary" and not 1 <= row.repeat <= primary_repeats:
            raise ValueError("primary repeat indices must be 1 through 10")
        if row.allocation_role == "confirmation" and not 1 <= row.repeat <= confirmation_repeats:
            raise ValueError("confirmation repeat indices must cover the declared allocation")
