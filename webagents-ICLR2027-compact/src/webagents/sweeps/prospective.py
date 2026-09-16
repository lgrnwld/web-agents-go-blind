"""Provider-free, hash-bound plans for vision and CDP full sweeps."""

from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Any

from webagents.capture.archive import canonical_json_bytes
from webagents.control.config import load_all_task_revisions, load_control_spec
from webagents.io import append_jsonl, atomic_write
from webagents.sweeps.config import SweepConfigurationError, load_sweep_spec


def plan_prospective_sweep(
    spec_path: Path,
    *,
    control_spec_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Freeze a future architecture matrix without making any provider calls."""

    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise SweepConfigurationError(f"prospective plan output already exists: {output_dir}")
    spec = load_sweep_spec(spec_path)
    if spec.agent_class not in {"vision", "cdp_frame_traversal"}:
        raise SweepConfigurationError("provider-free architecture plans are only defined for vision or CDP")
    control = load_control_spec(control_spec_path)
    all_tasks = load_all_task_revisions(control_spec_path, control)
    tasks = [all_tasks[(item.id, item.active_revision)] for item in control.tasks]
    rows: list[dict[str, Any]] = []
    for repeat in range(1, spec.allocation.valid_trials_per_cell + 1):
        block: list[dict[str, Any]] = []
        for task in tasks:
            for model in control.models:
                for condition in spec.conditions:
                    identity = {
                        "agent_class": spec.agent_class,
                        "task_id": task.revision.task_id,
                        "task_revision": task.revision.revision,
                        "task_definition_sha256": task.definition_sha256,
                        "model_id": model.id,
                        "runner_model": model.runner_model,
                        "condition_id": condition.id,
                        "repeat": repeat,
                    }
                    block.append(
                        {
                            **identity,
                            "logical_trial_id": hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24],
                        }
                    )
        random.Random(spec.analysis.randomization_seed + repeat).shuffle(block)
        rows.extend(block)
    for index, row in enumerate(rows):
        row["schedule_index"] = index

    expected = len(tasks) * len(control.models) * len(spec.conditions) * spec.allocation.valid_trials_per_cell
    if len(rows) != expected or len({row["logical_trial_id"] for row in rows}) != expected:
        raise SweepConfigurationError("prospective matrix expansion is incomplete or contains duplicate IDs")
    control_classes = {item.id for item in control.classes}
    blockers = [] if spec.agent_class in control_classes else [
        f"expanded level-0 control does not include {spec.agent_class}"
    ]
    plan = {
        "schema_version": "1.0",
        "sweep_id": spec.sweep_id,
        "agent_class": spec.agent_class,
        "runner": spec.runner,
        "observation_kind": spec.observation_kind,
        "launchable": not blockers,
        "blocking_requirements": blockers,
        "task_count": len(tasks),
        "model_count": len(control.models),
        "condition_count": len(spec.conditions),
        "valid_trials_per_cell": spec.allocation.valid_trials_per_cell,
        "planned_trial_count": expected,
        "condition_inventory_sha256": hashlib.sha256(
            canonical_json_bytes(
                {
                    "conditions": [item.model_dump(mode="json") for item in spec.conditions],
                    "axes": [item.model_dump(mode="json") for item in spec.axes],
                }
            )
        ).hexdigest(),
        "control_spec_sha256": hashlib.sha256(canonical_json_bytes(control.model_dump(mode="json"))).hexdigest(),
    }
    output_dir.mkdir(parents=True)
    matrix_path = output_dir / "matrix.jsonl"
    atomic_write(matrix_path, b"")
    for row in rows:
        append_jsonl(matrix_path, row)
    plan["matrix_sha256"] = hashlib.sha256(matrix_path.read_bytes()).hexdigest()
    plan_bytes = canonical_json_bytes(plan)
    atomic_write(output_dir / "plan.json", plan_bytes)
    receipt = {
        "schema_version": "1.0",
        "sweep_id": spec.sweep_id,
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "matrix_sha256": plan["matrix_sha256"],
        "launchable": plan["launchable"],
    }
    atomic_write(output_dir / "plan-receipt.json", canonical_json_bytes(receipt))
    return plan
