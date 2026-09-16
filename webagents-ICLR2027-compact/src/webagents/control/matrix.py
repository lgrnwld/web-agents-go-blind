"""Deterministic Cartesian expansion for level-0 control trials."""

from __future__ import annotations

import hashlib
import random

from webagents.capture.archive import canonical_json_bytes
from webagents.control.schemas import ControlSpec, MatrixRow, ResolvedTask


def expand_matrix(
    spec: ControlSpec,
    tasks: list[ResolvedTask],
    *,
    phase: int,
    start_index: int = 0,
) -> list[MatrixRow]:
    raw: list[dict[str, object]] = []
    for task in tasks:
        for agent_class in spec.classes:
            for model in spec.models:
                for repeat in range(1, spec.policy.repeats + 1):
                    identity = {
                        "class_id": agent_class.id,
                        "task_id": task.revision.task_id,
                        "task_revision": task.revision.revision,
                        "model_id": model.id,
                        "repeat": repeat,
                    }
                    logical_id = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]
                    raw.append(
                        {
                            **identity,
                            "logical_trial_id": logical_id,
                            "runner": agent_class.runner,
                            "task_definition_sha256": task.definition_sha256,
                            "runner_model": model.runner_model,
                        }
                    )
    expected = len(tasks) * len(spec.classes) * len(spec.models) * spec.policy.repeats
    if len(raw) != expected or len({str(item["logical_trial_id"]) for item in raw}) != expected:
        raise ValueError("control matrix is incomplete or contains duplicate trials")
    random.Random(spec.policy.schedule_seed + phase).shuffle(raw)
    return [MatrixRow.model_validate({**item, "schedule_index": start_index + index}) for index, item in enumerate(raw)]
