"""Viewport screenshot capture and independent benchmark reachability probing."""

from __future__ import annotations

from typing import Any

from webagents.schemas import ReachabilityEvidence

VIEWPORT = {"width": 1280, "height": 900}
OBSERVATION_VERSION = "playwright-viewport-png-v1"


def _intersects_viewport(box: dict[str, float], viewport: dict[str, int]) -> bool:
    return (
        box["width"] > 0
        and box["height"] > 0
        and box["x"] < viewport["width"]
        and box["y"] < viewport["height"]
        and box["x"] + box["width"] > 0
        and box["y"] + box["height"] > 0
    )


async def probe_benchmark_reachability(page: Any) -> ReachabilityEvidence:
    """Measure whether a generated target occupies the captured viewport.

    This privileged probe is scorer-only metadata. It is intentionally run separately
    from screenshot capture and none of its values enter the model request.
    """

    selectors = (
        "[data-reachability-marker='true']",
        "#target-canvas",
        "#shadow-host-0",
    )
    visible = 0
    seen: set[tuple[int, str]] = set()
    for frame_index, frame in enumerate(page.frames):
        for selector in selectors:
            locator = frame.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(count):
                key = (frame_index, f"{selector}:{index}")
                if key in seen:
                    continue
                seen.add(key)
                try:
                    box = await locator.nth(index).bounding_box()
                except Exception:
                    box = None
                if box is not None and _intersects_viewport(box, VIEWPORT):
                    visible += 1
    return ReachabilityEvidence(
        method="playwright-visible-target-v1",
        visible=visible > 0,
        target_count=visible,
    )


async def capture_screenshot(page: Any) -> tuple[bytes, ReachabilityEvidence]:
    evidence = await probe_benchmark_reachability(page)
    screenshot = await page.screenshot(type="png", full_page=False, animations="disabled")
    if not isinstance(screenshot, bytes):
        screenshot = bytes(screenshot)
    return screenshot, evidence
