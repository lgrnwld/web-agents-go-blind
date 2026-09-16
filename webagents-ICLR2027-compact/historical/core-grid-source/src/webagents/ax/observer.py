"""Unmodified multi-target Accessibility.getFullAXTree capture."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from webagents.ax.refs import action_reference_catalog
from webagents.ax.targets import CDPError, TargetHandle
from webagents.capture.archive import canonical_json_bytes

PROTOCOL_METHOD = "Accessibility.getFullAXTree"
OBSERVATION_VERSION = "ax-cdp-full-action-refs-v2"


class _Connection(Protocol):
    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> Awaitable[dict[str, Any]]: ...


class _Collector(Protocol):
    @property
    def connection(self) -> _Connection: ...

    def refresh(self) -> Awaitable[list[TargetHandle]]: ...

    def invalidate(self, target_id: str) -> None: ...


def _origin(url: str) -> tuple[str, str, int | None] | None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return parts.scheme, parts.hostname, parts.port


def _frame_metadata(
    frame_tree: dict[str, Any],
    *,
    top_level_url: str | None = None,
    same_origin_only: bool = False,
) -> list[dict[str, str | None]]:
    frames: list[dict[str, str | None]] = []

    top_level_origin = _origin(top_level_url) if top_level_url else None

    def visit(node: dict[str, Any], parent: str | None = None, accessible: bool = True) -> None:
        frame = node.get("frame", {})
        frame_id = str(frame.get("id", ""))
        frame_url = str(frame.get("url", ""))
        current_accessible = accessible and (
            not same_origin_only or parent is None or _origin(frame_url) == top_level_origin
        )
        if not current_accessible:
            return
        frames.append(
            {
                "frame_id": frame_id,
                "parent_frame_id": parent,
                "url": frame_url,
            }
        )
        for child in node.get("childFrames", []) or []:
            visit(child, frame_id, current_accessible)

    tree = frame_tree.get("frameTree")
    if isinstance(tree, dict):
        visit(tree)
    return frames


async def capture_ax_tree(
    collector: _Collector,
    *,
    observation_policy: str = "unrestricted",
    top_level_url: str | None = None,
) -> tuple[dict[str, Any], bytes, bool, list[str]]:
    """Capture every applicable target; failures are explicit partial entries."""

    captured_at = datetime.now(UTC)
    targets: list[dict[str, Any]] = []
    missing: list[str] = []
    same_origin_only = observation_policy == "same_origin_only"
    for handle in await collector.refresh():
        if same_origin_only and (
            handle.target_type == "iframe"
            or (top_level_url is not None and _origin(handle.url) != _origin(top_level_url))
        ):
            continue
        entry: dict[str, Any] = {
            "target_id": handle.target_id,
            "session_id": handle.session_id,
            "target_type": handle.target_type,
            "url": handle.url,
            "captured_at": datetime.now(UTC).isoformat(),
            "protocol_method": PROTOCOL_METHOD,
        }
        if handle.session_id is None:
            entry.update(status="unattached", error=handle.error or "target session unavailable")
            missing.append(handle.target_id)
            targets.append(entry)
            continue
        try:
            await collector.connection.send("Accessibility.enable", session_id=handle.session_id)
            frame_tree = await collector.connection.send("Page.getFrameTree", session_id=handle.session_id)
            frames = _frame_metadata(
                frame_tree,
                top_level_url=top_level_url or handle.url,
                same_origin_only=same_origin_only,
            )
            root_frame_id = frames[0]["frame_id"] if frames else None
            root_params = {"frameId": root_frame_id} if root_frame_id else None
            protocol_result = await collector.connection.send(
                PROTOCOL_METHOD,
                root_params,
                session_id=handle.session_id,
            )
            frame_protocol_results: list[dict[str, Any]] = []
            for frame in frames[1:]:
                frame_id = frame["frame_id"]
                frame_entry: dict[str, Any] = {**frame, "protocol_method": PROTOCOL_METHOD}
                try:
                    frame_entry.update(
                        status="captured",
                        protocol_result=await collector.connection.send(
                            PROTOCOL_METHOD,
                            {"frameId": frame_id},
                            session_id=handle.session_id,
                        ),
                    )
                except CDPError as exc:
                    frame_entry.update(status="error", error=f"{type(exc).__name__}: {exc}")
                    missing.append(f"{handle.target_id}:{frame_id}")
                frame_protocol_results.append(frame_entry)
            entry.update(
                status="captured",
                frames=frames,
                protocol_result=protocol_result,
                frame_protocol_results=frame_protocol_results,
            )
        except CDPError as exc:
            entry.update(status="error", error=f"{type(exc).__name__}: {exc}")
            missing.append(handle.target_id)
            collector.invalidate(handle.target_id)
        targets.append(entry)

    if not targets:
        missing.append("no_applicable_targets")
    envelope = {
        "capture_version": OBSERVATION_VERSION,
        "captured_at": captured_at.isoformat(),
        "protocol_method": PROTOCOL_METHOD,
        "observation_policy": observation_policy,
        "reference_scheme": (
            "use the exact ax-N values in action_references; entries map deterministically to each target's "
            "protocol_result nodes followed by its frame_protocol_results nodes"
        ),
        "completeness": "partial" if missing else "complete",
        "targets": targets,
    }
    envelope["action_references"] = action_reference_catalog(envelope)
    return envelope, canonical_json_bytes(envelope), not missing, missing
