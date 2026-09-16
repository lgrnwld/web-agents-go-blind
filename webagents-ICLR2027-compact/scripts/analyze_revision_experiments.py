#!/usr/bin/env python3
"""Provider-free analysis and publication figures for the September 15 revision."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path

MODELS = ("foundry/gpt-5.6-terra", "foundry/Kimi-K2.6", "foundry/gpt-5.4", "foundry/gpt-5-mini")
LAYOUTS = (
    "canvas-inline-bar",
    "canvas-inline-two-spaces",
    "canvas-inline-gap-40px",
    "canvas-separate-line",
    "canvas-opposite-corner",
    "dom-inline-bar",
)
METRICS = ("exact_match", "substring_match", "target_without_marker", "content_exact", "delimitation_error")


def load(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def wilson(k, n):
    z = 1.959963984540054
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def aggregate(rows, dimensions, metrics=METRICS):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[k] for k in dimensions)].append(row)
    out = []
    for key, group in sorted(groups.items()):
        for metric in metrics:
            if metric not in group[0]:
                continue
            k = sum(bool(r[metric]) for r in group)
            lo, hi = wilson(k, len(group))
            out.append(
                dict(zip(dimensions, key), metric=metric, count=k, n=len(group), rate=k / len(group), low=lo, high=hi)
            )
    return out


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def paired_effect(rows, arm_key, baseline, treatment, match_keys, metric="exact_match"):
    arms = defaultdict(dict)
    for r in rows:
        if r[arm_key] in (baseline, treatment):
            key = tuple(r[k] for k in match_keys)
            assert r[arm_key] not in arms[key], (key, r[arm_key])
            arms[key][r[arm_key]] = bool(r[metric])
    assert arms and all(set(v) == {baseline, treatment} for v in arms.values())
    diffs = [int(v[treatment]) - int(v[baseline]) for v in arms.values()]
    # Repeated layouts/gaps at one randomized position form a block. Preserve
    # task strata and resample complete repeat blocks, never individual cells.
    blocks = defaultdict(list)
    for key, value in arms.items():
        task = key[match_keys.index("task_id")] if "task_id" in match_keys else "single-task"
        repeat = key[match_keys.index("repeat")] if "repeat" in match_keys else key
        blocks[(task, repeat)].append(int(value[treatment]) - int(value[baseline]))
    strata = defaultdict(list)
    for (task, _), values in blocks.items():
        strata[task].append((sum(values), len(values)))
    rng = random.Random(20260915)
    boot = []
    for _ in range(10000):
        sampled = [block for group in strata.values() for block in rng.choices(group, k=len(group))]
        boot.append(sum(block[0] for block in sampled) / sum(block[1] for block in sampled))
    boot.sort()
    improved = diffs.count(1)
    worsened = diffs.count(-1)
    discordant = improved + worsened
    p = (
        min(1.0, 2 * sum(math.comb(discordant, k) for k in range(min(improved, worsened) + 1)) / 2**discordant)
        if discordant
        else 1.0
    )
    return dict(
        pairs=len(diffs),
        bootstrap_position_blocks=len(blocks),
        baseline=sum(v[baseline] for v in arms.values()),
        treatment=sum(v[treatment] for v in arms.values()),
        difference=sum(diffs) / len(diffs),
        bootstrap_low=boot[249],
        bootstrap_high=boot[9749],
        improved=improved,
        worsened=worsened,
        mcnemar_exact_p=p,
    )


def text(c, x, y, value, size=8, bold=False):
    c.setFont("RevisionArialBold" if bold else "RevisionArial", size)
    c.setFillColorRGB(0.12, 0.17, 0.22)
    c.drawString(x, y, str(value))


COLORS = ((0.20, 0.38, 0.38), (0.60, 0.38, 0.18), (0.28, 0.42, 0.65), (0.55, 0.28, 0.50))


def axes(c, x, y, w, h, title, xlabels):
    text(c, x, y + h + 13, title, 9, True)
    c.setLineWidth(0.4)
    for val in (0, 0.5, 1):
        yy = y + h * val
        c.setStrokeColorRGB(0.82, 0.85, 0.89)
        c.line(x, yy, x + w, yy)
        text(c, x - 21, yy - 3, f"{int(val * 100)}", 6)
    for i, label in enumerate(xlabels):
        xx = x + w * i / max(1, len(xlabels) - 1)
        text(c, xx - 10, y - 13, label, 6)


def line(c, points, color):
    c.setStrokeColorRGB(*color)
    c.setFillColorRGB(*color)
    c.setLineWidth(1.2)
    for p, q in zip(points, points[1:]):
        c.line(*p, *q)
    for x, y in points:
        c.circle(x, y, 2.2, stroke=0, fill=1)


def legend(c, y):
    for i, model in enumerate(MODELS):
        x = 22 + i * 112
        c.setFillColorRGB(*COLORS[i])
        c.circle(x, y, 2.3, stroke=0, fill=1)
        text(c, x + 6, y - 3, model.split("/")[1], 7)


def separation_figure(separation, figdir):
    import reportlab
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(TTFont("RevisionArial", str(Path(reportlab.__file__).parent / "fonts" / "Vera.ttf")))
    pdfmetrics.registerFont(TTFont("RevisionArialBold", str(Path(reportlab.__file__).parent / "fonts" / "VeraBd.ttf")))
    figdir.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(figdir / "canvas-separation.pdf"), pagesize=(470, 245))
    text(c, 22, 226, "Separation curves remain deployment- and task-dependent", 11, True)
    legend(c, 207)
    for j, task in enumerate(("read-known-value", "copy-known-value")):
        x, y, w, h = 40 + j * 235, 48, 175, 130
        axes(c, x, y, w, h, "Read" if j == 0 else "Copy", ["Bar", "Spaces", "40 px", "Line", "Corner"])
        for i, model in enumerate(MODELS):
            points = []
            for k, layout in enumerate(LAYOUTS[:5]):
                group = [
                    r for r in separation if r["model"] == model and r["task_id"] == task and r["layout"] == layout
                ]
                assert len(group) == 10
                rate = sum(r["exact_match"] for r in group) / len(group)
                points.append((x + k * w / 4, y + rate * h))
            line(c, points, COLORS[i])
    text(c, 22, 14, "Exact-match task success (%); 10 trials per deployment, task, and layout.", 8)
    c.save()


def figures(separation, generalization, intervention, boundary, figdir):
    from reportlab.pdfgen import canvas

    separation_figure(separation, figdir)
    c = canvas.Canvas(str(figdir / "generalization-intervention.pdf"), pagesize=(470, 365))
    text(c, 22, 347, "A  Delimitation error (%) across familiar layouts", 11, True)
    legend(c, 329)
    for j, layout in enumerate(("table", "cards", "form")):
        x, y, w, h = 38 + j * 154, 180, 105, 112
        axes(c, x, y, w, h, layout.title(), ["0", "60", "120"])
        for i, model in enumerate(MODELS):
            points = []
            for k, gap in enumerate((8, 40, 96)):
                group = [r for r in generalization if r["model"] == model and r["layout"] == layout and r["gap"] == gap]
                assert len(group) == 40
                rate = sum(r["delimitation_error"] for r in group) / len(group)
                points.append((x + gap * w / 120, y + rate * h))
            line(c, points, COLORS[i])
            group = [r for r in boundary if r["model"] == model and r["layout"] == layout]
            assert len(group) == 20
            gap = sum(r["geometry"]["pixel_distance"] for r in group) / len(group)
            rate = sum(r["delimitation_error"] for r in group) / len(group)
            xx, yy = x + gap * w / 120, y + rate * h
            c.setFillColorRGB(*COLORS[i])
            c.setStrokeColorRGB(1, 1, 1)
            diamond = c.beginPath()
            diamond.moveTo(xx, yy + 3.5)
            diamond.lineTo(xx + 3.5, yy)
            diamond.lineTo(xx, yy - 3.5)
            diamond.lineTo(xx - 3.5, yy)
            diamond.close()
            c.drawPath(diamond, stroke=1, fill=1)
    text(c, 22, 316, "Lines: controlled gaps. Diamonds: separate, labeled components.", 7)
    text(c, 105, 151, "Measured target-distractor gap (pixels)", 8)
    text(c, 22, 128, "B  Explicit format prompt: inline-canvas task success", 11, True)
    x0, y0, w = 150, 25, 285
    for val in (0, 0.5, 1):
        xx = x0 + w * val
        c.setStrokeColorRGB(0.82, 0.85, 0.89)
        c.line(xx, y0, xx, 111)
        text(c, xx - 8, y0 - 12, f"{val * 100:.0f}%", 7)
    for i, model in enumerate(MODELS):
        y = 102 - i * 22
        text(c, 22, y - 3, model.split("/")[1], 8)
        positions = []
        for arm in ("baseline", "format"):
            group = [r for r in intervention if r["model"] == model and r["intervention"] == arm]
            assert len(group) == 20
            k = sum(r["exact_match"] for r in group)
            positions.append((x0 + w * k / len(group), y))
        line(c, positions, COLORS[i])
        c.setFillColorRGB(1, 1, 1)
        c.setStrokeColorRGB(*COLORS[i])
        c.circle(*positions[0], 3, stroke=1, fill=1)
    text(c, 235, 119, "Open: baseline   Filled: format prompt", 7)
    c.save()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("artifacts/revision-20260915"))
    p.add_argument("--output", type=Path, default=Path("analysis/revision-20260915"))
    p.add_argument("--figures", type=Path, default=Path("output/overleaf/webagents1.a-revision/figures"))
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    studies = {}
    sources = {}
    for study, count in [
        ("separation-extension-final", 240),
        ("generalization-v2", 1440),
        ("intervention-v2", 160),
        ("iframe-v2", 560),
        ("boundary-cues", 240),
    ]:
        path = args.root / study / "results.jsonl"
        report = json.loads((path.parent / "report.json").read_text())
        assert report.get("complete", report.get("verdict") == "complete"), f"{study} not complete"
        rows = load(path)
        assert len(rows) == count, (study, len(rows))
        assert all(r.get("runner_status") != "infrastructure_error" for r in rows)
        ids = [r.get("id", r.get("logical_trial_id")) for r in rows]
        assert len(set(ids)) == count
        assert hashlib.sha256(path.read_bytes()).hexdigest() == report["results_sha256"]
        studies[study] = rows
        sources[study] = {"path": str(path), "sha256": report["results_sha256"], "trials": count}
    old = load(Path("artifacts/followups/canvas-delimitation-v8/results.jsonl"))
    separation = old + studies["separation-extension-final"]
    for r in separation:
        candidates = [
            args.root / "runs" / "runs" / r["run_id"] / "transcript.json",
            Path("artifacts/runs") / r["run_id"] / "transcript.json",
        ]
        transcript_path = next(path for path in candidates if path.exists())
        transcript = json.loads(transcript_path.read_text())
        typed = [
            a["parameters"].get("text", "")
            for step in transcript["steps"]
            for a in step["actions"]
            if a["name"] == "type"
        ]
        candidate = r["emitted_value"] if r["task_id"] == "read-known-value" else (typed[-1] if typed else "")
        r["candidate_value"] = candidate
        r["content_exact"] = candidate.strip() == r["expected_value"]
        r["delimitation_error"] = bool(
            candidate.strip()
            and not r["content_exact"]
            and (r["expected_value"] in candidate.strip() or candidate.strip() in r["expected_value"])
        )
        r["no_candidate"] = not candidate.strip()
    write_csv(
        args.output / "separation-content-stages.csv",
        aggregate(
            separation,
            ("model", "task_id", "layout"),
            ("exact_match", "content_exact", "delimitation_error", "no_candidate"),
        ),
    )
    general = studies["generalization-v2"]
    intervention = studies["intervention-v2"]
    iframe = studies["iframe-v2"]
    boundary = studies["boundary-cues"]
    trial_metrics = []
    for r in general:
        trial_metrics.append(
            {
                "trial_id": r["id"],
                "model": r["model"],
                "task_id": r["task_id"],
                "layout": r["layout"],
                "repeat": r["repeat"],
                "pixel_distance": r["geometry"]["pixel_distance"],
                "dom_element_separation": r["geometry"]["dom_element_separation"],
                "position_x": r["position"]["x"],
                "position_y": r["position"]["y"],
                "candidate_value": r["candidate_value"],
                "emitted_value": r["emitted_value"],
                "content_category": r["content_category"],
                "delimitation_error": r["delimitation_error"],
                "exact_match": r["exact_match"],
                "runner_status": r["runner_status"],
                "run_id": r["run_id"],
            }
        )
    write_csv(args.output / "generalization-trial-metrics.csv", trial_metrics)
    boundary_metrics = []
    for r in boundary:
        boundary_metrics.append(
            {
                "trial_id": r["id"],
                "model": r["model"],
                "task_id": r["task_id"],
                "layout": r["layout"],
                "repeat": r["repeat"],
                "pixel_distance": r["geometry"]["pixel_distance"],
                "dom_element_separation": r["geometry"]["dom_element_separation"],
                "delimitation_error": r["delimitation_error"],
                "exact_match": r["exact_match"],
                "content_category": r["content_category"],
                "run_id": r["run_id"],
            }
        )
    write_csv(args.output / "natural-boundary-trial-metrics.csv", boundary_metrics)
    write_csv(args.output / "natural-boundary-cells.csv", aggregate(boundary, ("model", "task_id", "layout")))
    image_audits = []
    for name, rows, keys in [
        ("generalization", general, ("task_id", "repeat", "layout", "gap")),
        ("iframe", iframe, ("task_id", "repeat")),
        ("boundary-cues", boundary, ("task_id", "repeat", "layout")),
    ]:
        groups = defaultdict(set)
        for row in rows:
            image_path = args.root / "runs" / "runs" / row["run_id"] / "observations" / "step-000" / "observation.png"
            assert image_path.exists(), image_path
            groups[tuple(row[k] for k in keys)].add(hashlib.sha256(image_path.read_bytes()).hexdigest())
        differing = [key for key, values in groups.items() if len(values) != 1]
        image_audits.append(
            {
                "study": name,
                "paired_groups": len(groups),
                "different_image_groups": len(differing),
                "different_group_keys": differing,
            }
        )
    (args.output / "image-pair-audit.json").write_text(json.dumps(image_audits, indent=2))
    assert all(a["different_image_groups"] == 0 for a in image_audits), image_audits
    cells = aggregate(separation, ("model", "task_id", "layout"), METRICS[:3])
    write_csv(args.output / "separation-cells.csv", cells)
    write_csv(args.output / "separation-pooled-tasks.csv", aggregate(separation, ("model", "layout"), METRICS[:3]))
    write_csv(
        args.output / "generalization-cells.csv", aggregate(general, ("model", "task_id", "layout", "gap", "boundary"))
    )
    write_csv(args.output / "generalization-distance.csv", aggregate(general, ("model", "layout", "gap")))
    write_csv(args.output / "generalization-dom.csv", aggregate(general, ("model", "boundary")))
    write_csv(args.output / "intervention-cells.csv", aggregate(intervention, ("model", "task_id", "intervention")))
    write_csv(args.output / "iframe-cells.csv", aggregate(iframe, ("model", "task_id", "origin", "depth")))
    effects = []
    for model in MODELS:
        group = [r for r in intervention if r["model"] == model]
        effects.append(
            dict(model=model, **paired_effect(group, "intervention", "baseline", "format", ("task_id", "repeat")))
        )
    write_csv(args.output / "intervention-paired-effects.csv", effects)
    dom_effects = []
    distance_effects = []
    iframe_effects = []
    for model in MODELS:
        group = [r for r in general if r["model"] == model]
        dom_effects.append(
            dict(
                model=model,
                **paired_effect(
                    group,
                    "boundary",
                    "shared",
                    "separate",
                    ("task_id", "repeat", "layout", "gap"),
                    "delimitation_error",
                ),
            )
        )
        distance_effects.append(
            dict(
                model=model,
                **paired_effect(group, "gap", 8, 96, ("task_id", "repeat", "layout", "boundary"), "delimitation_error"),
            )
        )
        for task in ("read-known-value", "copy-known-value"):
            base = [r for r in iframe if r["model"] == model and r["task_id"] == task and r["depth"] == 0]
            for origin in ("same", "cross"):
                for depth in (1, 2, 3):
                    group = base + [
                        r
                        for r in iframe
                        if r["model"] == model
                        and r["task_id"] == task
                        and r["origin"] == origin
                        and r["depth"] == depth
                    ]
                    iframe_effects.append(
                        dict(
                            model=model,
                            task_id=task,
                            origin=origin,
                            depth=depth,
                            **paired_effect(group, "depth", 0, depth, ("repeat",)),
                        )
                    )
    write_csv(args.output / "dom-paired-effects.csv", dom_effects)
    write_csv(args.output / "distance-paired-effects.csv", distance_effects)
    write_csv(args.output / "iframe-paired-effects.csv", iframe_effects)
    summary = dict(
        sources=sources,
        intervention_effects=effects,
        dom_effects=dom_effects,
        distance_effects=distance_effects,
        iframe_effects=iframe_effects,
    )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    lines = [
        "# Completed revision experiments",
        "",
        "Reference: webagents1.a.pdf. Original trials and new runs remain separate.",
        "All new cells have ten allocated valid trials; timeouts count as failures. Infrastructure attempts "
        "are excluded.",
        "",
        "## Separation extension",
        "",
        "| Layout | gpt-5.6-terra | Kimi-K2.6 | gpt-5.4 | gpt-5-mini |",
        "|---|---:|---:|---:|---:|",
    ]
    for layout in LAYOUTS:
        values = []
        for model in MODELS:
            group = [r for r in separation if r["model"] == model and r["layout"] == layout]
            values.append(f"{sum(r['exact_match'] for r in group)}/{len(group)}")
        lines.append("| " + layout + " | " + " | ".join(values) + " |")
    lines += [
        "",
        "The original two deployments were measured September 4; the additional two were measured in this revision. "
        "The fixture hash, task instructions, model-facing prompt and budgets match. This is a deployment "
        "comparison across run windows.",
        "",
        "## Generalization and DOM separation",
        "",
        "Three self-authored interface shells (table, cards, form) cross measured text-box edge gaps of 8/40/96 pixels "
        "with DOM graph distances of 0/4 edges. There are 1,440 trials. DOM variants are visually matched "
        "negative controls. "
        "The vision agent receives no DOM. These shells broaden layout coverage but remain controlled synthetic tasks.",
        "",
        "| Model | Exact success | Delimitation errors | No answer |",
        "|---|---:|---:|---:|",
    ]
    for model in MODELS:
        group = [r for r in general if r["model"] == model]
        lines.append(
            f"| {model.split('/')[1]} | {sum(r['exact_match'] for r in group)}/{len(group)} | "
            f"{sum(r['delimitation_error'] for r in group)}/{len(group)} | "
            f"{sum(r['content_category'] == 'no_answer' for r in group)}/{len(group)} |"
        )
    lines += [
        "",
        "Delimitation is measured from the final read answer or last attempted typed content. Correct content "
        "with a failed "
        "submission is kept separate from delimitation. No-answer outcomes are reported separately; a lower "
        "delimitation rate "
        "alone is not evidence of improved task success. Per-cell CSVs retain task, deployment, layout, gap, "
        "and DOM boundary.",
        "",
        "| Model | Error rate: 8px | Error rate: 96px | Change (pp), paired 95% interval | "
        "Hidden DOM change (pp), paired 95% interval |",
        "|---|---:|---:|---|---|",
    ]
    for distance, dom in zip(distance_effects, dom_effects):
        lines.append(
            f"| {distance['model'].split('/')[1]} | {distance['baseline']}/{distance['pairs']} | "
            f"{distance['treatment']}/{distance['pairs']} | {100 * distance['difference']:+.1f} "
            f"[{100 * distance['bootstrap_low']:+.1f}, {100 * distance['bootstrap_high']:+.1f}] | "
            f"{100 * dom['difference']:+.1f} "
            f"[{100 * dom['bootstrap_low']:+.1f}, {100 * dom['bootstrap_high']:+.1f}] |"
        )
    lines += [
        "",
        "## Paired format intervention",
        "",
        "The baseline and explicit-format arms use the same inline fixture, 768x768 viewport, "
        "eight-action/90-second budget, "
        "and paired trials. The format prompt describes the token shape and excludes adjacent metadata without "
        "supplying the answer. "
        "The inline canvas is 730px wide to fit the calibrated viewport; the contemporaneous baseline is the "
        "comparator.",
        "",
        "| Model | Baseline | Format | Change (pp) | Paired bootstrap 95% interval |",
        "|---|---:|---:|---:|---|",
    ]
    for e in effects:
        lines.append(
            f"| {e['model'].split('/')[1]} | {e['baseline']}/{e['pairs']} | {e['treatment']}/{e['pairs']} | "
            f"{100 * e['difference']:+.1f} | {100 * e['bootstrap_low']:+.1f} to {100 * e['bootstrap_high']:+.1f} |"
        )
    lines += [
        "",
        "## Calibrated, randomized iframe rerun",
        "",
        "560 trials: four deployments, two tasks, seven unique conditions, ten repeats. Depth zero is shared "
        "across origins. "
        "Positions are randomized by task/repeat and paired across deployments, origins and depths. Each "
        "screenshot is 768x768 "
        "at device scale 1; the prompt states the coordinate dimensions. Privileged pixel-center click/submit "
        "checks pass for every "
        "unique fixture. Preflight asserts byte-identical screenshots across depths and origins at each paired "
        "position.",
        "",
        "| Model | Read success | Copy success |",
        "|---|---:|---:|",
    ]
    for model in MODELS:
        values = []
        for task in ("read-known-value", "copy-known-value"):
            group = [r for r in iframe if r["model"] == model and r["task_id"] == task]
            values.append(f"{sum(r['exact_match'] for r in group)}/{len(group)}")
        lines.append(f"| {model.split('/')[1]} | {' | '.join(values)} |")
    lines += [
        "",
        "Depth comparisons are paired against the shared flat cell; complete estimates and intervals are in "
        "iframe-paired-effects.csv. "
        "A nonsignificant difference is not an equivalence result. Ten paired repeats per task/model limit "
        "sensitivity to small effects. "
        "This removes the deterministic fixture scale/position confound; it does not guarantee that models "
        "localize every field correctly.",
        "",
        "## Statistical and provenance notes",
        "",
        "- Cell rates use Wilson 95% intervals. Paired differences resample complete randomized-position "
        "repeat blocks within task strata 10,000 times with a fixed seed.",
        "- A paired bootstrap interval can be degenerate when all observed pairs agree. This reflects "
        "resampling the observed sample, not certainty about future trials; use the cell Wilson intervals as well.",
        "- Paired tests are exploratory and are not adjusted for multiple comparisons; do not use isolated "
        "p-values as confirmatory evidence.",
        "- Generalization snapshots use the original scorer selector, which does not identify the new `pair` element; "
        "availability is established independently from saved text-range boxes and viewport checks, not those "
        "legacy reachability flags.",
        "- The old variance decomposition is retained in the appendix as a confounded descriptive audit. "
        "The new vision-only runs do not support a balanced architecture-by-model variance decomposition.",
        "- Trial archives retain exact screenshots, prompts, responses, actions, timings and token counts. The "
        "new scripts and results are hash-bound.",
        "",
    ]
    lines += [
        "",
        "## Visible natural-boundary controls",
        "",
        "An additional 240 trials place Source and Reference in separate table columns, neighboring cards, or "
        "labeled form fields. "
        "The same tokens, task/repeat positions, model roster and ten-repeat allocation are retained. "
        "Measured pixel and DOM distances are saved per trial. This is a compound layout/label/boundary control, "
        "not an isolated causal effect of DOM distance.",
        "",
        "| Model | Exact success | Delimitation error |",
        "|---|---:|---:|",
    ]
    for model in MODELS:
        group = [r for r in boundary if r["model"] == model]
        lines.append(
            f"| {model.split('/')[1]} | {sum(r['exact_match'] for r in group)}/{len(group)} | "
            f"{sum(r['delimitation_error'] for r in group)}/{len(group)} |"
        )
    (args.output / "report.md").write_text("\n".join(lines))
    figures(separation, general, intervention, boundary, args.figures)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
