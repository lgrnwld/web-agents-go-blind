#!/usr/bin/env python3
"""Versioned, randomized separation/generalization and position-matched iframe study.

No changes to the frozen runners or original fixtures. Run preflight before paid
trials. The privileged geometry probe is saved for scoring, never sent to models.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from scripts.run_canvas_delimitation_followup import TASKS, render_page, score_value, slug, utc_now
from webagents.capture.archive import canonical_json_bytes
from webagents.io import append_jsonl, atomic_write
from webagents.vision import observer, runner

MODELS = ("foundry/gpt-5.6-terra", "foundry/Kimi-K2.6", "foundry/gpt-5.4", "foundry/gpt-5-mini")
VIEWPORT = {"width": 768, "height": 768}
FORMAT = (
    " Treat the adjacent reference/control marker as a separate value. "
    "The Source value has the format five uppercase letters, a hyphen, and four digits. "
    "Return or enter only that value, without the label, separator, marker, or explanation."
)
CALIBRATION = " The screenshot is exactly 768 pixels wide and 768 pixels tall. Use those pixel coordinates for actions."


def digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def matrix(study, models, repeats, seed):
    rows = []
    if study == "generalization":
        conditions = [
            dict(layout=layout, gap=gap, boundary=boundary, intervention="baseline")
            for layout in ("table", "cards", "form")
            for gap in (8, 40, 96)
            for boundary in ("shared", "separate")
        ]
    elif study == "boundary-cues":
        conditions = [
            dict(layout=layout, gap=None, boundary="visible", intervention="baseline")
            for layout in ("table", "cards", "form")
        ]
    elif study == "intervention":
        conditions = [dict(layout="canvas-inline-bar", intervention=arm) for arm in ("baseline", "format")]
    else:
        conditions = [dict(layout="iframe", depth=0, origin="same", intervention="baseline")]
        conditions += [
            dict(layout="iframe", depth=d, origin=o, intervention="baseline")
            for d in (1, 2, 3)
            for o in ("same", "cross")
        ]
    for repeat in range(1, repeats + 1):
        for task_id in TASKS:
            # Pair position across model, origin, depth, layout, and treatment.
            rng = random.Random(f"{seed}:{task_id}:{repeat}")
            position = {"x": rng.randint(18, 110), "y": rng.randint(70, 260)}
            for model in models:
                for condition in conditions:
                    row = dict(study=study, model=model, task_id=task_id, repeat=repeat, position=position, **condition)
                    row["id"] = digest({"seed": seed, **row})[:24]
                    rows.append(row)
    random.Random(seed).shuffle(rows)
    return rows


def natural_content(row):
    task = TASKS[row["task_id"]]
    # Same glyphs, typography and measured whitespace across DOM boundary arms.
    # The shared arm has both text nodes in one element; the separate arm has
    # distinct field containers. Text Range boxes exclude whitespace and labels.
    target, marker = html.escape(task["value"]), html.escape(task["marker"])
    if row["boundary"] == "visible":
        source = f"<span>{target}</span>"
        reference = f"<span data-reachability-marker='true'>{marker}</span>"
        if row["layout"] == "table":
            return (
                "<h2>Shipment details</h2><table style='table-layout:fixed'>"
                "<colgroup><col style='width:200px'><col></colgroup><thead><tr>"
                "<th>Source</th><th>Reference</th></tr></thead><tbody><tr>"
                f"<td>{source}</td><td>{reference}</td></tr></tbody></table>"
                "<p>Status: Ready | Service: Standard</p>"
            )
        grid = "display:grid;grid-template-columns:190px 1fr;gap:16px"
        if row["layout"] == "cards":
            return (
                f"<h2>Order overview</h2><div style='{grid}'>"
                f"<article><h3>Source</h3>{source}</article>"
                f"<article><h3>Reference</h3>{reference}</article></div>"
                "<p>Status: Ready | Service: Standard</p>"
            )
        return (
            f"<h2>Transfer request</h2><section style='{grid}'>"
            f"<div><label>Source</label><div class='readonly' style='border:1px solid "
            f"#ccd4df;padding:12px'>{source}</div></div>"
            f"<div><label>Reference</label><div class='readonly' style='border:1px solid "
            f"#ccd4df;padding:12px'>{reference}</div></div>"
            "</section><p>Service: Standard</p>"
        )
    gap = row["gap"]
    if row["boundary"] == "shared":
        values = f"<div id='pair'>{target}<i style='display:inline-block;width:{gap}px'></i>{marker}</div>"
    else:
        values = (
            f"<div id='pair'><span class='field'><span>{target}</span></span>"
            f"<i style='display:inline-block;width:{gap}px'></i>"
            f"<span class='field'><span>{marker}</span></span></div>"
        )
    if row["layout"] == "table":
        return (
            "<h2>Shipment details</h2><table><thead><tr><th>Field</th><th>Value / reference</th></tr></thead>"
            "<tbody><tr><td>Status</td><td>Ready</td></tr><tr><td>Source</td><td>"
            + values
            + "</td></tr><tr><td>Service</td><td>Standard</td></tr></tbody></table>"
        )
    if row["layout"] == "cards":
        return (
            "<h2>Order overview</h2><article><small>FULFILLMENT RECORD</small><h3>Source</h3>"
            + values
            + "<hr><p>Status: Ready &nbsp; | &nbsp; Service: Standard</p></article>"
        )
    return (
        "<h2>Transfer request</h2><section><label>Source</label><div class='readonly'>"
        + values
        + "</div><p class='hint'>Reference information is shown alongside the source.</p>"
        "<label>Service</label><div class='readonly'>Standard</div></section>"
    )


def leaf_page(row, path):
    task = TASKS[row["task_id"]]
    if row["study"] == "intervention":
        # Original inline fixture with sufficient width; no token changes.
        page = render_page("canvas-inline-bar", row["task_id"], path)
        return page.replace("width='1100'", "width='730'").replace("font-size:24px", "font-size:24px")
    pos = row["position"]
    if row["study"] == "iframe":
        content = (
            f"<h2>Record details</h2><p id='pair'>Source: {task['value']}</p>"
            f"<p data-reachability-marker='true'>Target marker: {task['marker']}</p>"
        )
    else:
        content = natural_content(row)
    form = (
        f"<form method='post' action='{path}/submit'><label for='destination'>Destination</label>"
        "<input id='destination' name='destination' autocomplete='off'><button>Submit</button></form>"
        if row["task_id"] == "copy-known-value"
        else ""
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Record workspace</title><style>"
        "*{box-sizing:border-box}html,body{margin:0;background:#f5f7fa;font:16px Arial;color:#182738}"
        "header{height:48px;padding:14px 24px;background:#20334d;color:white}"
        f"main{{position:absolute;left:{pos['x']}px;top:{pos['y']}px;width:630px}}"
        "h2{font-size:21px;margin:0 0 16px}h3{font-size:16px;margin:12px 0}"
        "table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px 8px;border-bottom:1px "
        "solid #ccd4df;text-align:left}"
        "article,section{padding:18px;background:white;border:1px solid #ccd4df;border-radius:8px}"
        "#pair{font-size:16px;white-space:nowrap}small,.hint{color:#53657c;font-size:12px}"
        ".readonly{padding:12px 0}label{display:block;font-weight:bold}"
        "form{margin-top:18px;width:360px}input{width:360px;height:44px;font-size:18px;margin:8px 0}"
        "button{display:block;padding:12px 24px;background:#244567;color:white;border:0;border-radius:4px}"
        "</style></head><body><header>Operations / Records</header><main>" + content + form + "</main></body></html>"
    )


class Harness:
    def __init__(self, rows, renderer=leaf_page):
        self.rows = {r["id"]: r for r in rows}
        self.renderer = renderer
        self.submissions = {}
        self.servers, self.threads, self.urls = [], [], []

    def __enter__(self):
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urlsplit(self.path).path.strip("/").split("/")
                if len(parts) != 3 or parts[0] != "case" or parts[1] not in harness.rows:
                    self.send_error(404)
                    return
                row = harness.rows[parts[1]]
                depth = int(parts[2])
                if depth:
                    next_origin = (depth % 2) if row.get("origin") == "cross" else 0
                    src = f"{harness.urls[next_origin]}/case/{row['id']}/{depth - 1}"
                    body = (
                        "<!doctype html><html><head><style>html,body{margin:0;width:100%;height:100%;overflow:hidden}"
                        "iframe{border:0;position:absolute;inset:0;width:100%;height:100%}</style></head>"
                        f"<body><iframe src='{src}'></iframe></body></html>"
                    )
                else:
                    body = harness.renderer(row, f"/case/{row['id']}/0")
                self.send_body(body)

            def do_POST(self):
                parts = urlsplit(self.path).path.strip("/").split("/")
                if len(parts) != 4 or parts[-1] != "submit" or parts[1] not in harness.rows:
                    self.send_error(404)
                    return
                values = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode(), keep_blank_values=True)
                harness.submissions[parts[1]] = values.get("destination", [""])[0]
                self.send_body("<html><body><p role='status'>Saved</p></body></html>")

            def send_body(self, body):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        for _ in range(2):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.servers.append(server)
            self.urls.append(f"http://127.0.0.1:{server.server_port}")
        for server in self.servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def url(self, row):
        return f"{self.urls[0]}/case/{row['id']}/{row.get('depth', 0)}"

    def __exit__(self, *args):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join()


GEOMETRY_JS = """({target,marker}) => {
 const find = value => {
   const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
   for(let n;n=walker.nextNode();) {
     if(['SCRIPT','STYLE'].includes(n.parentElement.tagName)) continue;
     const start=n.textContent.indexOf(value); if(start<0) continue;
     const r=document.createRange();r.setStart(n,start);r.setEnd(n,start+value.length);
     const b=r.getBoundingClientRect();return {node:n.parentElement,box:{x:b.x,y:b.y,width:b.width,height:b.height}};
   } return null;
 };
 const a=find(target),b=find(marker);if(!a||!b)return null;
 const path=n=>{let p=[];for(;n;n=n.parentElement)p.push(n);return p;};
 const pa=path(a.node),pb=path(b.node);const lca=pa.find(n=>pb.includes(n));
 const gapX=Math.max(0,a.box.x-b.box.x-b.box.width,b.box.x-a.box.x-a.box.width);
 const gapY=Math.max(0,a.box.y-b.box.y-b.box.height,b.box.y-a.box.y-a.box.height);
 return {target_box:a.box,distractor_box:b.box,pixel_distance:Math.hypot(gapX,gapY),
         dom_element_separation:pa.indexOf(lca)+pb.indexOf(lca)};
}"""


async def preflight(harness, rows, out):
    from playwright.async_api import async_playwright

    geometry = {}
    paired_images = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--site-per-process"])
        page = await browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        # Model and intervention do not alter the page. Probe each unique rendering.
        cache = {}
        for row in rows:
            key = digest({k: v for k, v in row.items() if k not in ("model", "id", "intervention")})
            if key in cache:
                geometry[row["id"]] = cache[key]
                continue
            await page.mouse.move(0, 0)
            await page.goto(harness.url(row), wait_until="networkidle")
            frame = page.frames[-1]
            task = TASKS[row["task_id"]]
            measured = await frame.evaluate(GEOMETRY_JS, {"target": task["value"], "marker": task["marker"]})
            if row["study"] == "intervention":
                box = await frame.locator("#target-canvas").bounding_box()
                measured = {"canvas_box": box, "pixel_distance": None, "dom_element_separation": None}
            assert measured is not None, row
            for name in ("target_box", "distractor_box", "canvas_box"):
                box = measured.get(name)
                if box:
                    assert 0 <= box["x"] < box["x"] + box["width"] <= 768, (row, box)
                    assert 0 <= box["y"] < box["y"] + box["height"] <= 768, (row, box)
            png = await page.screenshot(animations="disabled")
            measured["screenshot_sha256"] = hashlib.sha256(png).hexdigest()
            measured["frame_count"] = len(page.frames)
            if row["study"] == "iframe":
                assert len(page.frames) == row["depth"] + 1
                pair = (row["task_id"], row["repeat"])
                previous = paired_images.setdefault(pair, measured["screenshot_sha256"])
                (out / f"debug-{row['task_id']}-{row['repeat']}-{row['origin']}-{row['depth']}.png").write_bytes(png)
                assert previous == measured["screenshot_sha256"], ("Depth/origin screenshots differ", row, measured)
            if row["study"] == "generalization":
                assert abs(measured["pixel_distance"] - row["gap"]) < 0.1, (row, measured)
                assert measured["dom_element_separation"] == (0 if row["boundary"] == "shared" else 4)
            if row["task_id"] == "copy-known-value":
                box = await frame.locator("#destination").bounding_box()
                assert box and box["y"] + box["height"] <= 768
                measured["destination_box"] = box
                # End-to-end calibration: known screenshot coordinates must hit the field.
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                await page.keyboard.insert_text(task["value"])
                await page.keyboard.press("Enter")
                for _ in range(60):
                    if harness.submissions.get(row["id"]) == task["value"]:
                        break
                    await asyncio.sleep(0.05)
                assert harness.submissions.get(row["id"]) == task["value"], (
                    "Coordinate calibration failed",
                    row,
                    box,
                    harness.submissions.get(row["id"]),
                )
                harness.submissions.pop(row["id"], None)
            geometry[row["id"]] = measured
            cache[key] = measured
            sample = f"{row['layout']}-{row.get('boundary', '')}-{row.get('gap', '')}-{row['task_id']}"
            sample_path = out / "fixture-previews" / f"{sample}.png"
            if not sample_path.exists():
                sample_path.parent.mkdir(exist_ok=True)
                sample_path.write_bytes(png)
        await browser.close()
    atomic_write(out / "geometry.json", canonical_json_bytes(geometry))
    return geometry


def content_score(transcript, row, submitted):
    task = TASKS[row["task_id"]]
    typed = [a.parameters.get("text", "") for s in transcript.steps for a in s.actions if a.name == "type"]
    if row["task_id"] == "read-known-value":
        candidate = (transcript.final.answer if transcript.final else "") or ""
        emitted = candidate
    else:
        candidate = typed[-1] if typed else ""
        emitted = submitted or ""
    scores = score_value(emitted, task["value"], task["marker"])
    c = candidate.strip()
    if c == task["value"]:
        category = "correct_content"
    elif c and (task["value"] in c or c in task["value"]):
        category = "delimitation"
    elif not c:
        category = "no_answer"
    else:
        category = "substitution_or_other"
    return {
        **scores,
        "candidate_value": candidate,
        "emitted_value": emitted,
        "content_category": category,
        "delimitation_error": category == "delimitation",
        "content_exact": c == task["value"],
    }


async def execute(args):
    rows = matrix(args.study, args.models, args.repeats, args.seed)
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=False)
    source_bytes = Path(__file__).read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    atomic_write(out / "runner-source.py", source_bytes)
    spec = dict(
        study=args.study,
        models=args.models,
        repeats=args.repeats,
        seed=args.seed,
        planned_trials=len(rows),
        viewport=VIEWPORT,
        max_steps=8,
        timeout_seconds=90,
        source_sha256=source_hash,
        source_paper_sha256=hashlib.sha256(Path(args.paper).read_bytes()).hexdigest(),
        coordinate_contract=(
            "768x768 PNG at device scale 1; explicit pixel dimensions in prompt; pixel-center click preflight"
        ),
        randomization="Paired positions across models/conditions; shuffled trial order",
        scoring="All completed/timeout outcomes count; infrastructure errors excluded, up to 2 retries",
        intervention=FORMAT,
        matrix_sha256=digest(rows),
    )
    atomic_write(out / "resolved-spec.json", canonical_json_bytes(spec))
    atomic_write(out / "matrix.json", canonical_json_bytes(rows))
    observer.VIEWPORT.clear()
    observer.VIEWPORT.update(VIEWPORT)
    # Same dictionary is imported by runner; default frozen code stays untouched.
    with Harness(rows) as harness:
        geometry = await preflight(harness, rows, out)
        print(json.dumps({"preflight": "passed", "planned": len(rows)}), flush=True)
        if args.preflight_only:
            return
        semaphore = asyncio.Semaphore(args.concurrency)

        async def run(row):
            async with semaphore:
                attempts = []
                for attempt in range(3):
                    harness.submissions.pop(row["id"], None)
                    run_id = f"REV-{slug(out.name)}-{row['id']}-A{attempt}"
                    prompt = TASKS[row["task_id"]]["prompt"] + CALIBRATION
                    if row["intervention"] == "format":
                        prompt += FORMAT
                    transcript = await runner.run_vision_agent(
                        harness.url(row),
                        prompt,
                        row["model"],
                        run_id=run_id,
                        max_steps=8,
                        timeout_seconds=90,
                        archive_root=args.archive_root,
                    )
                    attempts.append(
                        {
                            "run_id": run_id,
                            "status": transcript.status,
                            "error": transcript.final.error if transcript.final else None,
                        }
                    )
                    if transcript.status != "infrastructure_error":
                        break
                result = {
                    **row,
                    **content_score(transcript, row, harness.submissions.get(row["id"])),
                    "geometry": geometry[row["id"]],
                    "attempts": attempts,
                    "run_id": run_id,
                    "runner_status": transcript.status,
                    "valid": transcript.status != "infrastructure_error",
                    "started_at": transcript.started_at.isoformat(),
                    "finished_at": utc_now(),
                    "input_tokens": sum(s.usage.input_tokens or 0 for s in transcript.steps if s.usage),
                    "output_tokens": sum(s.usage.output_tokens or 0 for s in transcript.steps if s.usage),
                }
                append_jsonl(out / "results.jsonl", result)
                return result

        results = []
        for future in asyncio.as_completed([asyncio.create_task(run(row)) for row in rows]):
            result = await future
            results.append(result)
            print(
                json.dumps(
                    {
                        "completed": len(results),
                        "planned": len(rows),
                        "model": result["model"],
                        "study": args.study,
                        "status": result["runner_status"],
                        "exact": result["exact_match"],
                    }
                ),
                flush=True,
            )
        valid = sum(r["valid"] for r in results)
        atomic_write(
            out / "report.json",
            canonical_json_bytes(
                dict(
                    planned=len(rows),
                    valid=valid,
                    complete=valid == len(rows),
                    source_sha256=source_hash,
                    results_sha256=hashlib.sha256((out / "results.jsonl").read_bytes()).hexdigest(),
                )
            ),
        )
        if valid != len(rows):
            raise SystemExit("Study incomplete: infrastructure failures remain")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--study", choices=["generalization", "intervention", "iframe", "boundary-cues"], required=True)
    p.add_argument("--models", nargs="+", default=list(MODELS))
    p.add_argument("--repeats", type=int, default=10)
    p.add_argument("--seed", type=int, default=20260915)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--archive-root", type=Path, default=Path("artifacts/revision-20260915/runs"))
    p.add_argument("--paper", default="/anonymous/Downloads/webagents1.a.pdf")
    p.add_argument("--preflight-only", action="store_true")
    args = p.parse_args()
    if args.repeats < 1 or args.concurrency < 1:
        p.error("repeats and concurrency must be positive")
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()
