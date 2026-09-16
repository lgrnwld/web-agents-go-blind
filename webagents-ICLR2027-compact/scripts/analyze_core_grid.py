#!/usr/bin/env python3
# ruff: noqa: E501
"""Provider-free analysis of the frozen four-architecture core grid.

Inputs are completed, receipted sweep outputs. DOM and AX use their versioned
final-scanner rescores; vision and CDP use their completed sweep receipts. The
script refuses incomplete/unbalanced data and emits machine-readable tables,
publication SVGs, methods, a concise report, and a hash receipt.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

Z95 = 1.959963984540054
ARCHITECTURES = (
    "dom_extraction",
    "accessibility_tree",
    "vision",
    "cdp_frame_traversal",
)
ARCH_LABELS = {
    "dom_extraction": "DOM extraction",
    "accessibility_tree": "Accessibility tree",
    "vision": "Vision",
    "cdp_frame_traversal": "CDP traversal",
}
COLORS = {
    "dom_extraction": "#0072B2",
    "accessibility_tree": "#E69F00",
    "vision": "#009E73",
    "cdp_frame_traversal": "#CC79A7",
}
DEFAULT_INPUTS = {
    "dom_extraction": "artifacts/reconciled/final-scanner-v1/dom-full-r5-v1",
    "accessibility_tree": "artifacts/reconciled/final-scanner-v1/ax-full-v1-infra-retry-v1",
    "vision": "artifacts/sweeps-r5/vision-final/vision-full-scan-v2",
    "cdp_frame_traversal": "artifacts/sweeps-r5/cdp-final/cdp-frame-traversal-full-v1",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
    return rows


def wilson(successes: int, trials: int) -> tuple[float | None, float | None, float | None]:
    if not 0 <= successes <= trials:
        raise ValueError("Wilson interval requires 0 <= successes <= trials")
    if trials == 0:
        return None, None, None
    rate = successes / trials
    z2 = Z95**2
    denominator = 1 + z2 / trials
    center = (rate + z2 / (2 * trials)) / denominator
    margin = Z95 * math.sqrt(rate * (1 - rate) / trials + z2 / (4 * trials**2)) / denominator
    return rate, max(0.0, center - margin), min(1.0, center + margin)


def percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    location = (len(ordered) - 1) * probability
    lower = math.floor(location)
    upper = math.ceil(location)
    if lower == upper:
        return ordered[lower]
    fraction = location - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def mean_or_none(values: Iterable[float | int | None]) -> float | None:
    numeric = [float(value) for value in values if value is not None]
    return fmean(numeric) if numeric else None


def write_csv(path: Path, rows: list[dict[str, Any]], fields: Sequence[str] | None = None) -> None:
    if fields is None:
        fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def verify_input(root: Path, architecture: str, relative: str) -> dict[str, Any]:
    directory = root / relative
    report_path = directory / "report.json"
    results_path = directory / "results.jsonl"
    resolved_path = directory / "resolved-spec.json"
    report = load_json(report_path)
    if report.get("verdict") != "complete":
        raise RuntimeError(f"{architecture} report is not complete")
    if report.get("results_sha256") != sha256_file(results_path):
        raise RuntimeError(f"{architecture} report/results hash mismatch")
    if architecture in {"dom_extraction", "accessibility_tree"}:
        receipt_path = directory / "rescore-receipt.json"
        receipt = load_json(receipt_path)
        if receipt.get("agent_class") != architecture:
            raise RuntimeError(f"{architecture} rescore receipt class mismatch")
        if receipt.get("corrected_results_sha256") != sha256_file(results_path):
            raise RuntimeError(f"{architecture} corrected result hash mismatch")
        if receipt.get("corrected_report_sha256") != sha256_file(report_path):
            raise RuntimeError(f"{architecture} corrected report hash mismatch")
        if receipt.get("scoring_policy") != "initial-task-page-marker-v2":
            raise RuntimeError(f"{architecture} was not rescored with the final policy")
    else:
        receipt_path = directory / "sweep-receipt.json"
        receipt = load_json(receipt_path)
        if receipt.get("verdict") != "complete":
            raise RuntimeError(f"{architecture} receipt is not complete")
        if receipt.get("report_sha256") != sha256_file(report_path):
            raise RuntimeError(f"{architecture} report/receipt hash mismatch")
        if receipt.get("results_sha256") != sha256_file(results_path):
            raise RuntimeError(f"{architecture} results/receipt hash mismatch")
    resolved = load_json(resolved_path)
    rows = load_jsonl(results_path)
    final: dict[str, dict[str, Any]] = {}
    for row in rows:
        final[row["logical_trial_id"]] = row
    valid = [row for row in final.values() if row.get("counts_toward_cell")]
    if len(valid) != 440:
        raise RuntimeError(f"{architecture} has {len(valid)} valid logical trials, expected 440")
    return {
        "architecture": architecture,
        "path": relative,
        "directory": directory,
        "report": report,
        "receipt": receipt,
        "receipt_path": receipt_path,
        "resolved": resolved,
        "results": valid,
        "receipt_sha256": sha256_file(receipt_path),
        "results_sha256": sha256_file(results_path),
    }


def axes_from(resolved: dict[str, Any]) -> list[dict[str, Any]]:
    axes = resolved["spec"]["axes"]
    return [
        {
            "id": axis["id"],
            "levels": [
                {"id": level["id"], "condition_id": level["condition_id"], "order": order}
                for order, level in enumerate(axis["levels"])
            ],
        }
        for axis in axes
    ]


def cell_key(row: dict[str, Any]) -> tuple[str, int, str, str]:
    return row["task_id"], int(row["task_revision"]), row["model_id"], row["condition_id"]


def curve_statistics(rates: Sequence[float], *, cliff_threshold: float = 2 / 3) -> dict[str, Any]:
    if len(rates) < 2:
        raise ValueError("curve needs at least two levels")
    auc = sum((left + right) / 2 for left, right in zip(rates, rates[1:])) / (len(rates) - 1)
    total_drop = rates[0] - rates[-1]
    mean_drop = rates[0] - fmean(rates[1:])
    largest_drop = max(0.0, *(left - right for left, right in zip(rates, rates[1:])))
    cliff_ratio = largest_drop / total_drop if total_drop > 0 else 0.0
    if total_drop < 0.20:
        classifier = "no_material_net_drop"
    elif cliff_ratio >= cliff_threshold:
        classifier = "cliff"
    else:
        classifier = "slope"
    return {
        "auc": auc,
        "mean_drop": mean_drop,
        "total_drop": total_drop,
        "largest_single_level_drop": largest_drop,
        "cliff_ratio": cliff_ratio,
        "cliff_classifier": classifier,
        "cliff_at_0_50": total_drop >= 0.20 and cliff_ratio >= 0.50,
        "cliff_at_0_75": total_drop >= 0.20 and cliff_ratio >= 0.75,
    }


def resample_rate(values: Sequence[bool], rng: random.Random) -> float:
    return sum(values[rng.randrange(len(values))] for _ in values) / len(values)


def bootstrap_curve(
    level_values: Sequence[Sequence[bool]], samples: int, rng: random.Random
) -> dict[str, tuple[float, float]]:
    distributions: dict[str, list[float]] = defaultdict(list)
    for _ in range(samples):
        rates = [resample_rate(values, rng) for values in level_values]
        stats = curve_statistics(rates)
        for name in ("auc", "mean_drop", "total_drop", "largest_single_level_drop", "cliff_ratio"):
            distributions[name].append(float(stats[name]))
    return {name: (percentile(values, 0.025), percentile(values, 0.975)) for name, values in distributions.items()}


def two_way_shares(
    component_rates: dict[tuple[str, str, str], float],
    components: Sequence[str],
    models: Sequence[str],
) -> dict[str, float]:
    ss_architecture = 0.0
    ss_model = 0.0
    ss_interaction = 0.0
    for component in components:
        values = {
            (architecture, model): component_rates[(component, architecture, model)]
            for architecture in ARCHITECTURES
            for model in models
        }
        grand = fmean(values.values())
        arch_means = {
            architecture: fmean(values[(architecture, model)] for model in models)
            for architecture in ARCHITECTURES
        }
        model_means = {
            model: fmean(values[(architecture, model)] for architecture in ARCHITECTURES)
            for model in models
        }
        ss_architecture += len(models) * sum((value - grand) ** 2 for value in arch_means.values())
        ss_model += len(ARCHITECTURES) * sum((value - grand) ** 2 for value in model_means.values())
        ss_interaction += sum(
            (values[(architecture, model)] - arch_means[architecture] - model_means[model] + grand) ** 2
            for architecture in ARCHITECTURES
            for model in models
        )
    total = ss_architecture + ss_model + ss_interaction
    if total == 0:
        return {"architecture": 0.0, "model": 0.0, "interaction": 0.0, "total_ss": 0.0}
    return {
        "architecture": ss_architecture / total,
        "model": ss_model / total,
        "interaction": ss_interaction / total,
        "total_ss": total,
    }


def variance_decomposition(
    raw: dict[str, dict[tuple[str, int, str, str], list[dict[str, Any]]]],
    axes: list[dict[str, Any]],
    tasks: Sequence[tuple[str, int]],
    models: Sequence[str],
    outcome: str,
    samples: int,
    rng: random.Random,
) -> list[dict[str, Any]]:
    axis_groups = ["all"] + [axis["id"] for axis in axes]
    output: list[dict[str, Any]] = []
    for axis_group in axis_groups:
        selected_axes = axes if axis_group == "all" else [axis for axis in axes if axis["id"] == axis_group]
        component_defs = [
            (f"{axis['id']}::{level['id']}", axis["levels"][0]["condition_id"], level["condition_id"])
            for axis in selected_axes
            for level in axis["levels"][1:]
        ]
        components = [definition[0] for definition in component_defs]

        def rates(resample: bool) -> dict[tuple[str, str, str], float]:
            values: dict[tuple[str, str, str], float] = {}
            for component, baseline_condition, condition in component_defs:
                for architecture in ARCHITECTURES:
                    for model in models:
                        drops: list[float] = []
                        for task_id, task_revision in tasks:
                            baseline_rows = raw[architecture][(task_id, task_revision, model, baseline_condition)]
                            condition_rows = raw[architecture][(task_id, task_revision, model, condition)]
                            baseline_values = [bool(row[outcome]) for row in baseline_rows]
                            condition_values = [bool(row[outcome]) for row in condition_rows]
                            baseline_rate = resample_rate(baseline_values, rng) if resample else fmean(baseline_values)
                            condition_rate = resample_rate(condition_values, rng) if resample else fmean(condition_values)
                            drops.append(baseline_rate - condition_rate)
                        values[(component, architecture, model)] = fmean(drops)
            return values

        point = two_way_shares(rates(False), components, models)
        distributions = {"architecture": [], "model": [], "interaction": []}
        for _ in range(samples):
            sample = two_way_shares(rates(True), components, models)
            for effect in distributions:
                distributions[effect].append(sample[effect])
        for effect in ("architecture", "model", "interaction"):
            output.append(
                {
                    "outcome": outcome,
                    "axis_scope": axis_group,
                    "effect": effect,
                    "variance_share": point[effect],
                    "ci_lower": percentile(distributions[effect], 0.025),
                    "ci_upper": percentile(distributions[effect], 0.975),
                    "total_ss": point["total_ss"],
                    "component_count": len(components),
                    "bootstrap_samples": samples,
                }
            )
    return output


def svg_document(width: int, height: int, body: str, title: str, description: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">\n'
        f'<title id="title">{html.escape(title)}</title><desc id="desc">{html.escape(description)}</desc>\n'
        '<style>text{font-family:Arial,Helvetica,sans-serif;fill:#202124} .small{font-size:11px} '
        '.label{font-size:12px}.panel{font-size:14px;font-weight:600}.title{font-size:20px;font-weight:600} '
        '.subtitle{font-size:12px;fill:#5f6368}.grid{stroke:#d9dde3;stroke-width:1}.axis{stroke:#5f6368;stroke-width:1} '
        '.divider{stroke:#aeb4bc;stroke-width:1}.ci{stroke-width:2}</style>\n'
        f'<rect width="{width}" height="{height}" fill="#fff"/>{body}\n</svg>\n'
    )


def blend_hex(start: str, end: str, amount: float) -> str:
    amount = max(0.0, min(1.0, amount))
    left = tuple(int(start[index : index + 2], 16) for index in (1, 3, 5))
    right = tuple(int(end[index : index + 2], 16) for index in (1, 3, 5))
    return "#" + "".join(f"{round(a + (b - a) * amount):02x}" for a, b in zip(left, right))


def condition_layout(axes: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    axis_labels = {
        "iframe-same-origin-depth": "Same-origin iframe",
        "iframe-cross-origin-depth": "Cross-origin iframe",
        "shadow-containment": "Shadow containment",
        "rendered-target": "Rendered target",
    }
    level_labels = {
        "depth-1": "depth 1",
        "depth-2": "depth 2",
        "depth-3": "depth 3",
        "open": "open",
        "closed": "closed",
        "nested-open-open": "nested open",
        "canvas": "canvas",
    }
    output = [("Baseline", "Flat DOM baseline", "level-0")]
    for axis in axes:
        group = axis_labels[axis["id"]]
        for level in axis["levels"][1:]:
            output.append((group, f"{group} · {level_labels.get(level['id'], level['id'])}", level["condition_id"]))
    return output


def figure_paired_curves(rows: list[dict[str, Any]], axes: list[dict[str, Any]], path: Path) -> None:
    conditions = condition_layout(axes)
    short_labels = {
        "dom_extraction": "DOM",
        "accessibility_tree": "AX tree",
        "vision": "Vision",
        "cdp_frame_traversal": "CDP",
    }
    width, height = 1240, 690
    left, top, cell_w, cell_h, panel_gap = 270, 132, 105, 44, 38
    panel_w = cell_w * len(ARCHITECTURES)
    body = [
        '<text x="24" y="32" class="title">Architecture differences emerge at canvas; reachability does not guarantee success</text>',
        '<text x="24" y="54" class="subtitle">Observed rates pooled across 2 tasks × 2 models (N=40 per cell); Wilson intervals are retained in headline-curves.csv</text>',
    ]
    for panel_index, (outcome, panel_label) in enumerate((("reachable", "Evidence reachable"), ("task_success", "Task success"))):
        x0 = left + panel_index * (panel_w + panel_gap)
        body.append(f'<text x="{x0 + panel_w / 2:.1f}" y="84" text-anchor="middle" class="panel">{panel_label}</text>')
        for architecture_index, architecture in enumerate(ARCHITECTURES):
            x = x0 + architecture_index * cell_w + cell_w / 2
            body.append(f'<line x1="{x-18:.1f}" y1="102" x2="{x+18:.1f}" y2="102" stroke="{COLORS[architecture]}" stroke-width="4"/>')
            body.append(f'<text x="{x:.1f}" y="121" text-anchor="middle" class="label">{short_labels[architecture]}</text>')
        previous_group = None
        for row_index, (group, label, condition_id) in enumerate(conditions):
            y = top + row_index * cell_h
            if panel_index == 0:
                if previous_group is not None and group != previous_group:
                    body.append(f'<line x1="24" y1="{y-4}" x2="{width-24}" y2="{y-4}" class="divider" opacity="0.65"/>')
                body.append(f'<text x="{left-16}" y="{y+27}" text-anchor="end" class="label">{html.escape(label)}</text>')
                previous_group = group
            for architecture_index, architecture in enumerate(ARCHITECTURES):
                row = next(item for item in rows if item["architecture"] == architecture and item["condition_id"] == condition_id)
                value = float(row[f"{outcome}_rate"])
                fill = blend_hex("#f1f4f7", "#176b87", value)
                text_fill = "#fff" if value >= 0.58 else "#202124"
                x = x0 + architecture_index * cell_w
                body.append(f'<rect x="{x+2}" y="{y+2}" width="{cell_w-4}" height="{cell_h-4}" fill="{fill}"/>')
                body.append(f'<text x="{x+cell_w/2:.1f}" y="{y+28}" text-anchor="middle" class="label" style="fill:{text_fill};font-weight:600">{value:.0%}</text>')
    legend_x, legend_y = left, top + len(conditions) * cell_h + 28
    for index, value in enumerate((0.0, 0.25, 0.5, 0.75, 1.0)):
        x = legend_x + index * 42
        body.append(f'<rect x="{x}" y="{legend_y}" width="42" height="10" fill="{blend_hex("#f1f4f7", "#176b87", value)}"/>')
    body.append(f'<text x="{legend_x}" y="{legend_y+26}" class="small">0%</text><text x="{legend_x+210}" y="{legend_y+26}" text-anchor="end" class="small">100%</text>')
    body.append(f'<text x="{legend_x+236}" y="{legend_y+14}" class="small">observed rate</text>')
    path.write_text(svg_document(width, height, "".join(body), "Architecture reachability and task success matrix", "Observed reachability and task-success rates for four architectures across eleven structural conditions."), encoding="utf-8")


def figure_variance(rows: list[dict[str, Any]], path: Path) -> None:
    selected = [row for row in rows if row["axis_scope"] == "all"]
    width, height = 1000, 310
    body = [
        '<text x="24" y="32" class="title">Architecture accounts for reachability variation; task success also depends on model interaction</text>',
        '<text x="24" y="54" class="subtitle">Descriptive share of curve-shape sum of squares across the tested grid</text>',
    ]
    effects = ("architecture", "interaction", "model")
    effect_colors = {"architecture": "#0072B2", "model": "#E69F00", "interaction": "#009E73"}
    x0, bar_w, bar_h = 210, 740, 48
    for outcome_index, outcome in enumerate(("reachable", "task_success")):
        y = 92 + outcome_index * 82
        body.append(f'<text x="{x0-18}" y="{y+30}" text-anchor="end" class="panel">{outcome.replace("_", " ").title()}</text>')
        cursor = x0
        for effect in effects:
            row = next(row for row in selected if row["outcome"] == outcome and row["effect"] == effect)
            share = float(row["variance_share"])
            segment_w = share * bar_w
            if segment_w <= 0:
                continue
            body.append(f'<rect x="{cursor:.1f}" y="{y}" width="{segment_w:.1f}" height="{bar_h}" fill="{effect_colors[effect]}"/>')
            text_fill = "#202124" if effect == "model" else "#fff"
            label = effect.title()
            body.append(f'<text x="{cursor+segment_w/2:.1f}" y="{y+21}" text-anchor="middle" class="label" style="fill:{text_fill};font-weight:600">{label}</text>')
            body.append(f'<text x="{cursor+segment_w/2:.1f}" y="{y+38}" text-anchor="middle" class="small" style="fill:{text_fill}">{share:.1%}</text>')
            cursor += segment_w
        body.append(f'<rect x="{x0}" y="{y}" width="{bar_w}" height="{bar_h}" fill="none" stroke="#5f6368"/>')
    task_rows = {row["effect"]: row for row in selected if row["outcome"] == "task_success"}
    body.append(
        '<text x="210" y="270" class="small">Bootstrap 95% CI for task success: '
        f'architecture {float(task_rows["architecture"]["ci_lower"]):.0%}–{float(task_rows["architecture"]["ci_upper"]):.0%}; '
        f'interaction {float(task_rows["interaction"]["ci_lower"]):.0%}–{float(task_rows["interaction"]["ci_upper"]):.0%}; '
        f'model {float(task_rows["model"]["ci_lower"]):.0%}–{float(task_rows["model"]["ci_upper"]):.0%}</text>'
    )
    path.write_text(svg_document(width, height, "".join(body), "Variance decomposition", "Architecture, model, and interaction shares of curve-shape sum of squares with bootstrap intervals."), encoding="utf-8")


def figure_cost(rows: list[dict[str, Any]], axes: list[dict[str, Any]], path: Path) -> None:
    conditions = condition_layout(axes)
    short_labels = {"dom_extraction": "DOM", "accessibility_tree": "AX tree", "vision": "Vision", "cdp_frame_traversal": "CDP"}
    baselines = {
        architecture: float(next(row for row in rows if row["architecture"] == architecture and row["condition_id"] == "level-0")["mean_total_tokens"])
        for architecture in ARCHITECTURES
    }
    ratios = [
        float(next(row for row in rows if row["architecture"] == architecture and row["condition_id"] == condition_id)["mean_total_tokens"]) / baselines[architecture]
        for _, _, condition_id in conditions
        for architecture in ARCHITECTURES
    ]
    max_ratio = max(ratios)

    def cost_color(value: float) -> str:
        if value <= 1:
            return blend_hex("#4C78A8", "#f3f3f3", max(0.0, min(1.0, (value - 0.5) / 0.5)))
        return blend_hex("#f3f3f3", "#B2182B", (value - 1) / (max_ratio - 1))

    width, height = 800, 675
    left, top, cell_w, cell_h = 285, 112, 115, 44
    body = [
        '<text x="24" y="32" class="title">Token cost inflation is architecture- and condition-specific</text>',
        '<text x="24" y="54" class="subtitle">Mean input + output tokens per trial, relative to each architecture’s flat-DOM baseline</text>',
    ]
    for architecture_index, architecture in enumerate(ARCHITECTURES):
        x = left + architecture_index * cell_w + cell_w / 2
        body.append(f'<line x1="{x-20:.1f}" y1="78" x2="{x+20:.1f}" y2="78" stroke="{COLORS[architecture]}" stroke-width="4"/>')
        body.append(f'<text x="{x:.1f}" y="99" text-anchor="middle" class="label">{short_labels[architecture]}</text>')
    previous_group = None
    for row_index, (group, label, condition_id) in enumerate(conditions):
        y = top + row_index * cell_h
        if previous_group is not None and group != previous_group:
            body.append(f'<line x1="24" y1="{y-4}" x2="{width-24}" y2="{y-4}" class="divider" opacity="0.65"/>')
        body.append(f'<text x="{left-16}" y="{y+27}" text-anchor="end" class="label">{html.escape(label)}</text>')
        previous_group = group
        for architecture_index, architecture in enumerate(ARCHITECTURES):
            row = next(item for item in rows if item["architecture"] == architecture and item["condition_id"] == condition_id)
            ratio = float(row["mean_total_tokens"]) / baselines[architecture]
            x = left + architecture_index * cell_w
            fill = cost_color(ratio)
            text_fill = "#fff" if ratio > 1 + 0.55 * (max_ratio - 1) or ratio < 0.65 else "#202124"
            body.append(f'<rect x="{x+2}" y="{y+2}" width="{cell_w-4}" height="{cell_h-4}" fill="{fill}"/>')
            body.append(f'<text x="{x+cell_w/2:.1f}" y="{y+28}" text-anchor="middle" class="label" style="fill:{text_fill};font-weight:600">{ratio:.2f}×</text>')
    legend_y = top + len(conditions) * cell_h + 24
    for index, value in enumerate((0.5, 1.0, 2.0, 4.0, max_ratio)):
        x = left + index * 62
        body.append(f'<rect x="{x}" y="{legend_y}" width="62" height="10" fill="{cost_color(value)}"/>')
        body.append(f'<text x="{x+31}" y="{legend_y+25}" text-anchor="middle" class="small">{value:.1f}×</text>')
    body.append(f'<text x="{left+332}" y="{legend_y+13}" class="small">relative tokens per trial</text>')
    path.write_text(svg_document(width, height, "".join(body), "Relative token cost matrix", "Mean total tokens per trial relative to each architecture's flat-DOM baseline across eleven structural conditions."), encoding="utf-8")


def figure_confabulation(rows: list[dict[str, Any]], path: Path) -> None:
    width, height = 920, 365
    total_unreachable = sum(int(row["unreachable_trials"]) for row in rows)
    total_successes = sum(int(row["task_successes"]) for row in rows)
    body = [
        f'<text x="24" y="32" class="title">{total_successes} successes in {total_unreachable} trials where evidence was unreachable</text>',
        '<text x="24" y="54" class="subtitle">Observed task-success rate with two-sided Wilson 95% interval; x-axis is intentionally limited to 12%</text>',
    ]
    x0, y0, panel_w, row_h, maximum = 250, 92, 560, 52, 0.12
    for tick in range(5):
        x = x0 + panel_w * tick / 4
        body.append(f'<line x1="{x:.1f}" y1="{y0-8}" x2="{x:.1f}" y2="{y0+row_h*4-8}" class="grid"/>')
        body.append(f'<text x="{x:.1f}" y="{y0+row_h*4+12}" text-anchor="middle" class="small">{maximum*tick/4:.0%}</text>')
    for index, architecture in enumerate(ARCHITECTURES):
        row = next(row for row in rows if row["architecture"] == architecture)
        y = y0 + index * row_h + 16
        body.append(f'<text x="{x0-18}" y="{y+4}" text-anchor="end" class="label">{html.escape(ARCH_LABELS[architecture])}</text>')
        if row["rate"] is None:
            body.append(f'<line x1="{x0}" y1="{y}" x2="{x0+panel_w}" y2="{y}" stroke="#aeb4bc" stroke-dasharray="4 4"/>')
            body.append(f'<text x="{x0+12}" y="{y-8}" class="small">Not estimable: no unreachable-evidence trials</text>')
            continue
        rate = float(row["rate"])
        low = float(row["ci_lower"])
        high = float(row["ci_upper"])
        x_low = x0 + panel_w * low / maximum
        x_high = x0 + panel_w * high / maximum
        x_rate = x0 + panel_w * rate / maximum
        body.append(f'<line x1="{x_low:.1f}" y1="{y}" x2="{x_high:.1f}" y2="{y}" class="ci" stroke="{COLORS[architecture]}"/>')
        body.append(f'<line x1="{x_high:.1f}" y1="{y-7}" x2="{x_high:.1f}" y2="{y+7}" stroke="{COLORS[architecture]}" stroke-width="2"/>')
        body.append(f'<circle cx="{x_rate:.1f}" cy="{y}" r="5" fill="{COLORS[architecture]}"/>')
        body.append(f'<text x="{x_high+10:.1f}" y="{y+4}" class="small">{int(row["task_successes"])}/{int(row["unreachable_trials"])} · CI 0–{high:.1%}</text>')
    body.append(f'<line x1="{x0}" y1="{y0+row_h*4-8}" x2="{x0+panel_w}" y2="{y0+row_h*4-8}" class="axis"/>')
    body.append(f'<text x="{x0+panel_w/2:.1f}" y="{height-18}" text-anchor="middle" class="label">Task success among unreachable-evidence trials</text>')
    path.write_text(svg_document(width, height, "".join(body), "Confabulation proxy", "Task-success rate among trials where the architecture evidence did not contain the reachability marker."), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path("analysis/core-grid-v1"))
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260901)
    parser.add_argument(
        "--created-at",
        help="fixed ISO-8601 timestamp for a bit-for-bit reproducible analysis receipt",
    )
    parser.add_argument(
        "--reconciliation-created-at",
        help="fixed ISO-8601 timestamp for a bit-for-bit reproducible reconciliation manifest",
    )
    for architecture, default in DEFAULT_INPUTS.items():
        parser.add_argument(f"--{architecture.replace('_', '-')}", default=default)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.repo_root.resolve()
    output = args.output_dir if args.output_dir.is_absolute() else root / args.output_dir
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing analysis: {output}")
    output.mkdir(parents=True)
    figures = output / "figures"
    figures.mkdir()
    rng = random.Random(args.seed)
    created_at = args.created_at or datetime.now(UTC).isoformat()
    reconciliation_created_at = args.reconciliation_created_at or datetime.now(UTC).isoformat()
    inputs = {
        architecture: str(getattr(args, architecture))
        for architecture in ARCHITECTURES
    }
    datasets = {architecture: verify_input(root, architecture, inputs[architecture]) for architecture in ARCHITECTURES}
    axes = axes_from(datasets[ARCHITECTURES[0]]["resolved"])
    for architecture in ARCHITECTURES[1:]:
        if axes_from(datasets[architecture]["resolved"]) != axes:
            raise RuntimeError(f"{architecture} axis definition differs from the core grid")

    raw: dict[str, dict[tuple[str, int, str, str], list[dict[str, Any]]]] = {}
    for architecture, dataset in datasets.items():
        grouped: dict[tuple[str, int, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in dataset["results"]:
            grouped[cell_key(row)].append(row)
        if any(len(rows) != 10 for rows in grouped.values()) or len(grouped) != 44:
            counts = sorted(set(len(rows) for rows in grouped.values()))
            raise RuntimeError(f"{architecture} is unbalanced: {len(grouped)} cells, counts {counts}")
        raw[architecture] = grouped
    tasks = sorted({(key[0], key[1]) for key in raw[ARCHITECTURES[0]]})
    models = sorted({key[2] for key in raw[ARCHITECTURES[0]]})
    if len(tasks) != 2 or len(models) != 2:
        raise RuntimeError(f"expected 2 tasks and 2 models; observed {tasks}, {models}")

    cell_rows: list[dict[str, Any]] = []
    for architecture in ARCHITECTURES:
        for (task_id, revision, model_id, condition_id), rows in sorted(raw[architecture].items()):
            reachable = sum(row["reachable"] is True for row in rows)
            successful = sum(row["task_success"] is True for row in rows)
            blind = sum(row["self_reported_blindness"] is True for row in rows)
            rr, rl, ru = wilson(reachable, len(rows))
            sr, sl, su = wilson(successful, len(rows))
            br, bl, bu = wilson(blind, len(rows))
            total_tokens = [int(row["input_tokens"]) + int(row["output_tokens"]) for row in rows]
            actions = [int(row["actions"]) for row in rows]
            cell_rows.append({
                "architecture": architecture, "task_id": task_id, "task_revision": revision,
                "model_id": model_id, "condition_id": condition_id, "trials": len(rows),
                "reachable_successes": reachable, "reachability_rate": rr, "reachability_ci_lower": rl, "reachability_ci_upper": ru,
                "task_successes": successful, "task_success_rate": sr, "task_success_ci_lower": sl, "task_success_ci_upper": su,
                "blindness_reports": blind, "blindness_rate": br, "blindness_ci_lower": bl, "blindness_ci_upper": bu,
                "reachable_success": sum(row["reachable"] is True and row["task_success"] is True for row in rows),
                "reachable_failure": sum(row["reachable"] is True and row["task_success"] is False for row in rows),
                "unreachable_success": sum(row["reachable"] is False and row["task_success"] is True for row in rows),
                "unreachable_failure": sum(row["reachable"] is False and row["task_success"] is False for row in rows),
                "mean_actions": fmean(actions), "mean_input_tokens": fmean(int(row["input_tokens"]) for row in rows),
                "mean_output_tokens": fmean(int(row["output_tokens"]) for row in rows), "mean_total_tokens": fmean(total_tokens),
                "mean_tokens_per_action": sum(total_tokens) / sum(actions) if sum(actions) else None,
                "mean_elapsed_seconds": fmean((datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00")) - datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))).total_seconds() for row in rows),
                "mean_provider_cost_usd": mean_or_none(row.get("provider_cost_usd") for row in rows),
            })
    write_csv(output / "cell-estimates.csv", cell_rows)
    (output / "cell-estimates.json").write_text(json.dumps(cell_rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    curve_rows: list[dict[str, Any]] = []
    curve_summary_rows: list[dict[str, Any]] = []
    for architecture in ARCHITECTURES:
        for task_id, revision in tasks:
            for model in models:
                for axis in axes:
                    level_cell_rows: list[list[dict[str, Any]]] = []
                    for level in axis["levels"]:
                        values = raw[architecture][(task_id, revision, model, level["condition_id"])]
                        level_cell_rows.append(values)
                        reachable = sum(row["reachable"] is True for row in values)
                        successful = sum(row["task_success"] is True for row in values)
                        rr, rl, ru = wilson(reachable, len(values))
                        sr, sl, su = wilson(successful, len(values))
                        curve_rows.append({"architecture": architecture, "task_id": task_id, "task_revision": revision, "model_id": model,
                            "axis_id": axis["id"], "level_id": level["id"], "level_order": level["order"], "condition_id": level["condition_id"],
                            "trials": len(values), "reachability_rate": rr, "reachability_ci_lower": rl, "reachability_ci_upper": ru,
                            "task_success_rate": sr, "task_success_ci_lower": sl, "task_success_ci_upper": su})
                    for outcome, label in (("reachable", "reachability"), ("task_success", "task_success")):
                        level_values = [[bool(row[outcome]) for row in values] for values in level_cell_rows]
                        stats = curve_statistics([fmean(values) for values in level_values])
                        intervals = bootstrap_curve(level_values, args.bootstrap_samples, rng)
                        row = {"architecture": architecture, "task_id": task_id, "task_revision": revision, "model_id": model,
                            "axis_id": axis["id"], "outcome": label, "levels": len(level_values), **stats,
                            "bootstrap_samples": args.bootstrap_samples}
                        for metric, (low, high) in intervals.items():
                            row[f"{metric}_ci_lower"] = low
                            row[f"{metric}_ci_upper"] = high
                        curve_summary_rows.append(row)
    write_csv(output / "axis-curves.csv", curve_rows)
    write_csv(output / "curve-summaries.csv", curve_summary_rows)

    reliability_rows: list[dict[str, Any]] = []
    for cell in cell_rows:
        for outcome, successes in (("reachability", cell["reachable_successes"]), ("task_success", cell["task_successes"])):
            for k in range(1, int(cell["trials"]) + 1):
                rate, low, high = wilson(int(successes), int(cell["trials"]))
                reliability_rows.append({"architecture": cell["architecture"], "task_id": cell["task_id"], "task_revision": cell["task_revision"],
                    "model_id": cell["model_id"], "condition_id": cell["condition_id"], "outcome": outcome,
                    "k": k, "n": cell["trials"], "successes": successes, "meets_k_of_n": int(successes) >= k,
                    "observed_rate": rate, "wilson_ci_lower": low, "wilson_ci_upper": high})
    write_csv(output / "k-of-n-reliability.csv", reliability_rows)

    headline_rows: list[dict[str, Any]] = []
    cost_rows: list[dict[str, Any]] = []
    for architecture in ARCHITECTURES:
        for axis in axes:
            for level in axis["levels"]:
                pooled = [row for key, rows in raw[architecture].items() if key[3] == level["condition_id"] for row in rows]
                reachable = sum(row["reachable"] is True for row in pooled)
                successful = sum(row["task_success"] is True for row in pooled)
                rr, rl, ru = wilson(reachable, len(pooled))
                sr, sl, su = wilson(successful, len(pooled))
                headline_rows.append({"architecture": architecture, "axis_id": axis["id"], "level_id": level["id"], "level_order": level["order"],
                    "condition_id": level["condition_id"], "trials": len(pooled), "reachable_successes": reachable,
                    "reachable_rate": rr, "reachable_ci_lower": rl, "reachable_ci_upper": ru,
                    "task_successes": successful, "task_success_rate": sr, "task_success_ci_lower": sl, "task_success_ci_upper": su})
                actions = sum(int(row["actions"]) for row in pooled)
                total_tokens = sum(
                    int(row["input_tokens"]) + int(row["output_tokens"]) for row in pooled
                )
                cost_rows.append({"architecture": architecture, "axis_id": axis["id"], "level_id": level["id"], "level_order": level["order"],
                    "condition_id": level["condition_id"], "trials": len(pooled), "mean_actions": fmean(int(row["actions"]) for row in pooled),
                    "mean_input_tokens": fmean(int(row["input_tokens"]) for row in pooled), "mean_output_tokens": fmean(int(row["output_tokens"]) for row in pooled),
                    "mean_total_tokens": total_tokens / len(pooled), "mean_tokens_per_action": total_tokens / actions if actions else None,
                    "provider_cost_available": any(row.get("provider_cost_usd") is not None for row in pooled),
                    "mean_provider_cost_usd": mean_or_none(row.get("provider_cost_usd") for row in pooled)})
    write_csv(output / "headline-curves.csv", headline_rows)
    write_csv(output / "cost-by-level.csv", cost_rows)

    variance_rows = []
    for outcome in ("reachable", "task_success"):
        variance_rows.extend(variance_decomposition(raw, axes, tasks, models, outcome, args.bootstrap_samples, rng))
    write_csv(output / "variance-decomposition.csv", variance_rows)
    (output / "variance-decomposition.json").write_text(json.dumps(variance_rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    confab_rows: list[dict[str, Any]] = []
    for architecture in ARCHITECTURES:
        unreachable = [row for rows in raw[architecture].values() for row in rows if row["reachable"] is False]
        successful = sum(row["task_success"] is True for row in unreachable)
        rate, low, high = wilson(successful, len(unreachable))
        confab_rows.append({"architecture": architecture, "unreachable_trials": len(unreachable), "task_successes": successful,
            "rate": rate, "ci_lower": low, "ci_upper": high, "definition": "task success among trials with unreachable architecture evidence"})
    write_csv(output / "confabulation-proxy.csv", confab_rows)

    figure_paired_curves(headline_rows, axes, figures / "paired-reachability-success-curves.svg")
    figure_variance(variance_rows, figures / "variance-decomposition.svg")
    figure_cost(cost_rows, axes, figures / "cost-curves.svg")
    figure_confabulation(confab_rows, figures / "confabulation-proxy.svg")

    reconciliation = {
        "schema_version": "1.0", "analysis_id": "final-scanner-v1", "created_at": reconciliation_created_at,
        "provider_calls": 0, "scoring_policy": "initial-task-page-marker-v2",
        "datasets": [{"architecture": architecture, "path": dataset["path"], "receipt_sha256": dataset["receipt_sha256"],
            "results_sha256": dataset["results_sha256"], "changed_trial_count": dataset["receipt"].get("changed_trial_count")}
            for architecture, dataset in datasets.items()],
        "conclusion": "DOM and AX were rescored from immutable archives; neither required a reachability-label change under the final scanner.",
    }
    (output / "reconciliation-manifest.json").write_text(json.dumps(reconciliation, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    overall_variance = [row for row in variance_rows if row["axis_scope"] == "all"]
    report = ["# Core-grid analysis v1", "", "All four architecture sweeps are complete and balanced: 440 valid outcomes per class (1,760 total), with 10 trials in every task × model × condition cell.", "", "## Primary outputs", "",
        "- `cell-estimates.csv`: reachability, task success, self-reported blindness, joint outcomes, Wilson 95% intervals, and cost telemetry.",
        "- `headline-curves.csv`: pooled architecture-class curves (N=40 per plotted point).",
        "- `curve-summaries.csv`: normalized AUC, mean drop, total drop, largest single-level drop, the preregistered cliff ratio, and bootstrap intervals.",
        "- `k-of-n-reliability.csv`: observed k-of-N status and the binomial Wilson interval for every cell.",
        "- `variance-decomposition.csv`: architecture, model, and architecture×model curve-shape shares with stratified bootstrap intervals.",
        "- `cost-by-level.csv`: actions, tokens, and tokens/action. Provider billing cost is unavailable and is not imputed.", "", "## Overall variance shares", ""]
    for outcome in ("reachable", "task_success"):
        report.append(f"### {outcome.replace('_', ' ').title()}")
        report.append("")
        for row in overall_variance:
            if row["outcome"] == outcome:
                report.append(f"- {row['effect']}: {row['variance_share']:.1%} (bootstrap 95% CI {row['ci_lower']:.1%}–{row['ci_upper']:.1%})")
        report.append("")
    (output / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    methods = """# Statistical methods for the core grid

The analysis unit is the preregistered task × model × structural-condition cell. Every cell contains 10 valid trials. Reachability denotes whether the architecture's initial task-page evidence contained the task-specific marker; task success is determined by the external checker. Proportions use two-sided Wilson 95% confidence intervals. Pooled figure points combine the two tasks and two models within an architecture class (N=40) and are descriptive; cell-level estimates remain available for inferential inspection.

For each ordered axis and each task × model curve, normalized AUC is the equally spaced trapezoidal area. Mean drop is the level-0 rate minus the mean of all nonbaseline rates. Total drop is the first rate minus the final rate. Largest single-level drop is the largest positive adjacent decrement. The cliff ratio is largest single-level drop divided by total drop. A curve is classified as `no_material_net_drop` when total drop is below 0.20, `cliff` when total drop is at least 0.20 and the cliff ratio is at least 2/3, and `slope` otherwise. Threshold sensitivity at 0.50 and 0.75 is also emitted.

Curve-metric intervals use a deterministic stratified nonparametric bootstrap: trials are resampled with replacement within each task × model × condition cell. The variance decomposition represents curve shape as level-0-minus-level rates for every nonbaseline axis level, averaged over the two tasks. For each component, a balanced two-way decomposition partitions sums of squares into architecture class, model, and architecture × model interaction. Shares are normalized by their sum. Bootstrap confidence intervals resample trials within cells and repeat the entire decomposition. This is a descriptive variance partition over the tested roster, not a population random-effects estimate; with two models, interaction estimates should be interpreted cautiously.

The confabulation proxy is task success conditional on the architecture evidence being scored unreachable. It is reported as an operational joint-outcome rate, not as a claim about model intent. Cost outputs report recorded actions and input/output tokens. Provider-dollar cost was absent from the archived API responses and is therefore not estimated.

DOM-extraction and accessibility-tree reachability were reconciled provider-free from immutable run archives under `initial-task-page-marker-v2`. Vision and CDP use their completed receipted outputs. Product and framework names are intentionally excluded from architecture-level tables and figures; exact implementation identities remain in the control and sweep manifests for the appendix.
"""
    (output / "methods.md").write_text(methods, encoding="utf-8")

    output_files = sorted(path for path in output.rglob("*") if path.is_file() and path.name != "analysis-receipt.json")
    receipt = {"schema_version": "1.0", "analysis_id": "core-grid-v1", "created_at": created_at,
        "bootstrap_samples": args.bootstrap_samples, "random_seed": args.seed, "valid_outcomes": sum(len(dataset["results"]) for dataset in datasets.values()),
        "input_receipts": {architecture: dataset["receipt_sha256"] for architecture, dataset in datasets.items()},
        "outputs": {path.relative_to(output).as_posix(): sha256_file(path) for path in output_files}}
    receipt_path = output / "analysis-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"analysis_id": receipt["analysis_id"], "valid_outcomes": receipt["valid_outcomes"], "output_dir": str(output), "output_count": len(output_files), "receipt_sha256": sha256_file(receipt_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
