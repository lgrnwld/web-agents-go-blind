from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from webagents.ax.observer import capture_ax_tree
from webagents.ax.runner import _reserve_debug_port
from webagents.ax.targets import CDPConnection, TargetCollector, wait_for_debugger_url
from webagents.control.config import load_all_task_revisions, load_control_spec
from webagents.sweeps.config import load_sweep_spec
from webagents.sweeps.harness import StructuralFixtureHarness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("WEBAGENT_RUN_BROWSER_INTEGRATION") != "1",
        reason="set WEBAGENT_RUN_BROWSER_INTEGRATION=1 to run pinned-browser AX fixtures",
    ),
]

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_pinned_browser_structural_reachability_matches_dated_fixture() -> None:
    from playwright.async_api import async_playwright

    expected = json.loads((ROOT / "tests/fixtures/ax-structural-behavior-1.61.0.json").read_bytes())
    spec = load_sweep_spec(ROOT / "benchmarks/sweeps/accessibility-tree-full.yaml")
    control = load_control_spec(ROOT / "benchmarks/control/control.yaml")
    tasks = load_all_task_revisions(ROOT / "benchmarks/control/control.yaml", control)
    task = tasks[(control.tasks[0].id, control.tasks[0].active_revision)]
    marker = task.revision.level0.reachability_marker.encode()

    with StructuralFixtureHarness(spec.harness, [task], spec.conditions) as harness:
        harness.preflight()
        playwright = await async_playwright().start()
        port = _reserve_debug_port()
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                "--site-per-process",
            ],
        )
        assert browser.version == expected["browser_version"]
        context = await browser.new_context()
        page = await context.new_page()
        connection: CDPConnection | None = None
        collector: TargetCollector | None = None
        observed: dict[str, bool] = {}
        try:
            await page.goto(harness.condition_url(task, spec.conditions[0]))
            connection = await CDPConnection.open(await wait_for_debugger_url(port))
            collector = TargetCollector(connection)
            await collector.initialize()
            for condition in spec.conditions:
                await page.goto(harness.condition_url(task, condition))
                await page.wait_for_timeout(200)
                _, encoded, complete, missing = await capture_ax_tree(collector)
                assert complete, missing
                observed[condition.id] = marker in encoded
        finally:
            if collector is not None:
                await collector.close()
            if connection is not None:
                await connection.close()
            await context.close()
            await browser.close()
            await playwright.stop()
    assert observed == expected["marker_reachable"]


@pytest.mark.asyncio
async def test_same_origin_only_ax_policy_keeps_same_origin_and_excludes_cross_origin_frames() -> None:
    from playwright.async_api import async_playwright

    spec = load_sweep_spec(ROOT / "benchmarks/sweeps/accessibility-tree-full.yaml")
    control = load_control_spec(ROOT / "benchmarks/control/control.yaml")
    tasks = load_all_task_revisions(ROOT / "benchmarks/control/control.yaml", control)
    task = tasks[(control.tasks[0].id, control.tasks[0].active_revision)]
    marker = task.revision.level0.reachability_marker.encode()
    conditions = {item.id: item for item in spec.conditions}

    with StructuralFixtureHarness(spec.harness, [task], spec.conditions) as harness:
        playwright = await async_playwright().start()
        port = _reserve_debug_port()
        browser = await playwright.chromium.launch(
            headless=True,
            args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1", "--site-per-process"],
        )
        context = await browser.new_context()
        page = await context.new_page()
        connection: CDPConnection | None = None
        collector: TargetCollector | None = None
        try:
            await page.goto(harness.condition_url(task, conditions["level-0"]))
            connection = await CDPConnection.open(await wait_for_debugger_url(port))
            collector = TargetCollector(connection)
            await collector.initialize()
            observed: dict[str, bool] = {}
            for condition_id in ("iframe-same-depth-3", "iframe-cross-depth-3"):
                await page.goto(harness.condition_url(task, conditions[condition_id]))
                await page.wait_for_timeout(200)
                _, encoded, complete, missing = await capture_ax_tree(
                    collector,
                    observation_policy="same_origin_only",
                    top_level_url=page.url,
                )
                assert complete, missing
                observed[condition_id] = marker in encoded
        finally:
            if collector is not None:
                await collector.close()
            if connection is not None:
                await connection.close()
            await context.close()
            await browser.close()
            await playwright.stop()
    assert observed == {"iframe-same-depth-3": True, "iframe-cross-depth-3": False}
