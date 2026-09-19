#!/usr/bin/env python3
"""Run the reviewer-requested canvas delimitation and DOM layout control study.

The follow-up is intentionally separate from the frozen core grid. It uses the
same pinned vision and Browser Use runners, exact tasks, viewport, and checker
semantics, but serves purpose-built layout fixtures and records their source
hash in every study result.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import threading
import urllib.parse
from collections import defaultdict
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal

from webagents.capture.archive import canonical_json_bytes
from webagents.dom.runner import run_dom_agent
from webagents.io import append_jsonl, atomic_write
from webagents.vision import observer as vision_observer
from webagents.vision import runner as vision_runner
from webagents.vision.runner import run_vision_agent

TASKS = {
    "read-known-value": {
        "revision": 2,
        "prompt": "Read the Source value and finish with exactly that value as the answer.",
        "label": "Source",
        "value": "ALPHA-7391",
        "marker": "CONTROL_READ_VALUE_R2",
    },
    "copy-known-value": {
        "revision": 5,
        "prompt": (
            "The page has one Source value and one empty Destination field. Type exactly the Source value "
            "into Destination, press Enter to submit the form, then finish successfully."
        ),
        "label": "Source",
        "value": "BRAVO-4826",
        "marker": "CONTROL_COPY_VALUE_R5",
    },
}
LAYOUTS = (
    "canvas-inline-bar",
    "canvas-inline-two-spaces",
    "canvas-inline-gap-40px",
    "canvas-separate-line",
    "canvas-opposite-corner",
    "dom-inline-bar",
)
VISION_LAYOUTS = LAYOUTS
DOM_LAYOUTS = ("dom-inline-bar",)
DEFAULT_MODELS = ("foundry/gpt-5.6-terra", "foundry/Kimi-K2.6")
# OpenAI-compatible high-detail image preprocessing scales the 1280x900 core
# capture to 1092x768. Capturing at that delivered size keeps the prompt's pixel
# coordinate contract aligned with Playwright action coordinates.
VIEWPORT = {"width": 1092, "height": 768}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def slug(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-")


def task_form(trial_path: str) -> str:
    return (
        "<form method='post' action='"
        + html.escape(trial_path)
        + "/submit'><label for='destination'>Destination</label>"
        "<input id='destination' name='destination' autocomplete='off'>"
        "<button type='submit'>Submit</button></form>"
    )


def canvas_script(layout: str, task: dict[str, Any]) -> str:
    label = json.dumps(task["label"] + ":")
    value = json.dumps(task["value"])
    marker = json.dumps(task["marker"])
    common = (
        "const c=document.getElementById('target-canvas');const x=c.getContext('2d');"
        "x.font='24px sans-serif';x.fillStyle='#111';x.textBaseline='alphabetic';"
        f"const label={label},value={value},marker={marker};"
        "x.fillText(label,20,70);const vx=20+x.measureText(label).width+8;x.fillText(value,vx,70);"
        "const valueEnd=vx+x.measureText(value).width;"
    )
    if layout == "canvas-inline-bar":
        positioning = "x.fillText(' | '+marker,valueEnd,70);"
    elif layout == "canvas-inline-two-spaces":
        positioning = "x.fillText('  '+marker,valueEnd,70);"
    elif layout == "canvas-inline-gap-40px":
        positioning = "x.fillText(marker,valueEnd+40,70);"
    elif layout == "canvas-separate-line":
        positioning = "x.fillText(marker,20,130);"
    elif layout == "canvas-opposite-corner":
        positioning = "const w=x.measureText(marker).width;x.fillText(marker,c.width-w-20,c.height-25);"
    else:
        raise ValueError(f"not a canvas layout: {layout}")
    return "<script>" + common + positioning + "</script>"


def render_page(layout: str, task_id: str, trial_path: str) -> str:
    task = TASKS[task_id]
    title = html.escape(f"Canvas delimitation follow-up: {task_id} {layout}")
    style = (
        "body{font-family:sans-serif;margin:8px}main{max-width:1180px}"
        "canvas{border:1px solid #777;display:block}"
        "form{display:grid;gap:12px;margin-top:18px;width:420px}"
        "label{font-size:20px;font-weight:700}input{height:80px;font-size:20px}"
        "button{width:160px;height:56px;font-size:20px;font-weight:700}"
        ".dom-line{font-size:24px;height:320px;border:1px solid #777;box-sizing:border-box;padding:38px 20px}"
    )
    if layout == "dom-inline-bar":
        target = (
            "<div class='dom-line' data-benchmark-target='true' data-reachability-marker='true'>"
            f"<strong>{html.escape(task['label'])}:</strong> {html.escape(task['value'])} | "
            f"{html.escape(task['marker'])}</div>"
        )
    else:
        target = (
            "<canvas id='target-canvas' data-benchmark-target='true' width='1100' height='320' "
            "aria-label='Rendered target'></canvas>"
            + canvas_script(layout, task)
        )
    form = task_form(trial_path) if task_id == "copy-known-value" else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>{style}</style></head><body><main><h1>{title}</h1>"
        f"{target}{form}</main></body></html>"
    )


class FollowupHarness:
    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""
        self.submissions: dict[str, str | None] = {}

    def __enter__(self) -> FollowupHarness:
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
                if parts == ["health"]:
                    self._send("application/json", b'{"status":"ok"}')
                    return
                if len(parts) == 5 and parts[:1] == ["followup"]:
                    _, architecture, layout, task_id, trial_id = parts
                    if architecture not in {"vision", "dom"} or layout not in LAYOUTS or task_id not in TASKS:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    trial_path = "/" + "/".join(parts)
                    harness.submissions.setdefault(trial_id, None)
                    self._send("text/html", render_page(layout, task_id, trial_path).encode("utf-8"))
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802
                parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
                if len(parts) == 6 and parts[0] == "followup" and parts[-1] == "submit":
                    trial_id = parts[-2]
                    length = int(self.headers.get("Content-Length", "0"))
                    values = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
                    harness.submissions[trial_id] = values.get("destination", [""])[0]
                    body = b"<!doctype html><html><body><p role='status'>Saved</p></body></html>"
                    self._send("text/html", body)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _send(self, content_type: str, body: bytes) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self.server.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def url(self, architecture: str, layout: str, task_id: str, trial_id: str) -> str:
        return f"{self.base_url}/followup/{architecture}/{layout}/{task_id}/{trial_id}"


def score_value(emitted: str, expected: str, marker: str) -> dict[str, bool]:
    value = emitted.strip()
    return {
        "exact_match": value == expected,
        "substring_match": expected in emitted,
        "target_without_marker": expected in emitted and marker.casefold() not in emitted.casefold(),
    }


def fixture_hash() -> str:
    payload = {
        "tasks": TASKS,
        "layouts": LAYOUTS,
        "viewport": VIEWPORT,
        "renderings": {
            layout: {task: render_page(layout, task, "/HASHED-TRIAL") for task in TASKS}
            for layout in LAYOUTS
        },
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["architecture"], row["layout"], row["task_id"], row["model"])].append(row)
    summary: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items()):
        architecture, layout, task_id, model = key
        summary.append(
            {
                "architecture": architecture,
                "layout": layout,
                "task_id": task_id,
                "model": model,
                "trials": len(group),
                "exact_matches": sum(item["exact_match"] for item in group),
                "substring_matches": sum(item["substring_match"] for item in group),
                "target_without_marker_matches": sum(item["target_without_marker"] for item in group),
                "runner_success_claims": sum(item["runner_success_claimed"] for item in group),
                "infrastructure_errors": sum(item["runner_status"] == "infrastructure_error" for item in group),
            }
        )
    return summary


async def run_one(
    *,
    semaphore: asyncio.Semaphore,
    harness: FollowupHarness,
    architecture: Literal["vision", "dom"],
    layout: str,
    task_id: str,
    model: str,
    repeat: int,
    archive_root: Path,
    timeout_seconds: float,
    fixture_sha256: str,
    run_namespace: str,
    max_infrastructure_retries: int,
    max_steps: int,
) -> dict[str, Any]:
    task = TASKS[task_id]
    identity = canonical_json_bytes(
        {
            "architecture": architecture,
            "layout": layout,
            "task": task_id,
            "model": model,
            "repeat": repeat,
            "fixture_sha256": fixture_sha256,
        }
    )
    logical_id = hashlib.sha256(identity).hexdigest()[:24]
    url = harness.url(architecture, layout, task_id, logical_id)
    async with semaphore:
        started_at = utc_now()
        attempts: list[str] = []
        for attempt in range(max_infrastructure_retries + 1):
            run_id = f"FOLLOWUP-{architecture.upper()}-{run_namespace}-{logical_id}-A{attempt}"
            attempts.append(run_id)
            if architecture == "vision":
                archived = await run_vision_agent(
                    url,
                    task["prompt"],
                    model,
                    run_id=run_id,
                    max_steps=max_steps,
                    archive_root=archive_root,
                    timeout_seconds=timeout_seconds,
                )
            else:
                archived = await run_dom_agent(
                    url,
                    task["prompt"],
                    model,
                    run_id=run_id,
                    max_steps=max_steps,
                    archive_root=archive_root,
                    timeout_seconds=timeout_seconds,
                )
            # Match the core sweep: a complete timeout transcript is an
            # experimental outcome, while only provider/browser infrastructure
            # failure is retried and excluded from the allocated cell.
            if archived.status != "infrastructure_error":
                break
        if task_id == "read-known-value":
            emitted = (archived.final.answer if archived.final is not None else None) or ""
        else:
            emitted = harness.submissions.get(logical_id) or ""
        scores = score_value(emitted, task["value"], task["marker"])
        return {
            "schema_version": "1.0",
            "logical_trial_id": logical_id,
            "run_id": run_id,
            "attempt_run_ids": attempts,
            "infrastructure_retries": len(attempts) - 1,
            "architecture": architecture,
            "layout": layout,
            "task_id": task_id,
            "task_revision": task["revision"],
            "model": model,
            "repeat": repeat,
            "expected_value": task["value"],
            "marker_token": task["marker"],
            "emitted_value": emitted,
            **scores,
            "runner_status": archived.status,
            "runner_success_claimed": bool(archived.final and archived.final.success_claimed),
            "runner_final_error": archived.final.error if archived.final else None,
            "actions": sum(len(step.actions) for step in archived.steps),
            "input_tokens": sum(step.usage.input_tokens or 0 for step in archived.steps if step.usage),
            "output_tokens": sum(step.usage.output_tokens or 0 for step in archived.steps if step.usage),
            "fixture_sha256": fixture_sha256,
            "started_at": started_at,
            "finished_at": utc_now(),
        }


async def execute(args: argparse.Namespace) -> int:
    if args.output_dir.exists():
        raise RuntimeError(f"output already exists: {args.output_dir}")
    args.output_dir.mkdir(parents=True)
    args.archive_root.mkdir(parents=True, exist_ok=True)
    vision_observer.VIEWPORT.clear()
    vision_observer.VIEWPORT.update(VIEWPORT)
    vision_runner.VIEWPORT.clear()
    vision_runner.VIEWPORT.update(VIEWPORT)
    chosen_architectures = tuple(dict.fromkeys(args.architecture))
    layouts_by_architecture = {"vision": VISION_LAYOUTS, "dom": DOM_LAYOUTS}
    matrix: list[dict[str, Any]] = []
    for architecture in chosen_architectures:
        for layout in layouts_by_architecture[architecture]:
            for task_id in TASKS:
                for model in args.models:
                    for repeat in range(1, args.repeats + 1):
                        matrix.append(
                            {
                                "architecture": architecture,
                                "layout": layout,
                                "task_id": task_id,
                                "model": model,
                                "repeat": repeat,
                            }
                        )
    def randomization_key(item: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json_bytes({**item, "seed": args.seed})).hexdigest()

    matrix.sort(key=randomization_key)
    fixture_sha256 = fixture_hash()
    spec = {
        "schema_version": "1.0",
        "study": "canvas-delimitation-followup",
        "architectures": chosen_architectures,
        "layouts_by_architecture": {
            architecture: layouts_by_architecture[architecture] for architecture in chosen_architectures
        },
        "tasks": TASKS,
        "models": args.models,
        "repeats": args.repeats,
        "concurrency": args.concurrency,
        "timeout_seconds": args.timeout_seconds,
        "max_steps": args.max_steps,
        "max_infrastructure_retries": args.max_infrastructure_retries,
        "terminal_outcome_policy": "success, failure, and timeout count; infrastructure_error is retried",
        "resume_from": str(args.resume_from) if args.resume_from is not None else None,
        "coordinate_calibration": (
            "Capture at the 1092x768 high-detail delivered-image dimensions so model-returned pixel "
            "coordinates and Playwright action coordinates share one space."
        ),
        "seed": args.seed,
        "fixture_sha256": fixture_sha256,
        "planned_trials": len(matrix),
    }
    atomic_write(args.output_dir / "resolved-spec.json", canonical_json_bytes(spec))
    atomic_write(args.output_dir / "matrix.jsonl", b"")
    for item in matrix:
        append_jsonl(args.output_dir / "matrix.jsonl", item)

    semaphore = asyncio.Semaphore(args.concurrency)
    run_namespace = slug(args.output_dir.name)[:24]
    rows: list[dict[str, Any]] = []
    atomic_write(args.output_dir / "results.jsonl", b"")
    inherited_by_key: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
    if args.resume_from is not None:
        for line in (args.resume_from / "results.jsonl").read_text(encoding="utf-8").splitlines():
            inherited = json.loads(line)
            if inherited.get("fixture_sha256") != fixture_sha256:
                raise RuntimeError("resume source uses a different fixture hash")
            if inherited.get("runner_status") == "infrastructure_error":
                continue
            key = (
                inherited["architecture"],
                inherited["layout"],
                inherited["task_id"],
                inherited["model"],
                int(inherited["repeat"]),
            )
            inherited_by_key[key] = {**inherited, "source": "inherited"}
        atomic_write(args.output_dir / "inherited-results.jsonl", b"")
        for inherited in inherited_by_key.values():
            rows.append(inherited)
            append_jsonl(args.output_dir / "results.jsonl", inherited)
            append_jsonl(args.output_dir / "inherited-results.jsonl", inherited)
    live_matrix = [
        item
        for item in matrix
        if (item["architecture"], item["layout"], item["task_id"], item["model"], item["repeat"])
        not in inherited_by_key
    ]
    with FollowupHarness() as harness:
        pending = [
            asyncio.create_task(
                run_one(
                    semaphore=semaphore,
                    harness=harness,
                    archive_root=args.archive_root,
                    timeout_seconds=args.timeout_seconds,
                    fixture_sha256=fixture_sha256,
                    run_namespace=run_namespace,
                    max_infrastructure_retries=args.max_infrastructure_retries,
                    max_steps=args.max_steps,
                    **item,
                )
            )
            for item in live_matrix
        ]
        for completed in asyncio.as_completed(pending):
            row = await completed
            rows.append(row)
            append_jsonl(args.output_dir / "results.jsonl", row)
            print(
                json.dumps(
                    {
                        "completed": len(rows),
                        "planned": len(matrix),
                        "architecture": row["architecture"],
                        "layout": row["layout"],
                        "task": row["task_id"],
                        "model": row["model"],
                        "exact": row["exact_match"],
                        "status": row["runner_status"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    summaries = summarize(rows)
    atomic_write(args.output_dir / "summary.json", canonical_json_bytes({"cells": summaries}))
    infrastructure_errors = sum(row["runner_status"] == "infrastructure_error" for row in rows)
    timeouts = sum(row["runner_status"] == "timeout" for row in rows)
    report = {
        "schema_version": "1.0",
        "verdict": (
            "complete"
            if len(rows) == len(matrix) and infrastructure_errors == 0
            else "incomplete"
        ),
        "planned_trials": len(matrix),
        "completed_trials": len(rows),
        "fixture_sha256": fixture_sha256,
        "matrix_sha256": hashlib.sha256((args.output_dir / "matrix.jsonl").read_bytes()).hexdigest(),
        "results_sha256": hashlib.sha256((args.output_dir / "results.jsonl").read_bytes()).hexdigest(),
        "infrastructure_error_count": infrastructure_errors,
        "timeout_count": timeouts,
        "inherited_trial_count": len(inherited_by_key),
    }
    atomic_write(args.output_dir / "report.json", canonical_json_bytes(report))
    return 0 if report["verdict"] == "complete" else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--output-dir", type=Path, required=True)
    result.add_argument("--archive-root", type=Path, default=Path("artifacts"))
    result.add_argument("--architecture", choices=("vision", "dom"), action="append", default=[])
    result.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    result.add_argument("--repeats", type=int, default=10)
    result.add_argument("--concurrency", type=int, default=4)
    result.add_argument("--timeout-seconds", type=float, default=180.0)
    result.add_argument("--max-infrastructure-retries", type=int, default=2)
    result.add_argument("--max-steps", type=int, default=8)
    result.add_argument("--resume-from", type=Path)
    result.add_argument("--seed", type=int, default=20260904)
    return result


def main() -> int:
    args = parser().parse_args()
    if not args.architecture:
        args.architecture = ["vision", "dom"]
    if (
        args.repeats < 1
        or args.concurrency < 1
        or args.max_steps < 1
        or args.timeout_seconds <= 0
        or args.max_infrastructure_retries < 0
    ):
        raise SystemExit("repeats, concurrency, and timeout must be positive; retries must be nonnegative")
    return asyncio.run(execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
