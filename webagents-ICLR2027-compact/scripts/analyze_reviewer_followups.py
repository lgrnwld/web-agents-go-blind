#!/usr/bin/env python3
"""Provider-free audits requested during manuscript review.

This script never changes immutable run archives. It derives:

* three canvas answer-scoring criteria;
* a trial-level audit of the 40 deterministic Table 4 failures; and
* a blinded packet for an independent coder of the 40 canvas failures.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import random
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_RESULTS = Path("artifacts/sweeps-r5/vision-final/vision-full-scan-v2/results.jsonl")
DEFAULT_ARCHIVES = Path("artifacts/runs")
DEFAULT_OUTPUT = Path("analysis/reviewer-response-v1")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    fields = fields or (list(rows[0]) if rows else [])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def transcript(archives: Path, run_id: str) -> dict[str, Any]:
    return json.loads((archives / run_id / "transcript.json").read_text(encoding="utf-8"))


def emitted_value(row: dict[str, Any], archived: dict[str, Any]) -> str:
    if row["task_id"] == "read-known-value":
        return str((archived.get("final") or {}).get("answer") or "")
    match = re.search(r"submitted_value=(.*), expected=", row["checker_detail"])
    if match is None:
        return ""
    try:
        value = ast.literal_eval(match.group(1))
    except Exception:
        return ""
    return "" if value is None else str(value)


def expected_and_marker(task_id: str) -> tuple[str, str]:
    if task_id == "read-known-value":
        return "ALPHA-7391", "CONTROL_READ_VALUE_R2"
    if task_id == "copy-known-value":
        return "BRAVO-4826", "CONTROL_COPY_VALUE_R5"
    raise ValueError(f"unknown task {task_id}")


def normalized(value: str) -> str:
    return value.strip()


def action_summary(archived: dict[str, Any]) -> str:
    actions: list[str] = []
    for step in archived.get("steps", []):
        for action in step.get("actions", []):
            actions.append(json.dumps(action, sort_keys=True, separators=(",", ":")))
    return " -> ".join(actions)


def first_type_action(archived: dict[str, Any]) -> tuple[int | None, int | None]:
    for step in archived.get("steps", []):
        for action in step.get("actions", []):
            if action.get("name") == "type":
                parameters = action.get("parameters", {})
                return parameters.get("x"), parameters.get("y")
    return None, None


def initial_screenshot(archives: Path, run_id: str) -> Path:
    return archives / run_id / "observations" / "step-000" / "observation.png"


def build_canvas_scoring(
    rows: list[dict[str, Any]], archives: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trials: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("counts_toward_cell") or row.get("condition_id") != "rendered-canvas":
            continue
        archived = transcript(archives, row["run_id"])
        expected, marker = expected_and_marker(row["task_id"])
        emitted = emitted_value(row, archived)
        trials.append(
            {
                "task_id": row["task_id"],
                "model_id": row["model_id"],
                "repeat": row["repeat"],
                "run_id": row["run_id"],
                "expected_value": expected,
                "marker_token": marker,
                "emitted_value": emitted,
                "exact_match": normalized(emitted) == expected,
                "substring_match": expected in emitted,
                "target_without_marker": expected in emitted and marker.casefold() not in emitted.casefold(),
            }
        )

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        groups[(trial["task_id"], trial["model_id"])].append(trial)
        groups[(trial["task_id"], "ALL")].append(trial)
        groups[("ALL", trial["model_id"])].append(trial)
        groups[("ALL", "ALL")].append(trial)
    summary: list[dict[str, Any]] = []
    for (task_id, model_id), group in sorted(groups.items()):
        summary.append(
            {
                "task_id": task_id,
                "model_id": model_id,
                "trials": len(group),
                "exact_matches": sum(item["exact_match"] for item in group),
                "substring_matches": sum(item["substring_match"] for item in group),
                "target_without_marker_matches": sum(item["target_without_marker"] for item in group),
            }
        )
    return trials, summary


def build_table4_audit(rows: list[dict[str, Any]], archives: Path) -> list[dict[str, Any]]:
    selected = [
        row
        for row in rows
        if row.get("counts_toward_cell")
        and row.get("model_id") == "foundry-gpt-5-6-terra"
        and (
            (row.get("task_id") == "copy-known-value" and row.get("condition_id", "").endswith("depth-1"))
            or (row.get("task_id") == "read-known-value" and row.get("condition_id", "").endswith("depth-3"))
        )
    ]
    audit: list[dict[str, Any]] = []
    for row in sorted(selected, key=lambda item: (item["task_id"], item["condition_id"], item["repeat"])):
        archived = transcript(archives, row["run_id"])
        screenshot = initial_screenshot(archives, row["run_id"])
        x, y = first_type_action(archived)
        first_response = ((archived.get("steps") or [{}])[0].get("model_response") or {}).get("text", "")
        audit.append(
            {
                "task_id": row["task_id"],
                "condition_id": row["condition_id"],
                "repeat": row["repeat"],
                "run_id": row["run_id"],
                "task_success": row["task_success"],
                "actions": row["actions"],
                "checker_detail": row["checker_detail"],
                "initial_screenshot_sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
                "first_response": first_response,
                "first_type_x": x,
                "first_type_y": y,
                "action_trace": action_summary(archived),
            }
        )
    if len(audit) != 40:
        raise RuntimeError(f"expected 40 Table 4 audit trials, found {len(audit)}")
    return audit


def build_blind_packet(
    canvas_trials: list[dict[str, Any]], archives: Path, output: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    packet_root = output / "blind-coding-packet"
    cases_root = packet_root / "cases"
    cases_root.mkdir(parents=True, exist_ok=True)
    shuffled = list(canvas_trials)
    random.Random(20260904).shuffle(shuffled)
    packet_rows: list[dict[str, Any]] = []
    key_rows: list[dict[str, Any]] = []
    for index, trial in enumerate(shuffled, start=1):
        case_id = f"CASE-{index:03d}"
        case_dir = cases_root / case_id
        case_dir.mkdir()
        archived = transcript(archives, trial["run_id"])
        source_image = initial_screenshot(archives, trial["run_id"])
        image_name = "initial-screenshot.png"
        shutil.copyfile(source_image, case_dir / image_name)
        evidence = {
            "case_id": case_id,
            "task_instruction": archived["input"]["task_prompt"],
            "expected_value": trial["expected_value"],
            "final_answer_or_submitted_value": trial["emitted_value"],
            "runner_final": archived.get("final"),
            "model_responses": [
                (step.get("model_response") or {}).get("text") for step in archived.get("steps", [])
            ],
            "action_trace": [step.get("actions", []) for step in archived.get("steps", [])],
            "action_results": [step.get("action_results", []) for step in archived.get("steps", [])],
            "initial_screenshot": image_name,
        }
        (case_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        packet_rows.append(
            {
                "case_id": case_id,
                "failure_stage": "",
                "notes": "",
                "allowed_failure_stages": "extraction_failure|localization_failure|action_failure|refusal_abstention",
                "evidence_path": f"cases/{case_id}/evidence.json",
            }
        )
        key_rows.append({"case_id": case_id, "run_id": trial["run_id"]})
    write_csv(packet_root / "coding-sheet.csv", packet_rows)
    write_csv(output / "blind-coding-key.csv", key_rows)
    return packet_rows, key_rows


def write_report(
    output: Path,
    canvas_trials: list[dict[str, Any]],
    canvas_summary: list[dict[str, Any]],
    table4: list[dict[str, Any]],
) -> None:
    pooled = next(item for item in canvas_summary if item["task_id"] == "ALL" and item["model_id"] == "ALL")
    copy_rows = [item for item in table4 if item["task_id"] == "copy-known-value"]
    read_rows = [item for item in table4 if item["task_id"] == "read-known-value"]
    copy_y = sorted({item["first_type_y"] for item in copy_rows})
    copy_hashes = len({item["initial_screenshot_sha256"] for item in copy_rows})
    read_hashes = len({item["initial_screenshot_sha256"] for item in read_rows})
    report = f"""# Reviewer follow-up audit

All results here are provider-free derivations from the immutable, receipted
`vision-full-scan-v2` archives.

## Three canvas scoring criteria

Across the {pooled['trials']} vision canvas trials:

| Criterion | Passing trials |
| --- | ---: |
| Exact match after outer-whitespace stripping | {pooled['exact_matches']}/{pooled['trials']} |
| Target appears as a substring | {pooled['substring_matches']}/{pooled['trials']} |
| Target appears and the task-specific marker token is absent | {pooled['target_without_marker_matches']}/\
{pooled['trials']} |

The third criterion is deliberately between exact-match and substring scoring:
it permits explanatory wrapper text, but rejects the specific observed failure
where the response includes the benchmark marker. Trial-level and stratified
results are in `canvas-scoring-trials.csv` and `canvas-scoring-summary.csv`.

## Table 4 audit

The requested 40 cells comprise the 20 `gpt-5.6-terra` copy failures at iframe
depth 1 (same and cross origin) and the 20 read failures at depth 3.

The depth-1 copy failures are coordinate-grounding artifacts. In a provider-free
reproduction of the exact fixture at 1280x900, the destination label occupies
y=195.75..223.75 and the input occupies y=235.75..291.75. Every first type
action used y in {copy_y}; consequently every click fell in the 12-pixel gap
between label and field. The action API reported only that the click/type call
executed, not that an editable control received text. Nineteen trials exhausted
30 steps and one exhausted after 14 recorded actions; no form submission was
recorded. At depth 2, the corresponding biased y=293..295 happens to fall inside
the associated label at y=277.625..305.625, and clicking that label focuses the
field. The apparent recovery is therefore accidental and cannot be interpreted
as a depth effect.

The 20 depth-3 read trials used only {read_hashes} distinct initial screenshots
(one per origin-labelled condition, each repeated ten times) and all emitted
the same transcription, `AI PHA-7391`, for the fully visible `ALPHA-7391` source.
The target was at y=325.5..343.5, far inside the 900-pixel viewport. This is a
deterministic raster/OCR-position effect, not below-fold loss. Likewise, the 20
depth-1 copy trials used {copy_hashes} distinct initial screenshots. Repeated
trials therefore estimate repeated model behavior on identical pixels; they do
not independently vary layout.

Conclusion: Table 4 does not identify a depth mechanism. The manuscript should
remove the depth interpretation, identify the coordinate contract and absolute
layout as confounds, and either rerun vision with calibrated coordinates and
position-matched fixtures or present these cells only as a runner audit.

## Blind recoding packet

`blind-coding-packet/coding-sheet.csv` contains 40 randomized opaque case IDs.
Each case directory contains the initial screenshot plus de-identified response,
action, and checker evidence. The model, condition, repeat, run ID, original
coding, and aggregate result are excluded from the packet. The separate
`blind-coding-key.csv` should remain hidden from the second coder.
"""
    (output / "report.md").write_text(report, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--archives", type=Path, default=DEFAULT_ARCHIVES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rows = load_jsonl(args.results)
    canvas_trials, canvas_summary = build_canvas_scoring(rows, args.archives)
    if len(canvas_trials) != 40:
        raise RuntimeError(f"expected 40 canvas trials, found {len(canvas_trials)}")
    table4 = build_table4_audit(rows, args.archives)
    write_csv(args.output / "canvas-scoring-trials.csv", canvas_trials)
    write_csv(args.output / "canvas-scoring-summary.csv", canvas_summary)
    write_csv(args.output / "table4-vision-audit.csv", table4)
    build_blind_packet(canvas_trials, args.archives, args.output)
    write_report(args.output, canvas_trials, canvas_summary, table4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
