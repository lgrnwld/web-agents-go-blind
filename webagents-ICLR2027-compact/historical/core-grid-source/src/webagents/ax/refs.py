"""Deterministic, step-local AX reference assignment."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


class ReferenceError(ValueError):
    code = "invalid_ref"


class StaleReferenceError(ReferenceError):
    code = "stale_ref"


class UnactionableReferenceError(ReferenceError):
    code = "unactionable_ref"


@dataclass(frozen=True, slots=True)
class AXReference:
    ref: str
    generation: int
    target_id: str
    session_id: str
    backend_dom_node_id: int | None


def iter_ax_nodes(
    envelope: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any], str, int, dict[str, Any]]]:
    """Yield deterministic references without changing Chromium's protocol results."""

    ordinal = 0
    for target_index, target in enumerate(envelope.get("targets", [])):
        results: list[tuple[str, dict[str, Any]]] = [("root", target.get("protocol_result", {}))]
        results.extend(
            (f"frame-{frame_index}", frame.get("protocol_result", {}))
            for frame_index, frame in enumerate(target.get("frame_protocol_results", []))
        )
        for scope, protocol_result in results:
            for node_index, node in enumerate(protocol_result.get("nodes", [])):
                yield f"ax-{ordinal}", target, f"target-{target_index}:{scope}", node_index, node
                ordinal += 1


def _computed_value(node: dict[str, Any], field: str) -> Any:
    value = node.get(field)
    return value.get("value") if isinstance(value, dict) else None


def action_reference_catalog(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe every step-local action reference using AX-derived fields only."""

    catalog: list[dict[str, Any]] = []
    for ref, target, scope, node_index, node in iter_ax_nodes(envelope):
        backend_id = node.get("backendDOMNodeId")
        catalog.append(
            {
                "ref": ref,
                "target_id": str(target.get("target_id", "")),
                "scope": scope,
                "node_index": node_index,
                "node_id": node.get("nodeId"),
                "role": _computed_value(node, "role"),
                "name": _computed_value(node, "name"),
                "value": _computed_value(node, "value"),
                "actionable": isinstance(backend_id, int),
            }
        )
    return catalog


class ReferenceMap:
    def __init__(self, generation: int, references: dict[str, AXReference]) -> None:
        self.generation = generation
        self._references = references
        self._valid = True

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any], *, generation: int) -> ReferenceMap:
        references: dict[str, AXReference] = {}
        for ref, target, _, _, node in iter_ax_nodes(envelope):
            session_id = target.get("session_id")
            if isinstance(session_id, str):
                backend_id = node.get("backendDOMNodeId")
                references[ref] = AXReference(
                    ref=ref,
                    generation=generation,
                    target_id=str(target.get("target_id")),
                    session_id=session_id,
                    backend_dom_node_id=backend_id if isinstance(backend_id, int) else None,
                )
        return cls(generation, references)

    def resolve(self, ref: str, *, generation: int) -> AXReference:
        if not self._valid or generation != self.generation:
            raise StaleReferenceError(f"reference {ref!r} belongs to an expired AX snapshot")
        reference = self._references.get(ref)
        if reference is None:
            raise ReferenceError(f"unknown AX reference: {ref}")
        if reference.backend_dom_node_id is None:
            raise UnactionableReferenceError(f"AX node {ref} has no backendDOMNodeId")
        return reference

    def invalidate(self) -> None:
        self._valid = False
