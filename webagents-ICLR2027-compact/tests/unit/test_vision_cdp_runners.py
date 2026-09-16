from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from webagents.ax.targets import TargetHandle
from webagents.capture.archive import (
    RunArchive,
    canonical_json_bytes,
    scan_model_observations,
    scan_run_observations,
)
from webagents.cdp.observer import capture_cdp_snapshot
from webagents.cdp.prompt import build_model_request as build_cdp_request
from webagents.schemas import (
    CaptureIdentity,
    ImplementationIdentity,
    ModelCallMetadata,
    ReachabilityEvidence,
    RunInput,
    RunManifest,
    RuntimeIdentity,
)
from webagents.vision.actions import ClickAction, FinishAction, TypeAction, execute_action
from webagents.vision.prompt import build_model_request as build_vision_request


def _manifest(run_id: str, agent_class: str) -> RunManifest:
    now = datetime.now(UTC)
    return RunManifest.model_validate(
        {
            "run_id": run_id,
            "agent_class": agent_class,
            "input": RunInput(
                page_url="http://localhost/task",
                task_prompt="Complete the task.",
                model="foundry/deployment",
            ),
            "runtime": RuntimeIdentity(started_at=now, host_platform="test", python="3.12"),
            "implementation": ImplementationIdentity(
                repository_commit=None,
                source_state="unversioned",
                framework_name="fixture",
                framework_version="1",
                framework_commit="fixture",
                prompt_version="fixture",
            ),
            "capture": CaptureIdentity(
                observation_encoding="binary" if agent_class == "vision" else "utf-8"
            ),
        }
    )


def test_archive_binds_exact_png_and_cdp_snapshot_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = datetime.now(UTC)
    png = b"\x89PNG\r\n\x1a\nfixture-pixels"
    _, vision_request = build_vision_request(
        model="deployment",
        task_prompt="Complete the task.",
        screenshot_bytes=png,
        previous_action_result=None,
    )
    vision = RunArchive.create(tmp_path, _manifest("VISION-ARCHIVE", "vision"))
    vision.record_model_call(
        step=0,
        attempt=0,
        observation_kind="screenshot",
        observation_bytes=png,
        model_request_bytes=vision_request,
        metadata=ModelCallMetadata(
            run_id="VISION-ARCHIVE",
            step=0,
            attempt=0,
            agent_class="vision",
            observation_kind="screenshot",
            captured_at=captured,
            model_call_started_at=captured,
            reachability=ReachabilityEvidence(
                method="playwright-visible-target-v1",
                visible=True,
                target_count=1,
            ),
        ),
    )
    request_value = json.loads(vision_request)
    data_url = request_value["messages"][1]["content"][1]["image_url"]["url"]
    assert base64.b64decode(data_url.split(",", 1)[1]) == png
    assert (vision.run_dir / "observations/step-000/observation.png").read_bytes() == png

    snapshot = canonical_json_bytes({"capture_version": "fixture", "targets": [{"marker": "CDP_MARKER"}]})
    _, cdp_request = build_cdp_request(
        model="deployment",
        task_prompt="Complete the task.",
        observation_bytes=snapshot,
        previous_action_result=None,
    )
    cdp = RunArchive.create(tmp_path, _manifest("CDP-ARCHIVE", "cdp_frame_traversal"))
    cdp.record_model_call(
        step=0,
        attempt=0,
        observation_kind="cdp_dom_snapshot",
        observation_bytes=snapshot,
        model_request_bytes=cdp_request,
        metadata=ModelCallMetadata(
            run_id="CDP-ARCHIVE",
            step=0,
            attempt=0,
            agent_class="cdp_frame_traversal",
            observation_kind="cdp_dom_snapshot",
            captured_at=captured,
            model_call_started_at=captured,
        ),
    )
    scan = scan_model_observations(tmp_path)
    assert {record.observation_kind for record in scan.records} == {"screenshot", "cdp_dom_snapshot"}

    # Per-run sweep scoring must use the same four-class observation-kind map
    # as archive-wide verification. These fixture archives intentionally omit
    # transcript/completion files, so isolate this assertion from finalization.
    monkeypatch.setattr("webagents.capture.archive.verify_run", lambda *_args, **_kwargs: [])
    vision_scan = scan_run_observations(vision.run_dir)
    cdp_scan = scan_run_observations(cdp.run_dir)
    assert [record.observation_kind for record in vision_scan.records] == ["screenshot"]
    assert [record.observation_kind for record in cdp_scan.records] == ["cdp_dom_snapshot"]
    assert vision_scan.records[0].reachability is not None
    assert vision_scan.records[0].reachability.visible is True
    assert not vision_scan.diagnostics
    assert not cdp_scan.diagnostics


class _SnapshotConnection:
    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        assert method == "DOMSnapshot.captureSnapshot"
        assert params == {"computedStyles": [], "includePaintOrder": True, "includeDOMRects": True}
        assert session_id == "session"
        strings = ["https://fixture/task", "Title", "frame", "#document", "", "SPAN", "MARKER", "id", "source"]
        return {
            "strings": strings,
            "documents": [
                {
                    "documentURL": 0,
                    "title": 1,
                    "frameId": 2,
                    "nodes": {
                        "parentIndex": [-1, 0, 1],
                        "nodeType": [9, 1, 3],
                        "nodeName": [3, 5, 4],
                        "nodeValue": [4, 4, 6],
                        "backendNodeId": [1, 2, 3],
                        "attributes": [[], [7, 8], []],
                        "isClickable": {"index": [1]},
                    },
                    "layout": {"nodeIndex": [1], "bounds": [[1, 2, 30, 40]], "styles": [[]], "text": [4]},
                }
            ],
        }


class _SnapshotCollector:
    connection = _SnapshotConnection()

    async def refresh(self) -> list[TargetHandle]:
        return [TargetHandle("target", "session", "page", "https://fixture/task")]

    def invalidate(self, target_id: str) -> None:
        pytest.fail(f"unexpected invalidation: {target_id}")


@pytest.mark.asyncio
async def test_cdp_snapshot_normalization_preserves_marker_layout_and_action_reference() -> None:
    envelope, observation, refs, complete, missing = await capture_cdp_snapshot(
        _SnapshotCollector(),  # type: ignore[arg-type]
        generation=4,
    )
    assert complete is True
    assert missing == []
    assert b"MARKER" in observation
    node = envelope["targets"][0]["documents"][0]["nodes"][1]
    assert node["attributes"] == {"id": "source"}
    assert node["bounds"] == [1, 2, 30, 40]
    assert node["clickable"] is True
    assert refs.resolve(node["ref"], generation=4).backend_node_id == 2


class _Keyboard:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def press(self, key: str) -> None:
        self.events.append(("press", key))

    async def insert_text(self, text: str) -> None:
        self.events.append(("text", text))


class _Mouse:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int]] = []

    async def click(self, x: int, y: int) -> None:
        self.events.append(("click", x, y))


class _Page:
    def __init__(self) -> None:
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()


@pytest.mark.asyncio
async def test_vision_actions_remain_coordinate_only() -> None:
    page = _Page()
    await execute_action(ClickAction(action="click", x=10, y=20), page=page)
    await execute_action(TypeAction(action="type", x=30, y=40, text="VALUE"), page=page)
    result = await execute_action(FinishAction(action="finish", answer="VALUE", success=True), page=page)
    assert page.mouse.events == [("click", 10, 20), ("click", 30, 40)]
    assert page.keyboard.events == [("press", "ControlOrMeta+A"), ("text", "VALUE")]
    assert result.is_done is True
