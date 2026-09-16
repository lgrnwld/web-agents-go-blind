#!/usr/bin/env python3
"""Validate and summarize the four-deployment vision roster."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

CONDITIONS = (
    "level-0",
    "iframe-same-depth-1",
    "iframe-same-depth-2",
    "iframe-same-depth-3",
    "iframe-cross-depth-1",
    "iframe-cross-depth-2",
    "iframe-cross-depth-3",
    "shadow-open",
    "shadow-closed",
    "shadow-nested-open-open",
    "rendered-canvas",
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--core-results",
        type=Path,
        default=Path("artifacts/sweeps-r5/vision-final/vision-full-scan-v2/results.jsonl"),
    )
    parser.add_argument("--extension-results", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    core = [row for row in read_jsonl(args.core_results) if row.get("counts_toward_cell") is True]
    extension = [row for row in read_jsonl(args.extension_results) if row.get("counts_toward_cell") is True]
    rows = core + extension
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["model_id"], row["task_id"], row["condition_id"])].append(row)

    models = sorted({row["model_id"] for row in rows})
    tasks = sorted({row["task_id"] for row in rows})
    expected = {(model, task, condition) for model in models for task in tasks for condition in CONDITIONS}
    if set(groups) != expected or any(len(group) != 10 for group in groups.values()):
        incomplete = {key: len(groups.get(key, [])) for key in expected if len(groups.get(key, [])) != 10}
        raise SystemExit(f"incomplete four-model vision grid: {incomplete}")

    cells = []
    for model, task, condition in sorted(groups, key=lambda key: (key[0], key[1], CONDITIONS.index(key[2]))):
        group = groups[(model, task, condition)]
        cells.append(
            {
                "model_id": model,
                "task_id": task,
                "condition_id": condition,
                "successes": sum(row.get("task_success") is True for row in group),
                "trials": len(group),
                "reachable": sum(row.get("reachable") is True for row in group),
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "combined-cells.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(cells[0]))
        writer.writeheader()
        writer.writerows(cells)

    lines = [
        "# Four-deployment vision extension",
        "",
        f"Validated {len(rows)} outcomes: four deployments x two tasks x 11 conditions x ten trials.",
        "",
        (
            "The table reports successes out of ten. The original coordinate/position confound remains part of "
            "the core-runner replication, so iframe differences are descriptive runner behavior, not depth effects."
        ),
        "",
        "| Model | Task | L0 | S1 | S2 | S3 | X1 | X2 | X3 | Open | Closed | Nested | Canvas |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    shorthand = [
        "level-0",
        "iframe-same-depth-1",
        "iframe-same-depth-2",
        "iframe-same-depth-3",
        "iframe-cross-depth-1",
        "iframe-cross-depth-2",
        "iframe-cross-depth-3",
        "shadow-open",
        "shadow-closed",
        "shadow-nested-open-open",
        "rendered-canvas",
    ]
    lookup = {(row["model_id"], row["task_id"], row["condition_id"]): row for row in cells}
    for model in models:
        for task in tasks:
            values = [f"{lookup[(model, task, condition)]['successes']}/10" for condition in shorthand]
            lines.append(f"| {model} | {task} | " + " | ".join(values) + " |")
    (args.output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
