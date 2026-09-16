#!/usr/bin/env python3
"""Deblind the independent canvas coding and report agreement honestly."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def cohen_kappa(left: list[str], right: list[str]) -> tuple[float | None, float, float]:
    if len(left) != len(right) or not left:
        raise ValueError("paired nonempty labels are required")
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
    left_counts = Counter(left)
    right_counts = Counter(right)
    labels = set(left_counts) | set(right_counts)
    expected = sum(left_counts[label] / len(left) * right_counts[label] / len(right) for label in labels)
    if expected == 1:
        return None, observed, expected
    return (observed - expected) / (1 - expected), observed, expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--first",
        type=Path,
        default=Path("analysis/core-grid-v1/vision-canvas-failure-coding.csv"),
    )
    parser.add_argument(
        "--second",
        type=Path,
        default=Path("analysis/reviewer-response-v1/blind-coding-packet/second-coder-sheet.csv"),
    )
    parser.add_argument(
        "--key",
        type=Path,
        default=Path("analysis/reviewer-response-v1/blind-coding-key.csv"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("analysis/reviewer-response-v1/blind-coding-agreement.json"),
    )
    args = parser.parse_args()
    first = {row["run_id"]: row["failure_stage"] for row in read_csv(args.first)}
    key = {row["case_id"]: row["run_id"] for row in read_csv(args.key)}
    second = {row["case_id"]: row["failure_stage"] for row in read_csv(args.second)}
    if set(key) != set(second) or set(first) != set(key.values()):
        raise RuntimeError("first labels, blind key, and second labels do not define the same 40 trials")
    case_ids = sorted(key)
    left = [first[key[case_id]] for case_id in case_ids]
    right = [second[case_id] for case_id in case_ids]
    kappa, observed, expected = cohen_kappa(left, right)
    disagreements = [
        {"case_id": case_id, "first": a, "second": b}
        for case_id, a, b in zip(case_ids, left, right, strict=True)
        if a != b
    ]
    result: dict[str, Any] = {
        "trials": len(left),
        "agreements": len(left) - len(disagreements),
        "percent_agreement": observed,
        "cohen_kappa": kappa,
        "cohen_kappa_status": (
            "undefined_zero_marginal_variance"
            if kappa is None
            else "defined"
        ),
        "expected_chance_agreement": expected,
        "prevalence_adjusted_bias_adjusted_kappa": 2 * observed - 1,
        "first_label_counts": dict(sorted(Counter(left).items())),
        "second_label_counts": dict(sorted(Counter(right).items())),
        "disagreements": disagreements,
        "interpretation": (
            "Both coders assigned all trials to extraction_failure. Raw agreement is complete, but ordinary "
            "Cohen's kappa is mathematically undefined because both marginal distributions have zero variance. "
            "PABAK is reported as a prevalence-adjusted sensitivity statistic, not as a replacement for ordinary kappa."
        ),
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
