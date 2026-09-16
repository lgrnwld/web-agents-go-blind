#!/usr/bin/env python3
"""Provider-free iframe reachability replication with second implementations."""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import playwright
import selenium
from playwright.sync_api import sync_playwright
from selenium import webdriver
from selenium.webdriver.common.by import By

from webagents.capture.archive import canonical_json_bytes
from webagents.io import append_jsonl, atomic_write
from webagents.sweeps.harness import StructuralFixtureHarness
from webagents.sweeps.schemas import ResolvedAccessibilitySweep

DEFAULT_RESOLVED = Path("artifacts/sweeps-r5/vision-final/vision-full-scan-v2/resolved-spec.json")
DEFAULT_OUTPUT = Path("analysis/reviewer-response-v1/iframe-implementation-replication")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def selenium_sources(driver: webdriver.Chrome) -> list[str]:
    sources = [driver.page_source]
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
    for frame in frames:
        driver.switch_to.frame(frame)
        sources.extend(selenium_sources(driver))
        driver.switch_to.parent_frame()
    return sources


def axis_cells(resolved: ResolvedAccessibilitySweep) -> list[dict[str, str]]:
    conditions = {condition.id: condition for condition in resolved.spec.conditions}
    cells: list[dict[str, str]] = []
    for origin in ("same", "cross"):
        for depth in range(4):
            condition_id = "level-0" if depth == 0 else f"iframe-{origin}-depth-{depth}"
            if condition_id not in conditions:
                raise RuntimeError(f"missing condition {condition_id}")
            cells.append({"axis_origin": origin, "axis_depth": str(depth), "condition_id": condition_id})
    return cells


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolved", type=Path, default=DEFAULT_RESOLVED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError(f"output already exists: {args.output}")
    args.output.mkdir(parents=True)
    resolved = ResolvedAccessibilitySweep.model_validate_json(args.resolved.read_bytes())
    tasks = {task.revision.task_id: task for task in resolved.tasks}
    conditions = {condition.id: condition for condition in resolved.spec.conditions}
    cells = axis_cells(resolved)
    rows: list[dict[str, Any]] = []
    started_at = datetime.now(UTC)

    with sync_playwright() as playwright_runtime:
        chrome_path = playwright_runtime.chromium.executable_path
        options = webdriver.ChromeOptions()
        options.binary_location = chrome_path
        options.add_argument("--headless=new")
        options.add_argument("--site-per-process")
        selenium_driver = webdriver.Chrome(options=options)
        playwright_browser = playwright_runtime.chromium.launch(headless=True, args=["--site-per-process"])
        playwright_page = playwright_browser.new_page()
        try:
            with StructuralFixtureHarness(resolved.spec.harness, resolved.tasks, resolved.spec.conditions) as harness:
                harness.preflight()
                for task_id, task in tasks.items():
                    marker = task.revision.level0.reachability_marker
                    for cell in cells:
                        condition = conditions[cell["condition_id"]]
                        url = harness.condition_url(task, condition)

                        selenium_driver.get(url)
                        sources = selenium_sources(selenium_driver)
                        rows.append(
                            {
                                "implementation": "selenium-recursive-page-source",
                                "implementation_version": selenium.__version__,
                                "browser_version": selenium_driver.capabilities.get("browserVersion"),
                                "task_id": task_id,
                                **cell,
                                "marker": marker,
                                "reachable": any(marker in source for source in sources),
                                "documents_captured": len(sources),
                                "capture_sha256": hashlib.sha256(
                                    canonical_json_bytes(sources)
                                ).hexdigest(),
                            }
                        )

                        playwright_page.goto(url, wait_until="domcontentloaded")
                        expected_frame_count = int(cell["axis_depth"]) + 1
                        for _ in range(50):
                            if len(playwright_page.frames) >= expected_frame_count:
                                break
                            playwright_page.wait_for_timeout(100)
                        snapshots: list[str] = []
                        for frame in playwright_page.frames:
                            try:
                                snapshots.append(frame.locator("body").aria_snapshot(timeout=5000))
                            except Exception as exc:
                                snapshots.append(f"CAPTURE_ERROR:{type(exc).__name__}:{exc}")
                        rows.append(
                            {
                                "implementation": "playwright-per-frame-aria-snapshot",
                                "implementation_version": getattr(playwright, "__version__", "1.61.0"),
                                "browser_version": playwright_browser.version,
                                "task_id": task_id,
                                **cell,
                                "marker": marker,
                                "reachable": any(marker in snapshot for snapshot in snapshots),
                                "documents_captured": len(snapshots),
                                "capture_sha256": hashlib.sha256(
                                    canonical_json_bytes(snapshots)
                                ).hexdigest(),
                            }
                        )
        finally:
            playwright_browser.close()
            selenium_driver.quit()

    results_path = args.output / "results.jsonl"
    atomic_write(results_path, b"")
    for row in rows:
        append_jsonl(results_path, row)
    by_implementation: dict[str, dict[str, int]] = {}
    for implementation in sorted({row["implementation"] for row in rows}):
        selected = [row for row in rows if row["implementation"] == implementation]
        by_implementation[implementation] = {
            "reachable": sum(row["reachable"] for row in selected),
            "trials": len(selected),
        }
    report = {
        "schema_version": "1.0",
        "design": "2 tasks x 2 origin axes x depths 0..3 x 2 independent implementations",
        "note": (
            "The shared depth-0 page is measured once under each conceptual origin axis, yielding eight axis cells."
        ),
        "source_resolved_spec": str(args.resolved),
        "source_resolved_spec_sha256": sha256_file(args.resolved),
        "results_sha256": sha256_file(results_path),
        "results": by_implementation,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
    }
    atomic_write(args.output / "report.json", canonical_json_bytes(report))
    lines = [
        "# Independent iframe implementation replication",
        "",
        report["design"] + ".",
        "",
        "| Implementation | Reachable |",
        "| --- | ---: |",
    ]
    for implementation, result in by_implementation.items():
        lines.append(f"| {implementation} | {result['reachable']}/{result['trials']} |")
    lines.extend(
        [
            "",
            "Selenium recursively switched into each iframe and inspected each frame's own page source. "
            "The second AX path used Playwright's public per-frame ARIA snapshot API; it does not call "
            "the core runner's raw `Accessibility.getFullAXTree` collector.",
            "",
        ]
    )
    (args.output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return 0 if all(item["reachable"] == item["trials"] for item in by_implementation.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
