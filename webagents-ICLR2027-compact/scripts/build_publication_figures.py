#!/usr/bin/env python3
"""Build restrained, publication-style vector figures for the workshop paper."""

from __future__ import annotations

import csv
import math
import reportlab
from pathlib import Path

from reportlab.lib.colors import Color, HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "analysis/core-grid-v1"
VISION_EXTENSION = (
    ROOT / "analysis/reviewer-response-v1/vision-model-extension/combined-cells.csv"
)
OUTPUT = ROOT / "output/overleaf/webagents1.7/figures"

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
ARCH_SHORT = {
    "dom_extraction": "DOM",
    "accessibility_tree": "Accessibility",
    "vision": "Vision",
    "cdp_frame_traversal": "CDP",
}
PALETTE_DARK_SLATE = HexColor("#495362")
PALETTE_DARK_SAGE = HexColor("#4B6D60")
PALETTE_MID_SLATE = HexColor("#8997A2")
PALETTE_LIGHT_SAGE = HexColor("#88A5A2")
PALETTE_GRID = HexColor("#D1DFE6")

ARCH_COLORS = {
    "dom_extraction": PALETTE_DARK_SLATE,
    "accessibility_tree": PALETTE_DARK_SAGE,
    "vision": PALETTE_LIGHT_SAGE,
    "cdp_frame_traversal": PALETTE_MID_SLATE,
}

TEXT = HexColor("#222222")
MUTED = HexColor("#666666")
GRID = PALETTE_GRID
REACH = PALETTE_MID_SLATE
SUCCESS = PALETTE_DARK_SLATE
MODEL_LABELS = {
    "foundry-gpt-5-6-terra": "gpt-5.6-terra",
    "foundry-kimi-k2-6": "Kimi-K2.6",
    "foundry-gpt-5-4": "gpt-5.4",
    "foundry-gpt-5-mini": "gpt-5-mini",
}
MODEL_COLORS = {
    "foundry-gpt-5-6-terra": PALETTE_DARK_SLATE,
    "foundry-kimi-k2-6": PALETTE_DARK_SAGE,
    "foundry-gpt-5-4": PALETTE_LIGHT_SAGE,
    "foundry-gpt-5-mini": PALETTE_MID_SLATE,
}
VARIANCE_COLORS = {
    "architecture": PALETTE_DARK_SLATE,
    "model": PALETTE_MID_SLATE,
    "interaction": PALETTE_DARK_SAGE,
}

FONT_REGULAR = str(Path(reportlab.__file__).parent / "fonts" / "Vera.ttf")
FONT_BOLD = str(Path(reportlab.__file__).parent / "fonts" / "VeraBd.ttf")
SANS = "FigureSans"
SANS_BOLD = "FigureSans-Bold"
pdfmetrics.registerFont(TTFont(SANS, FONT_REGULAR))
pdfmetrics.registerFont(TTFont(SANS_BOLD, FONT_BOLD))


def read_csv(name: str) -> list[dict[str, str]]:
    with (ANALYSIS / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def text(c: canvas.Canvas, x: float, y: float, value: str, size: float = 8, *,
         bold: bool = False, color: Color = TEXT, align: str = "left") -> None:
    c.setFont(SANS_BOLD if bold else SANS, size)
    c.setFillColor(color)
    if align == "center":
        c.drawCentredString(x, y, value)
    elif align == "right":
        c.drawRightString(x, y, value)
    else:
        c.drawString(x, y, value)


def unique_by_arch_condition(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    selected: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        selected.setdefault((row["architecture"], row["condition_id"]), row)
    return selected


def rate_x(rate: float, left: float, right: float) -> float:
    """Map a rate to a horizontal axis while keeping endpoint marks unclipped."""
    return left + 5 + rate * (right - left - 10)


def horizontal_rate_axis(c: canvas.Canvas, left: float, right: float,
                         bottom: float, top: float, tick_y: float) -> None:
    for value in (0, 0.25, 0.5, 0.75, 1.0):
        x = rate_x(value, left, right)
        c.setStrokeColor(GRID)
        c.setLineWidth(0.45)
        c.line(x, bottom, x, top)
        label = "100%" if value == 1 else f"{value * 100:.0f}"
        text(c, x, tick_y, label, 6.4, color=MUTED, align="center")


def circle_marker(c: canvas.Canvas, x: float, y: float, color: Color, *, open_marker: bool = False) -> None:
    c.setStrokeColor(color)
    c.setFillColor(Color(1, 1, 1) if open_marker else color)
    c.setLineWidth(1.4)
    c.circle(x, y, 3.2, fill=1, stroke=1)


def square_marker(c: canvas.Canvas, x: float, y: float, color: Color) -> None:
    c.setFillColor(color)
    c.setStrokeColor(Color(1, 1, 1))
    c.setLineWidth(0.6)
    c.rect(x - 3.1, y - 3.1, 6.2, 6.2, fill=1, stroke=1)


def diamond_marker(c: canvas.Canvas, x: float, y: float, color: Color) -> None:
    c.setFillColor(color)
    c.setStrokeColor(Color(1, 1, 1))
    c.setLineWidth(0.6)
    c.saveState()
    c.translate(x, y)
    c.rotate(45)
    c.rect(-2.7, -2.7, 5.4, 5.4, fill=1, stroke=1)
    c.restoreState()


def draw_horizontal_interval(c: canvas.Canvas, low: float, high: float, y: float,
                             left: float, right: float, color: Color) -> None:
    x_low = rate_x(low, left, right)
    x_high = rate_x(high, left, right)
    c.setStrokeColor(color)
    c.setLineWidth(0.8)
    c.line(x_low, y, x_high, y)
    c.line(x_low, y - 2.0, x_low, y + 2.0)
    c.line(x_high, y - 2.0, x_high, y + 2.0)


def wilson(successes: int, trials: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Return a two-sided Wilson interval for a binomial proportion."""
    rate = successes / trials
    denominator = 1 + z * z / trials
    center = (rate + z * z / (2 * trials)) / denominator
    half_width = (
        z
        * math.sqrt(rate * (1 - rate) / trials + z * z / (4 * trials * trials))
        / denominator
    )
    return center - half_width, center + half_width


def build_outcomes() -> None:
    rows = read_csv("headline-curves.csv")
    with VISION_EXTENSION.open(newline="", encoding="utf-8") as handle:
        combined_vision_cells = list(csv.DictReader(handle))
    unique = unique_by_arch_condition(rows)
    width, height = 397, 302
    c = canvas.Canvas(str(OUTPUT / "outcomes.pdf"), pagesize=(width, height))
    c.setTitle("Reachability and task success at the key structural boundaries")

    left, right = 105, 391

    # One visual grammar throughout: horizontal estimates on a common 0--100% scale.
    circle_marker(c, 119, 290, REACH, open_marker=True)
    text(c, 128, 287.5, "Text reached / region visible", 6.2, color=MUTED)
    square_marker(c, 226, 290, SUCCESS)
    text(c, 235, 287.5, "Task completed", 6.7, color=MUTED)

    # Panel A: canvas separates screenshot evidence from structural evidence.
    text(c, 2, 270, "A", 9, bold=True)
    text(c, 15, 270, "Canvas target", 9, bold=True)
    horizontal_rate_axis(c, left, right, 173, 250, 255)
    row_y = (238, 218, 198, 178)
    for architecture, y in zip(ARCHITECTURES, row_y):
        row = unique[(architecture, "rendered-canvas")]
        reachable = float(row["reachable_rate"])
        success = float(row["task_success_rate"])
        x_reach = rate_x(reachable, left, right)
        x_success = rate_x(success, left, right)
        c.setStrokeColor(HexColor("#B8B8B8"))
        c.setLineWidth(1.0)
        c.line(min(x_reach, x_success), y, max(x_reach, x_success), y)
        draw_horizontal_interval(c, float(row["reachable_ci_lower"]),
                                 float(row["reachable_ci_upper"]), y + 2.6,
                                 left, right, REACH)
        draw_horizontal_interval(c, float(row["task_success_ci_lower"]),
                                 float(row["task_success_ci_upper"]), y - 2.6,
                                 left, right, SUCCESS)
        circle_marker(c, x_reach, y + 2.6, REACH, open_marker=True)
        square_marker(c, x_success, y - 2.6, SUCCESS)
        text(c, left - 9, y - 2.3, ARCH_LABELS[architecture], 7.0,
             align="right")

    # Panel B combines the primary and extension deployments on the matched
    # original-layout vision-canvas cells while pooling tasks (N=20 per model).
    text(c, 2, 157, "B", 9, bold=True)
    text(c, 15, 157, "Original-layout vision canvas by model", 9, bold=True)
    text(c, right, 145, "Tasks pooled; N=20 per deployment", 6.1,
         color=MUTED, align="right")
    horizontal_rate_axis(c, left, right, 25, 130, 12)

    def pooled_canvas_rate(model_id: str) -> tuple[float, float, float]:
        selected = [
            row for row in combined_vision_cells
            if row["model_id"] == model_id and row["condition_id"] == "rendered-canvas"
        ]
        trials = sum(int(row["trials"]) for row in selected)
        successes = sum(int(row["successes"]) for row in selected)
        low, high = wilson(successes, trials)
        return successes / trials, low, high

    model_rows = (
        ("foundry-gpt-5-6-terra", 119),
        ("foundry-kimi-k2-6", 96),
        ("foundry-gpt-5-4", 65),
        ("foundry-gpt-5-mini", 42),
    )
    for model_id, y in model_rows:
        color = MODEL_COLORS[model_id]
        success, low, high = pooled_canvas_rate(model_id)
        draw_horizontal_interval(c, low, high, y, left, right, color)
        square_marker(c, rate_x(success, left, right), y, color)
        text(c, left - 9, y - 2.3, MODEL_LABELS[model_id], 7.0,
             bold=True, color=color, align="right")
        count_x = rate_x(success, left, right) + 8
        text(c, count_x, y + 5.0, f"{int(round(success * 20))}/20", 6.2,
             color=color)

    c.setStrokeColor(GRID)
    c.setLineWidth(0.7)
    c.line(15, 80, right, 80)
    text(c, 15, 84, "Primary grid", 6.0, color=MUTED)
    text(c, 15, 72, "Vision-only extension", 6.0, color=MUTED)

    c.showPage()
    c.save()


def build_variance() -> None:
    rows = [row for row in read_csv("variance-decomposition.csv") if row["axis_scope"] == "all"]
    lookup = {(row["outcome"], row["effect"]): row for row in rows}
    width, height = 397, 112
    c = canvas.Canvas(str(OUTPUT / "variance.pdf"), pagesize=(width, height))
    c.setTitle("Variance decomposition")

    left, right = 105, 390
    circle_marker(c, 132, 101, REACH, open_marker=True)
    text(c, 141, 98.5, "Reachability", 6.7, color=MUTED)
    square_marker(c, 242, 101, SUCCESS)
    text(c, 251, 98.5, "Task success", 6.7, color=MUTED)
    horizontal_rate_axis(c, left, right, 17, 87, 3)

    effect_labels = {
        "architecture": "Architecture",
        "model": "Model",
        "interaction": "Architecture x model",
    }
    for effect, y in zip(("architecture", "model", "interaction"), (77, 52, 27)):
        reachable = lookup[("reachable", effect)]
        success = lookup[("task_success", effect)]
        reach_share = float(reachable["variance_share"])
        success_share = float(success["variance_share"])
        x_reach = rate_x(reach_share, left, right)
        x_success = rate_x(success_share, left, right)
        c.setStrokeColor(HexColor("#B8B8B8"))
        c.setLineWidth(1.0)
        c.line(min(x_reach, x_success), y, max(x_reach, x_success), y)
        draw_horizontal_interval(c, float(reachable["ci_lower"]),
                                 float(reachable["ci_upper"]), y + 2.6,
                                 left, right, REACH)
        draw_horizontal_interval(c, float(success["ci_lower"]),
                                 float(success["ci_upper"]), y - 2.6,
                                 left, right, SUCCESS)
        circle_marker(c, x_reach, y + 2.6, REACH, open_marker=True)
        square_marker(c, x_success, y - 2.6, SUCCESS)
        text(c, left - 9, y - 2.3, effect_labels[effect], 7.0, align="right")
    c.showPage()
    c.save()


def build_cost() -> None:
    cost_rows = read_csv("cost-by-level.csv")
    width, height = 397, 180
    c = canvas.Canvas(str(OUTPUT / "cost.pdf"), pagesize=(width, height))
    c.setTitle("Tokens per action across iframe depth")

    values: dict[tuple[str, int], float] = {}
    for architecture in ARCHITECTURES:
        for depth in range(4):
            selected = [
                row for row in cost_rows
                if row["architecture"] == architecture
                and row["axis_id"] in ("iframe-same-origin-depth", "iframe-cross-origin-depth")
                and int(row["level_order"]) == depth
            ]
            total_trials = sum(float(row["trials"]) for row in selected)
            values[(architecture, depth)] = sum(
                float(row["trials"]) * float(row["mean_tokens_per_action"])
                for row in selected
            ) / total_trials

    left, right = 45, 312
    bottom, top = 30, 164
    minimum, maximum = 1_000, 11_500
    for tick in (2_000, 4_000, 6_000, 8_000, 10_000):
        y = bottom + (tick - minimum) / (maximum - minimum) * (top - bottom)
        c.setStrokeColor(GRID)
        c.setLineWidth(0.45)
        c.line(left, y, right, y)
        text(c, left - 6, y - 2.2, f"{tick / 1000:.0f}k", 6.3,
             color=MUTED, align="right")

    xs = [left + depth * (right - left) / 3 for depth in range(4)]
    for depth, x in enumerate(xs):
        c.setStrokeColor(GRID)
        c.setLineWidth(0.45)
        c.line(x, bottom, x, top)
        text(c, x, 17, str(depth), 6.5, color=MUTED, align="center")

    label_offsets = {
        "dom_extraction": 2.0,
        "accessibility_tree": -1.5,
        "vision": 1.0,
        "cdp_frame_traversal": -1.0,
    }
    for architecture in ARCHITECTURES:
        points: list[tuple[float, float]] = []
        for depth, x in enumerate(xs):
            value = values[(architecture, depth)]
            y = bottom + (value - minimum) / (maximum - minimum) * (top - bottom)
            points.append((x, y))
        color = ARCH_COLORS[architecture]
        c.setStrokeColor(color)
        c.setLineWidth(1.6)
        for first, second in zip(points, points[1:]):
            c.line(first[0], first[1], second[0], second[1])
        for x, y in points:
            circle_marker(c, x, y, color)
        final_y = points[-1][1] + label_offsets[architecture]
        text(c, right + 9, final_y - 2.2,
             f"{ARCH_SHORT[architecture]}  {values[(architecture, 3)] / 1000:.1f}k", 6.7,
             bold=True, color=color)

    text(c, (left + right) / 2, 5, "Iframe depth", 7.0, color=MUTED, align="center")
    c.saveState()
    c.translate(9, (bottom + top) / 2)
    c.rotate(90)
    text(c, 0, 0, "Input + output tokens per action", 6.8, color=MUTED,
         align="center")
    c.restoreState()
    text(c, left, 171, "Same- and cross-origin conditions pooled", 6.5, color=MUTED)

    c.showPage()
    c.save()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    build_outcomes()
    build_variance()
    build_cost()
    print(f"Wrote publication figures to {OUTPUT}")


if __name__ == "__main__":
    main()
