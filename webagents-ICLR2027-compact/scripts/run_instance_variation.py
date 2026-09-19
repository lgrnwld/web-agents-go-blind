#!/usr/bin/env python3
"""Paired instance variation, frozen before model calls; no generic-prompt control."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import string
from pathlib import Path

from scripts.run_canvas_delimitation_followup import TASKS, score_value, slug, utc_now
from scripts.run_revision_experiments import (
    CALIBRATION, FORMAT, GEOMETRY_JS, MODELS, VIEWPORT, Harness, digest, leaf_page,
)
from webagents.capture.archive import canonical_json_bytes
from webagents.io import append_jsonl, atomic_write
from webagents.vision import observer, runner


def matrix(models, repeats, seed):
    rows = []
    conditions = [
        dict(layout=layout, gap=gap, boundary=boundary, intervention="baseline")
        for layout in ("table", "cards", "form")
        for gap, boundary in ((8, "shared"), (96, "shared"), (None, "visible"))
    ] + [
        dict(layout="canvas-inline-bar", gap=None, boundary="inline", intervention=arm)
        for arm in ("baseline", "format")
    ]
    for task_id in TASKS:
        for repeat in range(1, repeats + 1):
            rng = random.Random(f"instance-v1:{seed}:{task_id}:{repeat}")
            value = "".join(rng.choices(string.ascii_uppercase, k=5)) + "-" + "".join(rng.choices(string.digits, k=4))
            marker = "REF_" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=12))
            instance = dict(
                value=value, marker=marker,
                position={"x": rng.randint(18, 65), "y": rng.randint(60, 130)},
                font_family=rng.choice(["Arial", "Verdana", "Georgia"]),
                font_size=rng.choice([15, 16, 17]),
                line_height=rng.choice([1.2, 1.35, 1.5]),
            )
            instance_id = digest({"seed": seed, "task_id": task_id, "repeat": repeat, **instance})[:20]
            for model in models:
                for condition in conditions:
                    row = dict(study="instance-variation", model=model, task_id=task_id, repeat=repeat,
                               instance_id=instance_id, **instance, **condition)
                    row["id"] = digest({"seed": seed, **row})[:24]
                    rows.append(row)
    random.Random(seed).shuffle(rows)
    assert len({r["id"] for r in rows}) == len(rows)
    return rows


def render(row, path):
    if row["layout"] != "canvas-inline-bar":
        # Keep the established layout structure; substitute only instance features.
        page = leaf_page(row, path)
        task = TASKS[row["task_id"]]
        page = page.replace(task["value"], row["value"]).replace(task["marker"], row["marker"])
        css = (f"#pair{{font:{row['font_size']}px {row['font_family']};line-height:{row['line_height']}}}"
               f"td span,article span,.readonly span{{font:{row['font_size']}px {row['font_family']}}}")
        return page.replace("</style>", css + "</style>")
    x, y = row["position"]["x"], row["position"]["y"]
    form = (f"<form method='post' action='{path}/submit'><label for='destination'>Destination</label>"
            "<input id='destination' name='destination' autocomplete='off'><button>Submit</button></form>"
            if row["task_id"] == "copy-known-value" else "")
    font = f"{row['font_size'] + 4}px {row['font_family']}"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Record workspace</title><style>"
        "*{box-sizing:border-box}html,body{margin:0;background:#f5f7fa;font:16px Arial;color:#182738}"
        f"main{{position:absolute;left:{x}px;top:{y}px;width:650px}}"
        "canvas{display:block;background:white;border:1px solid #ccd4df}"
        "form{margin-top:18px;width:360px}label{display:block;font-weight:bold}"
        "input{width:360px;height:44px;font-size:18px;margin:8px 0}button{display:block;padding:12px 24px}"
        "</style></head><body><main><h2>Record details</h2>"
        "<canvas id='target-canvas' width='650' height='180' aria-label='Rendered target'></canvas>"
        + form + "</main><script>const c=document.getElementById('target-canvas'),x=c.getContext('2d');"
        f"x.font={json.dumps(font)};x.fillStyle='#182738';x.textBaseline='alphabetic';"
        f"const target={json.dumps(row['value'])},marker={json.dumps(row['marker'])};"
        "const label='Source: ',sep=' | ';const tx=16+x.measureText(label).width;"
        "const mx=tx+x.measureText(target+sep).width;x.fillText(label+target+sep+marker,16,65);"
        "function box(s,left){const m=x.measureText(s);return {x:left,y:65-m.actualBoundingBoxAscent,"
        "width:m.width,height:m.actualBoundingBoxAscent+m.actualBoundingBoxDescent}}"
        "window.fixtureGeometry={target_box:box(target,tx),distractor_box:box(marker,mx)};"
        "</script></body></html>"
    )


def content_score(transcript, row, submitted):
    typed = [a.parameters.get("text", "") for s in transcript.steps for a in s.actions if a.name == "type"]
    candidate = ((transcript.final.answer if transcript.final else "") or ""
                 if row["task_id"] == "read-known-value" else (typed[-1] if typed else ""))
    emitted = candidate if row["task_id"] == "read-known-value" else (submitted or "")
    c, value = candidate.strip(), row["value"]
    category = ("correct_content" if c == value else "delimitation" if c and (value in c or c in value)
                else "no_answer" if not c else "substitution_or_other")
    return dict(**score_value(emitted, value, row["marker"]), candidate_value=candidate, emitted_value=emitted,
                content_category=category, delimitation_error=category == "delimitation", content_exact=c == value,
                target_token_in_candidate=value in c, marker_in_candidate=row["marker"] in c,
                no_answer=not c)


async def preflight(harness, rows, out):
    from playwright.async_api import async_playwright

    geometry, cache = {}, {}
    previews = out / "fixture-previews"
    previews.mkdir(exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--site-per-process"])
        page = await browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        for row in rows:
            key = digest({k: v for k, v in row.items() if k not in ("model", "id", "intervention")})
            if key in cache:
                geometry[row["id"]] = cache[key]
                continue
            await page.goto(harness.url(row), wait_until="load")
            await page.evaluate("document.fonts.ready")
            if row["layout"] == "canvas-inline-bar":
                measured = await page.evaluate("""() => {
                    const b=document.querySelector('canvas').getBoundingClientRect();
                    const g=structuredClone(window.fixtureGeometry);
                    for(const v of Object.values(g)){v.x+=b.x+1;v.y+=b.y+1;}
                    g.pixel_distance=g.distractor_box.x-g.target_box.x-g.target_box.width;
                    g.dom_element_separation=null;return g;
                }""")
            else:
                measured = await page.evaluate(GEOMETRY_JS, {"target": row["value"], "marker": row["marker"]})
                assert measured is not None, row
                if row["boundary"] == "shared":
                    assert abs(measured["pixel_distance"] - row["gap"]) < 0.1, (row, measured)
                    assert measured["dom_element_separation"] == 0
            for name in ("target_box", "distractor_box"):
                b = measured[name]
                assert 0 <= b["x"] < b["x"] + b["width"] <= 768, (row, b)
                assert 0 <= b["y"] < b["y"] + b["height"] <= 768, (row, b)
            measured["target_region_visible"] = True
            measured["text_legibility_verified"] = False
            await page.mouse.move(0, 0)
            png = await page.screenshot(animations="disabled")
            measured["screenshot_sha256"] = hashlib.sha256(png).hexdigest()
            preview = previews / f"{row['instance_id']}-{row['layout']}-{row['boundary']}-{row['gap']}.png"
            preview.write_bytes(png)
            if row["task_id"] == "copy-known-value":
                b = await page.locator("#destination").bounding_box()
                assert b and 0 <= b["y"] < b["y"] + b["height"] <= 768, (row, b)
                measured["destination_box"] = b
                await page.mouse.click(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
                await page.keyboard.insert_text(row["value"])
                await page.keyboard.press("Enter")
                for _ in range(60):
                    if harness.submissions.get(row["id"]) == row["value"]:
                        break
                    await asyncio.sleep(0.05)
                assert harness.submissions.pop(row["id"], None) == row["value"], (row, "calibration")
            cache[key] = measured
            geometry[row["id"]] = measured
        await browser.close()
    if (out / "geometry.json").exists():
        assert json.loads((out / "geometry.json").read_text()) == geometry, "Fixture changed since preflight"
    else:
        atomic_write(out / "geometry.json", canonical_json_bytes(geometry))
    return geometry


async def execute(args):
    out = args.output_dir
    rows = matrix(args.models, args.repeats, args.seed)
    source = Path(__file__).read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    if args.resume:
        spec = json.loads((out / "resolved-spec.json").read_text())
        assert json.loads((out / "matrix.json").read_text()) == rows
        assert spec["source_sha256"] == source_hash, "Runner changed after allocation"
    else:
        out.mkdir(parents=True, exist_ok=False)
        spec = dict(study="instance-variation-v1", created_at=utc_now(), planned_trials=len(rows),
                    models=args.models, repeats=args.repeats, seed=args.seed, viewport=VIEWPORT,
                    max_steps=8, timeout_seconds=90, source_sha256=source_hash,
                    source_paper_sha256=hashlib.sha256(Path(args.paper).read_bytes()).hexdigest(),
                    matrix_sha256=digest(rows), coordinate_prompt=CALIBRATION, format_prompt=FORMAT,
                    randomization="Fresh task/repeat instances paired across every model and condition; shuffled order",
                    scoring="Timeout and model failures count; only infrastructure failures excluded and retried",
                    endpoints=["delimitation_error", "content_exact", "exact_match", "no_answer"],
                    contrasts=["8 versus 96 pixel gap", "8 pixel versus labeled components", "inline baseline versus format"],
                    uncertainty="Wilson cell intervals; paired bootstrap over task-stratified instance blocks",
                    generic_instruction_control=False)
        atomic_write(out / "resolved-spec.json", canonical_json_bytes(spec))
        atomic_write(out / "matrix.json", canonical_json_bytes(rows))
        atomic_write(out / "runner-source.py", source)
        dependencies = [Path("scripts/run_revision_experiments.py"),
                        Path("scripts/run_canvas_delimitation_followup.py")]
        for dep in dependencies:
            atomic_write(out / dep.name, dep.read_bytes())
    results_path = out / "results.jsonl"
    previous = [json.loads(s) for s in results_path.read_text().splitlines() if s] if results_path.exists() else []
    done = {r["id"] for r in previous if r["valid"]}
    observer.VIEWPORT.clear()
    observer.VIEWPORT.update(VIEWPORT)
    with Harness(rows, renderer=render) as harness:
        geometry = await preflight(harness, rows, out)
        print(json.dumps({"preflight": "passed", "planned": len(rows), "already_valid": len(done)}), flush=True)
        if args.preflight_only:
            return
        semaphore = asyncio.Semaphore(args.concurrency)

        async def run(row):
            async with semaphore:
                attempts = []
                prior_count = sum(len(r["attempts"]) for r in previous if r["id"] == row["id"])
                for attempt in range(prior_count, prior_count + 3):
                    harness.submissions.pop(row["id"], None)
                    run_id = f"INSTANCE-{slug(out.name)}-{row['id']}-A{attempt}"
                    prompt = TASKS[row["task_id"]]["prompt"] + CALIBRATION
                    if row["intervention"] == "format":
                        prompt += FORMAT
                    transcript = await runner.run_vision_agent(harness.url(row), prompt, row["model"],
                        run_id=run_id, max_steps=8, timeout_seconds=90, archive_root=args.archive_root)
                    attempts.append(dict(run_id=run_id, status=transcript.status,
                                         error=transcript.final.error if transcript.final else None))
                    if transcript.status != "infrastructure_error":
                        break
                    await asyncio.sleep(2 * (attempt - prior_count + 1))
                result = dict(**row, **content_score(transcript, row, harness.submissions.get(row["id"])),
                    geometry=geometry[row["id"]], attempts=attempts, run_id=run_id, runner_status=transcript.status,
                    valid=transcript.status != "infrastructure_error", started_at=transcript.started_at.isoformat(),
                    finished_at=utc_now(), input_tokens=sum(s.usage.input_tokens or 0 for s in transcript.steps if s.usage),
                    output_tokens=sum(s.usage.output_tokens or 0 for s in transcript.steps if s.usage))
                append_jsonl(results_path, result)
                return result

        completed = len(done)
        pending = [r for r in rows if r["id"] not in done]
        for future in asyncio.as_completed([asyncio.create_task(run(r)) for r in pending]):
            result = await future
            completed += 1
            print(json.dumps(dict(completed=completed, planned=len(rows), model=result["model"],
                                  status=result["runner_status"], valid=result["valid"])), flush=True)
    latest = {r["id"]: r for r in map(json.loads, results_path.read_text().splitlines())}
    valid = sum(r["valid"] for r in latest.values())
    atomic_write(out / "report.json", canonical_json_bytes(dict(planned=len(rows), valid=valid,
                 complete=valid == len(rows), results_sha256=hashlib.sha256(results_path.read_bytes()).hexdigest())),
                 replace=True)
    if valid != len(rows):
        raise SystemExit("Infrastructure failures remain; resume without changing allocation")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="+", default=list(MODELS))
    p.add_argument("--repeats", type=int, default=10)
    p.add_argument("--seed", type=int, default=20260919)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--output-dir", type=Path, default=Path("artifacts/instance-variation-20260919-final"))
    p.add_argument("--archive-root", type=Path, default=Path("artifacts/instance-variation-20260919-final/runs"))
    p.add_argument("--paper", type=Path, default=Path("paper/reference/coolwebagentsiclr.pdf"))
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()
    if args.repeats < 1 or args.concurrency < 1:
        p.error("repeats and concurrency must be positive")
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()
