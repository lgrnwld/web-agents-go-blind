"""Protocol-native DOMSnapshot capture across page and OOPIF targets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from webagents.ax.targets import CDPError, TargetHandle
from webagents.capture.archive import canonical_json_bytes

PROTOCOL_METHOD = "DOMSnapshot.captureSnapshot"
OBSERVATION_VERSION = "cdp-dom-snapshot-normalized-v1"


class _Connection(Protocol):
    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]: ...


class _Collector(Protocol):
    @property
    def connection(self) -> _Connection: ...

    async def refresh(self) -> list[TargetHandle]: ...

    def invalidate(self, target_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class CDPReference:
    ref: str
    generation: int
    target_id: str
    session_id: str
    backend_node_id: int


class CDPReferenceMap:
    def __init__(self, generation: int, references: dict[str, CDPReference]) -> None:
        self.generation = generation
        self._references = references
        self._valid = True

    def resolve(self, ref: str, *, generation: int) -> CDPReference:
        if not self._valid or generation != self.generation:
            raise ValueError(f"reference {ref!r} belongs to an expired CDP snapshot")
        try:
            return self._references[ref]
        except KeyError as exc:
            raise ValueError(f"unknown CDP reference: {ref}") from exc

    def invalidate(self) -> None:
        self._valid = False


def _origin(url: str) -> tuple[str, str, int | None] | None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return parts.scheme, parts.hostname, parts.port


def _string(strings: list[Any], index: Any) -> str:
    if isinstance(index, int) and 0 <= index < len(strings):
        value = strings[index]
        return value if isinstance(value, str) else str(value)
    return ""


def _rare_values(value: Any) -> dict[int, Any]:
    if not isinstance(value, dict):
        return {}
    indices = value.get("index", [])
    values = value.get("value", [])
    if not isinstance(indices, list):
        return {}
    if not isinstance(values, list):
        return {index: True for index in indices if isinstance(index, int)}
    return {
        index: values[position]
        for position, index in enumerate(indices)
        if isinstance(index, int) and position < len(values)
    }


def _rare_booleans(value: Any) -> set[int]:
    if not isinstance(value, dict) or not isinstance(value.get("index"), list):
        return set()
    return {index for index in value["index"] if isinstance(index, int)}


def _normalize_snapshot(
    snapshot: dict[str, Any],
    *,
    target: TargetHandle,
    generation: int,
    ordinal_start: int,
) -> tuple[list[dict[str, Any]], dict[str, CDPReference], int]:
    strings = snapshot.get("strings", [])
    if not isinstance(strings, list):
        raise CDPError("DOMSnapshot returned no strings table")
    documents = snapshot.get("documents", [])
    if not isinstance(documents, list):
        raise CDPError("DOMSnapshot returned no document list")
    normalized_documents: list[dict[str, Any]] = []
    references: dict[str, CDPReference] = {}
    ordinal = ordinal_start
    for document_index, document in enumerate(documents):
        if not isinstance(document, dict):
            continue
        nodes = document.get("nodes", {})
        layout = document.get("layout", {})
        if not isinstance(nodes, dict) or not isinstance(layout, dict):
            continue
        backend_ids = nodes.get("backendNodeId", [])
        names = nodes.get("nodeName", [])
        values = nodes.get("nodeValue", [])
        types = nodes.get("nodeType", [])
        parents = nodes.get("parentIndex", [])
        attributes = nodes.get("attributes", [])
        if not isinstance(backend_ids, list):
            continue
        layout_nodes = layout.get("nodeIndex", [])
        layout_bounds = layout.get("bounds", [])
        bounds_by_node = {
            node_index: layout_bounds[position]
            for position, node_index in enumerate(layout_nodes)
            if isinstance(node_index, int)
            and isinstance(layout_bounds, list)
            and position < len(layout_bounds)
        } if isinstance(layout_nodes, list) else {}
        rare_string_fields = {
            key: _rare_values(nodes.get(key))
            for key in ("shadowRootType", "textValue", "inputValue", "pseudoType")
        }
        clickable = _rare_booleans(nodes.get("isClickable"))
        checked = _rare_booleans(nodes.get("inputChecked"))
        selected = _rare_booleans(nodes.get("optionSelected"))
        normalized_nodes: list[dict[str, Any]] = []
        for node_index, backend_id in enumerate(backend_ids):
            name = _string(strings, names[node_index] if node_index < len(names) else None)
            node_value = _string(strings, values[node_index] if node_index < len(values) else None)
            node_type = types[node_index] if node_index < len(types) else None
            parent = parents[node_index] if node_index < len(parents) else None
            attribute_indices = attributes[node_index] if node_index < len(attributes) else []
            decoded_attributes: dict[str, str] = {}
            if isinstance(attribute_indices, list):
                for offset in range(0, len(attribute_indices) - 1, 2):
                    decoded_attributes[_string(strings, attribute_indices[offset])] = _string(
                        strings, attribute_indices[offset + 1]
                    )
            entry: dict[str, Any] = {
                "node_index": node_index,
                "parent_index": parent,
                "node_type": node_type,
                "name": name,
            }
            if node_value:
                entry["value"] = node_value
            if decoded_attributes:
                entry["attributes"] = decoded_attributes
            for key, values_by_index in rare_string_fields.items():
                if node_index in values_by_index:
                    entry[key] = _string(strings, values_by_index[node_index])
            if node_index in clickable:
                entry["clickable"] = True
            if node_index in checked:
                entry["checked"] = True
            if node_index in selected:
                entry["selected"] = True
            bounds = bounds_by_node.get(node_index)
            if isinstance(bounds, list) and len(bounds) == 4:
                entry["bounds"] = bounds
            if isinstance(backend_id, int) and target.session_id is not None and node_type == 1:
                ref = f"cdp-{ordinal}"
                ordinal += 1
                entry["ref"] = ref
                references[ref] = CDPReference(
                    ref=ref,
                    generation=generation,
                    target_id=target.target_id,
                    session_id=target.session_id,
                    backend_node_id=backend_id,
                )
            normalized_nodes.append(entry)
        normalized_documents.append(
            {
                "document_index": document_index,
                "url": _string(strings, document.get("documentURL")),
                "title": _string(strings, document.get("title")),
                "frame_id": _string(strings, document.get("frameId")),
                "nodes": normalized_nodes,
            }
        )
    return normalized_documents, references, ordinal


async def capture_cdp_snapshot(
    collector: _Collector,
    *,
    generation: int,
    observation_policy: str = "unrestricted",
    top_level_url: str | None = None,
) -> tuple[dict[str, Any], bytes, CDPReferenceMap, bool, list[str]]:
    """Capture and normalize the protocol snapshot for every applicable target."""

    same_origin_only = observation_policy == "same_origin_only"
    missing: list[str] = []
    target_entries: list[dict[str, Any]] = []
    references: dict[str, CDPReference] = {}
    ordinal = 0
    for handle in await collector.refresh():
        if same_origin_only and (
            handle.target_type == "iframe"
            or (top_level_url is not None and _origin(handle.url) != _origin(top_level_url))
        ):
            continue
        entry: dict[str, Any] = {
            "target_id": handle.target_id,
            "target_type": handle.target_type,
            "url": handle.url,
            "protocol_method": PROTOCOL_METHOD,
        }
        if handle.session_id is None:
            entry.update(status="unattached", error=handle.error or "target session unavailable")
            missing.append(handle.target_id)
            target_entries.append(entry)
            continue
        try:
            snapshot = await collector.connection.send(
                PROTOCOL_METHOD,
                {"computedStyles": [], "includePaintOrder": True, "includeDOMRects": True},
                session_id=handle.session_id,
            )
            documents, target_refs, ordinal = _normalize_snapshot(
                snapshot,
                target=handle,
                generation=generation,
                ordinal_start=ordinal,
            )
            references.update(target_refs)
            entry.update(status="captured", documents=documents)
        except CDPError as exc:
            entry.update(status="error", error=f"{type(exc).__name__}: {exc}")
            missing.append(handle.target_id)
            collector.invalidate(handle.target_id)
        target_entries.append(entry)
    if not target_entries:
        missing.append("no_applicable_targets")
    envelope = {
        "capture_version": OBSERVATION_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "protocol_method": PROTOCOL_METHOD,
        "observation_policy": observation_policy,
        "reference_scheme": "cdp-N is a step-local element reference backed by DOM.BackendNodeId",
        "completeness": "partial" if missing else "complete",
        "targets": target_entries,
    }
    return envelope, canonical_json_bytes(envelope), CDPReferenceMap(generation, references), not missing, missing
