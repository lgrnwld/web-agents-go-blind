#!/usr/bin/env python3
"""Run two additional deployments on the frozen 11-condition vision arm.

This is a separately versioned extension rather than a mutation of the core
sweep. It reuses the core tasks, fixtures, viewport, prompt, action runner, and
checker, while allocating ten fresh trials to every model/task/condition cell.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
from pathlib import Path
from typing import Any

from webagents.capture.archive import canonical_json_bytes
from webagents.control.schemas import ModelSpec
from webagents.io import append_jsonl, atomic_write
from webagents.sweeps.harness import StructuralFixtureHarness
from webagents.sweeps.scheduler import _execute_once  # noqa: PLC2701
from webagents.sweeps.schemas import ResolvedAccessibilitySweep, SweepMatrixRow

DEFAULT_SOURCE = Path("artifacts/sweeps-r5/vision-final/vision-full-scan-v2/resolved-spec.json")
DEFAULT_MODELS = ("foundry/gpt-5.4", "foundry/gpt-5-mini")


def slug(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-")


def model_spec(runner_model: str) -> ModelSpec:
    provider, name = runner_model.split("/", 1)
    return ModelSpec(
        id=slug(runner_model),
        runner_model=runner_model,
        expected_provider=provider,
        expected_model=name,
    )


def build_matrix(
    resolved: ResolvedAccessibilitySweep,
    models: tuple[ModelSpec, ...],
    repeats: int,
    seed: int,
) -> list[SweepMatrixRow]:
    rows: list[SweepMatrixRow] = []
    for repeat in range(1, repeats + 1):
        block: list[SweepMatrixRow] = []
        for task in resolved.tasks:
            for model in models:
                for condition in resolved.spec.conditions:
                    identity = {
                        "agent_class": "vision",
                        "task_id": task.revision.task_id,
                        "task_revision": task.revision.revision,
                        "task_definition_sha256": task.definition_sha256,
                        "model_id": model.id,
                        "runner_model": model.runner_model,
                        "observation_policy": "unrestricted",
                        "condition_id": condition.id,
                        "repeat": repeat,
                    }
                    logical_id = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]
                    block.append(
                        SweepMatrixRow(
                            logical_trial_id=logical_id,
                            runner="vision",
                            schedule_index=0,
                            **identity,
                        )
                    )
        random.Random(seed + repeat).shuffle(block)
        rows.extend(block)
    return [row.model_copy(update={"schedule_index": index}) for index, row in enumerate(rows)]


def load_resume(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    results = path / "results.jsonl" if path.is_dir() else path
    rows = [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
    return {
        row["logical_trial_id"]: row
        for row in rows
        if row.get("counts_toward_cell") is True and row.get("classification") == "VALID_OUTCOME"
    }


async def execute(args: argparse.Namespace) -> None:
    source_bytes = args.source_resolved_spec.read_bytes()
    source = ResolvedAccessibilitySweep.model_validate_json(source_bytes)
    models = tuple(model_spec(value) for value in args.models)
    spec = source.spec.model_copy(
        update={
            "sweep_id": args.sweep_id,
            "reuse_level0_controls": False,
            "allocation": source.spec.allocation.model_copy(
                update={"valid_trials_per_cell": args.repeats, "max_infrastructure_retries": args.retries}
            ),
            "runtime": source.spec.runtime.model_copy(update={"timeout_seconds": args.timeout_seconds}),
        }
    )
    resolved = source.model_copy(update={"spec": spec, "models": list(models)})
    matrix = build_matrix(resolved, models, args.repeats, args.seed)
    inherited = load_resume(args.resume_from)
    expected_ids = {row.logical_trial_id for row in matrix}
    if not set(inherited).issubset(expected_ids):
        raise SystemExit("resume source contains logical trials outside this extension matrix")

    args.output_dir.mkdir(parents=True, exist_ok=False)
    results_path = args.output_dir / "results.jsonl"
    attempts_path = args.output_dir / "attempts.jsonl"
    atomic_write(results_path, b"")
    atomic_write(attempts_path, b"")
    for row in matrix:
        if row.logical_trial_id in inherited:
            value = {**inherited[row.logical_trial_id], "source": "inherited"}
            append_jsonl(results_path, value)

    resolved_payload = resolved.model_dump(mode="json")
    resolved_payload["extension_provenance"] = {
        "source_resolved_spec": str(args.source_resolved_spec),
        "source_resolved_spec_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "note": "Exact core-runner replication; it intentionally retains the archived 1280x900 coordinate contract.",
    }
    atomic_write(args.output_dir / "resolved-spec.json", canonical_json_bytes(resolved_payload))
    matrix_path = args.output_dir / "matrix.jsonl"
    atomic_write(matrix_path, b"")
    for row in matrix:
        append_jsonl(matrix_path, row.model_dump(mode="json"))

    queue: asyncio.Queue[SweepMatrixRow] = asyncio.Queue()
    for row in matrix:
        if row.logical_trial_id not in inherited:
            queue.put_nowait(row)
    write_lock = asyncio.Lock()
    task_map = {(task.revision.task_id, task.revision.revision): task for task in resolved.tasks}
    condition_map = {condition.id: condition for condition in resolved.spec.conditions}
    completed = len(inherited)

    async def worker() -> None:
        nonlocal completed
        with StructuralFixtureHarness(spec.harness, resolved.tasks, spec.conditions) as harness:
            harness.preflight()
            while not queue.empty():
                try:
                    row = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                final = None
                for attempt in range(args.retries + 1):
                    result = await _execute_once(
                        resolved=resolved,
                        row=row,
                        task=task_map[(row.task_id, row.task_revision)],
                        condition=condition_map[row.condition_id],
                        archive_root=args.archive_root,
                        harness=harness,
                        runner=args.runner,
                        attempt=attempt,
                    )
                    async with write_lock:
                        append_jsonl(attempts_path, result.model_dump(mode="json"))
                    final = result
                    if result.counts_toward_cell or result.classification != "INFRASTRUCTURE_FAILURE":
                        break
                assert final is not None
                async with write_lock:
                    append_jsonl(results_path, final.model_dump(mode="json"))
                    completed += 1
                    print(
                        json.dumps(
                            {
                                "completed": completed,
                                "planned": len(matrix),
                                "model": row.runner_model,
                                "task": row.task_id,
                                "condition": row.condition_id,
                                "classification": final.classification,
                                "success": final.task_success,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                queue.task_done()

    from webagents.vision.runner import run_vision_agent

    args.runner = run_vision_agent
    await asyncio.gather(*(worker() for _ in range(args.concurrency)))
    final_rows = [json.loads(line) for line in results_path.read_text().splitlines() if line.strip()]
    valid_ids = {row["logical_trial_id"] for row in final_rows if row.get("counts_toward_cell") is True}
    report = {
        "schema_version": "1.0",
        "planned_trials": len(matrix),
        "valid_trials": len(valid_ids),
        "complete": valid_ids == expected_ids,
        "models": list(args.models),
        "source_resolved_spec_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
        "results_sha256": hashlib.sha256(results_path.read_bytes()).hexdigest(),
    }
    atomic_write(args.output_dir / "report.json", canonical_json_bytes(report))
    if not report["complete"]:
        raise SystemExit("extension incomplete; resume into a new output directory")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-resolved-spec", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--sweep-id", default="vision-model-extension-v1")
    parser.add_argument("--models", nargs=2, default=list(DEFAULT_MODELS))
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260904)
    parser.add_argument("--resume-from", type=Path)
    args = parser.parse_args()
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()
