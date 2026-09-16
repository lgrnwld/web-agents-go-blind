from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from webagents.ax.observer import OBSERVATION_VERSION, PROTOCOL_METHOD, capture_ax_tree
from webagents.ax.refs import ReferenceMap, StaleReferenceError, UnactionableReferenceError
from webagents.ax.targets import CDPError, TargetHandle
from webagents.capture.archive import canonical_json_bytes


class FakeConnection:
    def __init__(self, protocol_result: dict[str, Any], *, fail_tree: bool = False) -> None:
        self.protocol_result = protocol_result
        self.fail_tree = fail_tree
        self.calls: list[tuple[str, str | None]] = []

    async def send(
        self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None
    ) -> dict[str, Any]:
        del params
        self.calls.append((method, session_id))
        if method == "Accessibility.enable":
            return {}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "FRAME-A", "url": "https://fixture.invalid"}}}
        if self.fail_tree:
            raise CDPError("detached")
        return self.protocol_result


class FakeCollector:
    def __init__(self, connection: FakeConnection, handles: list[TargetHandle]) -> None:
        self.connection = connection
        self._handles = handles

    async def refresh(self) -> list[TargetHandle]:
        return self._handles

    def invalidate(self, target_id: str) -> None:
        assert target_id in {handle.target_id for handle in self._handles}


@pytest.fixture
def protocol_result() -> dict[str, Any]:
    path = Path(__file__).parents[2] / "fixtures" / "ax-tree-protocol-1.61.0.json"
    return json.loads(path.read_bytes())


@pytest.mark.asyncio
async def test_observer_preserves_unmodified_protocol_result(protocol_result: dict[str, Any]) -> None:
    connection = FakeConnection(protocol_result)
    collector = FakeCollector(
        connection,
        [TargetHandle("target-a", "session-a", "page", "https://fixture.invalid")],
    )
    envelope, encoded, complete, missing = await capture_ax_tree(collector)
    assert envelope["targets"][0]["protocol_result"] == protocol_result
    assert envelope["capture_version"] == OBSERVATION_VERSION
    assert len(envelope["action_references"]) == len(protocol_result["nodes"])
    assert [item["ref"] for item in envelope["action_references"]] == [
        f"ax-{index}" for index in range(len(protocol_result["nodes"]))
    ]
    assert encoded == canonical_json_bytes(envelope)
    assert complete is True
    assert missing == []
    assert connection.calls == [
        ("Accessibility.enable", "session-a"),
        ("Page.getFrameTree", "session-a"),
        (PROTOCOL_METHOD, "session-a"),
    ]


@pytest.mark.asyncio
async def test_detached_target_is_explicitly_partial(protocol_result: dict[str, Any]) -> None:
    connection = FakeConnection(protocol_result, fail_tree=True)
    collector = FakeCollector(
        connection,
        [TargetHandle("oopif-a", "session-b", "iframe", "https://cross.invalid")],
    )
    envelope, _, complete, missing = await capture_ax_tree(collector)
    assert complete is False
    assert missing == ["oopif-a"]
    assert envelope["targets"][0]["status"] == "error"
    assert "protocol_result" not in envelope["targets"][0]


def test_reference_assignment_is_deterministic_and_step_local(protocol_result: dict[str, Any]) -> None:
    envelope = {
        "targets": [
            {
                "target_id": "target-a",
                "session_id": "session-a",
                "protocol_result": protocol_result,
            }
        ]
    }
    first = ReferenceMap.from_envelope(envelope, generation=4)
    second = ReferenceMap.from_envelope(envelope, generation=4)
    assert first.resolve("ax-1", generation=4) == second.resolve("ax-1", generation=4)
    with pytest.raises(UnactionableReferenceError):
        first.resolve("ax-2", generation=4)
    first.invalidate()
    with pytest.raises(StaleReferenceError):
        first.resolve("ax-1", generation=4)


@pytest.mark.asyncio
async def test_action_catalog_matches_executor_references(protocol_result: dict[str, Any]) -> None:
    connection = FakeConnection(protocol_result)
    collector = FakeCollector(
        connection,
        [TargetHandle("target-a", "session-a", "page", "https://fixture.invalid")],
    )
    envelope, _, _, _ = await capture_ax_tree(collector)
    references = ReferenceMap.from_envelope(envelope, generation=7)

    for item in envelope["action_references"]:
        if not item["actionable"]:
            continue
        resolved = references.resolve(item["ref"], generation=7)
        assert resolved.target_id == item["target_id"]
        assert resolved.backend_dom_node_id is not None
