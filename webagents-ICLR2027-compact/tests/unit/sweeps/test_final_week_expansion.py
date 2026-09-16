from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from webagents.ax.observer import capture_ax_tree
from webagents.ax.targets import TargetHandle
from webagents.dom.runner import run_dom_agent
from webagents.sweeps.config import load_dom_light_spec, load_sweep_spec
from webagents.sweeps.prospective import plan_prospective_sweep

ROOT = Path(__file__).resolve().parents[3]


class _Connection:
    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        del session_id
        if method == "Page.getFrameTree":
            return {
                "frameTree": {
                    "frame": {"id": "root", "url": "http://127.0.0.1:8000/task"},
                    "childFrames": [
                        {"frame": {"id": "cross", "url": "http://127.0.0.1:9000/frame"}}
                    ],
                }
            }
        if method == "Accessibility.getFullAXTree":
            frame = (params or {}).get("frameId")
            name = "CROSS_ORIGIN_MARKER" if frame == "cross" else "top-level"
            return {"nodes": [{"nodeId": frame or "root", "name": {"value": name}}]}
        return {}


class _Collector:
    connection = _Connection()

    async def refresh(self) -> list[TargetHandle]:
        return [
            TargetHandle("page", "page-session", "page", "http://127.0.0.1:8000/task"),
            TargetHandle("iframe", "iframe-session", "iframe", "http://127.0.0.1:9000/frame"),
        ]

    def invalidate(self, target_id: str) -> None:
        del target_id


@pytest.mark.asyncio
async def test_ax_same_origin_policy_excludes_cross_origin_frame_observations() -> None:
    _, unrestricted, complete, _ = await capture_ax_tree(_Collector())  # type: ignore[arg-type]
    assert complete is True
    assert b"CROSS_ORIGIN_MARKER" in unrestricted

    envelope, restricted, complete, missing = await capture_ax_tree(
        _Collector(),  # type: ignore[arg-type]
        observation_policy="same_origin_only",
        top_level_url="http://127.0.0.1:8000/task",
    )
    assert complete is True
    assert missing == []
    assert envelope["observation_policy"] == "same_origin_only"
    assert b"CROSS_ORIGIN_MARKER" not in restricted
    assert len(envelope["targets"]) == 1


def test_full_dom_and_restricted_tradeoff_specs_freeze_the_intended_allocations() -> None:
    full = load_dom_light_spec(ROOT / "benchmarks/sweeps/dom-extraction-full.yaml")
    dom_tradeoff = load_dom_light_spec(ROOT / "benchmarks/sweeps/dom-tradeoff-restricted.yaml")
    ax_tradeoff = load_sweep_spec(ROOT / "benchmarks/sweeps/ax-tradeoff-restricted.yaml")

    assert full.allocation.additional_valid_trials_per_cell == 10
    assert full.inherit_results_from == "artifacts/corrections/reachability-v2/dom-light-v1"
    assert full.observation_policy == "unrestricted"
    assert dom_tradeoff.observation_policy == "same_origin_only"
    assert ax_tradeoff.observation_policy == "same_origin_only"
    assert [item.id for item in dom_tradeoff.conditions] == [
        "level-0",
        "iframe-cross-depth-1",
        "iframe-cross-depth-2",
        "iframe-cross-depth-3",
    ]
    assert dom_tradeoff.conditions == ax_tradeoff.conditions


def test_vision_and_cdp_plans_are_matched_and_launchable(tmp_path: Path) -> None:
    plans = []
    for name in ("vision-full.yaml", "cdp-frame-traversal-full.yaml"):
        output = tmp_path / name.removesuffix(".yaml")
        plans.append(
            plan_prospective_sweep(
                ROOT / "benchmarks/sweeps" / name,
                control_spec_path=ROOT / "benchmarks/control/control.yaml",
                output_dir=output,
            )
        )
        assert (output / "plan-receipt.json").is_file()
        rows = [json.loads(line) for line in (output / "matrix.jsonl").read_text().splitlines()]
        assert len(rows) == 440
        assert len({row["logical_trial_id"] for row in rows}) == 440

    assert plans[0]["planned_trial_count"] == plans[1]["planned_trial_count"] == 440
    assert plans[0]["condition_inventory_sha256"] == plans[1]["condition_inventory_sha256"]
    assert plans[0]["control_spec_sha256"] == plans[1]["control_spec_sha256"]
    assert plans[0]["launchable"] is plans[1]["launchable"] is True
    assert plans[0]["blocking_requirements"] == plans[1]["blocking_requirements"] == []


@pytest.mark.asyncio
async def test_dom_runner_rejects_unknown_observation_policy_without_provider_calls(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="observation_policy"):
        await run_dom_agent(
            "http://fixture.invalid/task",
            "read the value",
            "foundry/fixture",
            archive_root=tmp_path,
            observation_policy="unknown",
        )
