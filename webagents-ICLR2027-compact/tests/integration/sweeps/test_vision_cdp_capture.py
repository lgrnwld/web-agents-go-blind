from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from webagents.ax.runner import _reserve_debug_port
from webagents.ax.targets import CDPConnection, TargetCollector, wait_for_debugger_url
from webagents.cdp.actions import PressAction as CDPPressAction
from webagents.cdp.actions import TypeAction as CDPTypeAction
from webagents.cdp.actions import execute_action as execute_cdp_action
from webagents.cdp.observer import capture_cdp_snapshot
from webagents.control.config import load_all_task_revisions, load_control_spec
from webagents.sweeps.config import load_sweep_spec
from webagents.sweeps.harness import StructuralFixtureHarness
from webagents.vision.actions import PressAction as VisionPressAction
from webagents.vision.actions import TypeAction as VisionTypeAction
from webagents.vision.actions import execute_action as execute_vision_action
from webagents.vision.observer import VIEWPORT, capture_screenshot

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_cross_origin_fixture_is_visible_to_vision_and_cdp() -> None:
    pytest.importorskip("playwright.async_api")
    control = load_control_spec(ROOT / "benchmarks/control/control.yaml")
    all_tasks = load_all_task_revisions(ROOT / "benchmarks/control/control.yaml", control)
    task = all_tasks[("read-known-value", 2)]
    copy_roster = next(item for item in control.tasks if item.id == "copy-known-value")
    copy_task = all_tasks[(copy_roster.id, copy_roster.active_revision)]
    spec = load_sweep_spec(ROOT / "benchmarks/sweeps/cdp-frame-traversal-full.yaml")
    condition = next(item for item in spec.conditions if item.id == "iframe-cross-depth-3")
    canvas_condition = next(item for item in spec.conditions if item.id == "rendered-canvas")

    from playwright.async_api import ViewportSize, async_playwright

    browser = None
    context = None
    connection = None
    collector = None
    manager = await async_playwright().start()
    try:
        with StructuralFixtureHarness(spec.harness, [task, copy_task], spec.conditions) as harness:
            harness.preflight()
            port = _reserve_debug_port()
            browser = await manager.chromium.launch(
                headless=True,
                args=[
                    f"--remote-debugging-port={port}",
                    "--remote-debugging-address=127.0.0.1",
                    "--site-per-process",
                ],
            )
            context = await browser.new_context(
                viewport=ViewportSize(width=VIEWPORT["width"], height=VIEWPORT["height"]),
                device_scale_factor=1,
            )
            page = await context.new_page()
            await page.goto(harness.condition_url(task, condition), wait_until="domcontentloaded")
            await page.wait_for_timeout(100)

            screenshot, visual_evidence = await capture_screenshot(page)
            assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")
            assert visual_evidence.visible is True

            connection = await CDPConnection.open(await wait_for_debugger_url(port))
            collector = TargetCollector(connection)
            await collector.initialize()
            _, observation, _, complete, missing = await capture_cdp_snapshot(collector, generation=0)
            assert complete is True, missing
            assert task.revision.level0.reachability_marker.encode() in observation

            await page.goto(harness.condition_url(task, canvas_condition), wait_until="networkidle")
            screenshot, visual_evidence = await capture_screenshot(page)
            assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")
            assert visual_evidence.visible is True
            _, observation, _, complete, missing = await capture_cdp_snapshot(collector, generation=1)
            assert complete is True, missing
            assert task.revision.level0.reachability_marker.encode() not in observation
            assert task.revision.level0.fixture.source_value.encode() not in observation

            level_zero = next(item for item in spec.conditions if item.id == "level-0")
            harness.reset(copy_task)
            await page.goto(harness.condition_url(copy_task, level_zero), wait_until="domcontentloaded")
            input_box = await page.locator("#destination").bounding_box()
            button_box = await page.get_by_role("button", name="Submit").bounding_box()
            assert input_box is not None and button_box is not None
            assert input_box["height"] >= 56
            assert button_box["height"] >= 56
            await execute_vision_action(
                VisionTypeAction(
                    action="type",
                    x=int(input_box["x"] + input_box["width"] / 2),
                    y=int(input_box["y"] + input_box["height"] / 2),
                    text=copy_task.revision.level0.fixture.source_value,
                ),
                page=page,
            )
            await execute_vision_action(VisionPressAction(action="press", key="Enter"), page=page)
            await page.wait_for_url("**/submit")
            assert harness.read_json_checker(copy_task) == copy_task.revision.level0.fixture.source_value

            harness.reset(copy_task)
            await page.goto(harness.condition_url(copy_task, level_zero), wait_until="domcontentloaded")
            envelope, _, refs, complete, missing = await capture_cdp_snapshot(collector, generation=2)
            assert complete is True, missing
            nodes = [
                node
                for target in envelope["targets"]
                for document in target.get("documents", [])
                for node in document.get("nodes", [])
            ]
            input_ref = next(node["ref"] for node in nodes if node.get("name") == "INPUT")
            result = await execute_cdp_action(
                CDPTypeAction(
                    action="type",
                    ref=input_ref,
                    text=copy_task.revision.level0.fixture.source_value,
                ),
                refs=refs,
                generation=2,
                connection=connection,
                page=page,
            )
            assert result.success is True
            assert await page.locator("#destination").input_value() == copy_task.revision.level0.fixture.source_value
            assert await page.evaluate("document.activeElement && document.activeElement.id") == "destination"
            await page.wait_for_timeout(50)
            result = await execute_cdp_action(
                CDPPressAction(action="press", key="Enter"),
                refs=refs,
                generation=2,
                connection=connection,
                page=page,
            )
            assert result.success is True
            await page.wait_for_url("**/submit")
            assert harness.read_json_checker(copy_task) == copy_task.revision.level0.fixture.source_value
    finally:
        if collector is not None:
            with contextlib.suppress(Exception):
                await collector.close()
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        await manager.stop()
