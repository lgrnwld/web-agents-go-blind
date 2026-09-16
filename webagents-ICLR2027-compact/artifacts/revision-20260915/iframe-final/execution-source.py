#!/usr/bin/env python3
"""Resume an interrupted revision study from its frozen fixture source and geometry.

The optional takeover stops only the exact earlier command for the same output
folder. Completed rows are inherited verbatim; unfinished trials get new IDs in
the archive namespace. No completed trial is re-billed or overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import runpy
import signal
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from webagents.capture.archive import canonical_json_bytes
from webagents.io import append_jsonl, atomic_write


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


async def execute(args):
    source = args.source
    spec = json.loads((source / "resolved-spec.json").read_text())
    code = (source / "runner-source.py").read_bytes()
    assert hashlib.sha256(code).hexdigest() == spec["source_sha256"]
    rows = json.loads((source / "matrix.json").read_text())
    assert hashlib.sha256(canonical_json_bytes(rows)).hexdigest() == spec["matrix_sha256"]
    geometry = json.loads((source / "geometry.json").read_text())
    assert {r["id"] for r in rows} == set(geometry)
    if args.takeover:
        processes = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
        found = []
        for line in processes.splitlines():
            pid_text, command = line.strip().split(None, 1)
            if (
                any(
                    x in command
                    for x in (" -m scripts.run_revision_experiments ", " -m scripts.resume_revision_experiment ")
                )
                and f"--output-dir {source} " in command
                and not any(x in command for x in ("/bin/zsh", "/bin/bash"))
            ):
                found.append(int(pid_text))
        assert len(found) == 1, f"Expected exactly one source process; found {found}"
        os.kill(found[0], signal.SIGINT)
        for _ in range(50):
            try:
                os.kill(found[0], 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        else:
            os.kill(found[0], signal.SIGTERM)
            await asyncio.sleep(1)
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(found[0])], capture_output=True, text=True)
            assert not status.stdout.strip() or status.stdout.strip().startswith("Z"), "Source process still active"
    existing = read_rows(source / "results.jsonl")
    inherited = {r["id"]: r for r in existing if r["valid"]}
    assert len(inherited) == sum(r["valid"] for r in existing)
    assert set(inherited).issubset({r["id"] for r in rows})
    module = runpy.run_path(str(source / "runner-source.py"))
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=False)
    atomic_write(out / "runner-source.py", code)
    atomic_write(out / "matrix.json", canonical_json_bytes(rows))
    atomic_write(out / "geometry.json", canonical_json_bytes(geometry))
    atomic_write(
        out / "resolved-spec.json",
        canonical_json_bytes(
            {
                **spec,
                "concurrency": args.concurrency,
                "resume_source": str(source),
                "inherited_trials": len(inherited),
                "resume_results_sha256": hashlib.sha256((source / "results.jsonl").read_bytes()).hexdigest(),
                "execution_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
        ),
    )
    atomic_write(out / "execution-source.py", Path(__file__).read_bytes())
    for r in inherited.values():
        append_jsonl(out / "results.jsonl", r)
    module["observer"].VIEWPORT.clear()
    module["observer"].VIEWPORT.update(module["VIEWPORT"])
    pending = [r for r in rows if r["id"] not in inherited]
    sem = asyncio.Semaphore(args.concurrency)
    model_sems = {m: asyncio.Semaphore(8) for m in spec["models"]}
    initial_limits = {m: args.mini_concurrency if m.endswith("gpt-5-mini") else 4 for m in spec["models"]}
    atomic_write(out / "concurrency.json", canonical_json_bytes(initial_limits))
    active = {m: 0 for m in spec["models"]}
    last_limits = None

    @asynccontextmanager
    async def model_gate(model):
        nonlocal last_limits
        while True:
            limits = json.loads((out / "concurrency.json").read_text())
            assert set(limits) == set(active) and all(isinstance(v, int) and 0 <= v <= 8 for v in limits.values())
            if limits != last_limits:
                append_jsonl(out / "concurrency-log.jsonl", {"at": module["utc_now"](), "limits": limits})
                last_limits = limits
            if active[model] < limits[model]:
                active[model] += 1
                break
            await asyncio.sleep(1)
        try:
            yield
        finally:
            active[model] -= 1

    results = list(inherited.values())
    with module["Harness"](rows) as harness:

        async def run(row):
            async with model_sems[row["model"]], model_gate(row["model"]), sem:
                attempts = []
                for attempt in range(10):
                    harness.submissions.pop(row["id"], None)
                    run_id = f"REV-{module['slug'](out.name)}-{row['id']}-A{attempt}"
                    prompt = module["TASKS"][row["task_id"]]["prompt"] + module["CALIBRATION"]
                    if row["intervention"] == "format":
                        prompt += module["FORMAT"]
                    t = await module["runner"].run_vision_agent(
                        harness.url(row),
                        prompt,
                        row["model"],
                        run_id=run_id,
                        max_steps=8,
                        timeout_seconds=90,
                        archive_root=args.archive_root,
                    )
                    attempts.append({"run_id": run_id, "status": t.status, "error": t.final.error if t.final else None})
                    if t.status != "infrastructure_error":
                        break
                    if t.final and "429" in (t.final.error or ""):
                        await asyncio.sleep(60 * min(attempt + 1, 3))
                    elif attempt >= 2:
                        break
                result = {
                    **row,
                    **module["content_score"](t, row, harness.submissions.get(row["id"])),
                    "geometry": geometry[row["id"]],
                    "attempts": attempts,
                    "run_id": run_id,
                    "runner_status": t.status,
                    "valid": t.status != "infrastructure_error",
                    "started_at": t.started_at.isoformat(),
                    "finished_at": module["utc_now"](),
                    "input_tokens": sum(s.usage.input_tokens or 0 for s in t.steps if s.usage),
                    "output_tokens": sum(s.usage.output_tokens or 0 for s in t.steps if s.usage),
                }
                append_jsonl(out / "results.jsonl", result)
                return result

        for future in asyncio.as_completed([asyncio.create_task(run(row)) for row in pending]):
            r = await future
            results.append(r)
            print(
                json.dumps(
                    {"completed": len(results), "planned": len(rows), "model": r["model"], "status": r["runner_status"]}
                ),
                flush=True,
            )
    valid = sum(r["valid"] for r in results)
    atomic_write(
        out / "report.json",
        canonical_json_bytes(
            {
                "planned": len(rows),
                "valid": valid,
                "complete": valid == len(rows),
                "source_sha256": spec["source_sha256"],
                "results_sha256": hashlib.sha256((out / "results.jsonl").read_bytes()).hexdigest(),
            }
        ),
    )
    if valid != len(rows):
        raise SystemExit("Study incomplete")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--archive-root", type=Path, default=Path("artifacts/revision-20260915/runs"))
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--takeover", action="store_true")
    p.add_argument("--mini-concurrency", type=int, default=1)
    args = p.parse_args()
    if args.concurrency < 1 or not 1 <= args.mini_concurrency <= 8:
        p.error("concurrency must be positive")
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()
