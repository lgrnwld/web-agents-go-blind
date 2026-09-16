#!/usr/bin/env python3
"""Aggregate the reviewer-requested canvas separation follow-up.

The script is provider-free.  It consumes the immutable JSONL emitted by
``run_canvas_delimitation_followup.py`` and reports all three prespecified
scoring rules with Wilson intervals, both pooled and by task/model.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from webagents.io import atomic_write

LAYOUT_ORDER = (
    "canvas-inline-bar",
    "canvas-inline-two-spaces",
    "canvas-inline-gap-40px",
    "canvas-separate-line",
    "canvas-opposite-corner",
    "dom-inline-bar",
)
METRICS = ("exact_match", "target_without_marker", "substring_match")
SUCCESS_STATUSES = {"success", "failure", "timeout"}


def wilson(successes: int, trials: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if trials == 0:
        return math.nan, math.nan
    estimate = successes / trials
    denominator = 1 + z * z / trials
    center = (estimate + z * z / (2 * trials)) / denominator
    half_width = z * math.sqrt(estimate * (1 - estimate) / trials + z * z / (4 * trials * trials))
    half_width /= denominator
    return center - half_width, center + half_width


def aggregate(rows: list[dict[str, Any]], dimensions: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(str(row[key]) for key in dimensions)].append(row)

    output: list[dict[str, Any]] = []
    for key, group in groups.items():
        base = dict(zip(dimensions, key, strict=True))
        for metric in METRICS:
            successes = sum(bool(row[metric]) for row in group)
            low, high = wilson(successes, len(group))
            output.append(
                {
                    **base,
                    "criterion": metric,
                    "successes": successes,
                    "trials": len(group),
                    "rate": successes / len(group),
                    "wilson_low": low,
                    "wilson_high": high,
                }
            )
    return sorted(
        output,
        key=lambda item: (
            LAYOUT_ORDER.index(item["layout"]),
            item["criterion"],
            *(item.get(dimension, "") for dimension in dimensions if dimension != "layout"),
        ),
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def percent(value: float) -> str:
    return f"{100 * value:.1f}"


def build_markdown(
    pooled: list[dict[str, Any]],
    stratified: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
) -> str:
    del stratified
    by_layout_metric = {(row["layout"], row["criterion"]): row for row in pooled}
    lines = [
        "# Canvas delimitation follow-up",
        "",
        (
            "All estimates below use final non-infrastructure trials; complete timeout transcripts count as "
            "task failures, matching the core sweep. Intervals are two-sided Wilson 95% intervals."
        ),
        "",
        "| Layout | Exact | Target present, marker absent | Substring |",
        "|---|---:|---:|---:|",
    ]
    for layout in LAYOUT_ORDER:
        cells = []
        for metric in METRICS:
            row = by_layout_metric[(layout, metric)]
            cells.append(
                f"{row['successes']}/{row['trials']} "
                f"({percent(row['wilson_low'])}--{percent(row['wilson_high'])}%)"
            )
        lines.append(f"| {layout} | " + " | ".join(cells) + " |")

    canvas_exact = [by_layout_metric[(layout, "exact_match")]["rate"] for layout in LAYOUT_ORDER[:5]]
    canvas_middle = [
        by_layout_metric[(layout, "target_without_marker")]["rate"] for layout in LAYOUT_ORDER[:5]
    ]
    lines.extend(
        [
            "",
            "## Shape checks",
            "",
            f"- Exact-match rates are monotone nondecreasing across the five ordered canvas layouts: "
            f"{all(left <= right for left, right in zip(canvas_exact, canvas_exact[1:], strict=False))}.",
            f"- Target-without-marker rates are monotone nondecreasing: "
            f"{all(left <= right for left, right in zip(canvas_middle, canvas_middle[1:], strict=False))}.",
            f"- Final records excluded for infrastructure status: {len(excluded)}.",
            "",
            "The DOM inline-bar row is a layout control, not a sixth point on the separation ordering.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    raw = [json.loads(line) for line in args.results.read_text().splitlines() if line.strip()]
    valid = [row for row in raw if row.get("runner_status") in SUCCESS_STATUSES]
    excluded = [row for row in raw if row.get("runner_status") not in SUCCESS_STATUSES]

    expected = {
        (layout, task, model, repeat)
        for layout in LAYOUT_ORDER
        for task in {row["task_id"] for row in raw}
        for model in {row["model"] for row in raw}
        for repeat in range(1, 11)
    }
    observed = {(row["layout"], row["task_id"], row["model"], row["repeat"]) for row in valid}
    if observed != expected:
        missing = sorted(expected - observed)
        duplicate_count = len(valid) - len(observed)
        raise SystemExit(f"incomplete/nonunique matrix: missing={len(missing)}, duplicates={duplicate_count}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    pooled = aggregate(valid, ("layout",))
    stratified = aggregate(valid, ("layout", "task_id", "model"))
    write_csv(args.output_dir / "pooled.csv", pooled)
    write_csv(args.output_dir / "by-task-model.csv", stratified)
    atomic_write(args.output_dir / "report.md", build_markdown(pooled, stratified, excluded).encode())
    atomic_write(
        args.output_dir / "analysis.json",
        (json.dumps({"trials": len(valid), "excluded": excluded, "pooled": pooled}, indent=2) + "\n").encode(),
    )


if __name__ == "__main__":
    main()
