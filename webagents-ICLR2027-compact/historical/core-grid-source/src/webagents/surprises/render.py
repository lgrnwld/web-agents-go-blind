"""Deterministic human-readable and tabular views of the canonical surprise log."""

from __future__ import annotations

import csv
import io
from datetime import UTC
from pathlib import Path

from webagents.io import atomic_write
from webagents.surprises.log import SurpriseLogError, _read_events_strict
from webagents.surprises.schemas import FrameworkSurpriseEvent


def _axis_text(event: FrameworkSurpriseEvent) -> str:
    return ", ".join(f"{item.axis}:{item.level}" for item in event.condition.axes) or event.condition.id


def _framework_text(event: FrameworkSurpriseEvent) -> str:
    browser = ""
    if event.browser.name:
        browser = f"; {event.browser.name} {event.browser.version or 'unknown'}"
        if event.browser.revision:
            browser += f" (revision {event.browser.revision})"
    return f"{event.framework.name} {event.framework.version}@{event.framework.commit}{browser}"


def _root_and_latest(
    events: list[FrameworkSurpriseEvent],
) -> list[tuple[FrameworkSurpriseEvent, FrameworkSurpriseEvent]]:
    roots = {event.event_id: event for event in events if event.event_type == "candidate"}
    latest = dict(roots)
    for event in events:
        target = event.updates
        if target in roots:
            latest[target] = event
        elif target is not None:
            root = next(
                (
                    root_id
                    for root_id, root in roots.items()
                    if root.event_id == target or latest[root_id].event_id == target
                ),
                None,
            )
            if root is not None:
                latest[root] = event
    return sorted(
        ((root, latest[root_id]) for root_id, root in roots.items()),
        key=lambda pair: (pair[0].observed_at, pair[0].event_id),
    )


def _timeline(events: list[FrameworkSurpriseEvent]) -> bytes:
    lines = [
        "# Framework-surprise timeline",
        "",
        "Canonical source: `events.jsonl`. Events are append-only; later entries update rather than erase history.",
        "",
        "| Observed (UTC) | Event | Type/status | Class | Condition | Framework identity | Summary | Updates |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for event in sorted(events, key=lambda item: (item.observed_at, item.logged_at, item.event_id)):
        lines.append(
            f"| {event.observed_at.astimezone(UTC).isoformat().replace('+00:00', 'Z')} | {event.event_id} | "
            f"{event.event_type}/{event.status} | {event.agent_class} | {_axis_text(event)} | "
            f"{_framework_text(event)} | {event.summary} | {event.updates or '—'} |"
        )
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _limitations(events: list[FrameworkSurpriseEvent]) -> bytes:
    unresolved_or_material = {
        "candidate",
        "awaiting_followup",
        "confirmed",
        "model_specific",
        "implementation_change",
        "evidence_issue",
        "fixture_issue",
    }
    lines = [
        "# Limitations evidence from framework surprises",
        "",
        "This generated table retains confirmed and unresolved version-contingent observations.",
        "",
        "| First observed | Candidate | Current status | Class/condition | Expected | Observed | "
        "Framework/browser | Paper disposition | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for root, latest in _root_and_latest(events):
        if latest.status not in unresolved_or_material or latest.paper_disposition == "not_applicable":
            continue
        evidence = ", ".join(f"`{item.artifact_path}` ({item.sha256[:12]})" for item in root.evidence)
        lines.append(
            f"| {root.observed_date.isoformat()} | {root.event_id} | {latest.status} | "
            f"{root.agent_class}/{root.condition.id} | {root.expected_behavior} | "
            f"{latest.observed_behavior} | {_framework_text(latest)} | {latest.paper_disposition} | {evidence} |"
        )
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _framework_matrix(events: list[FrameworkSurpriseEvent]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "candidate_event_id",
            "observed_date_utc",
            "latest_status",
            "agent_class",
            "condition_id",
            "axes",
            "framework_name",
            "framework_version",
            "framework_commit",
            "browser_version",
            "browser_revision",
            "playwright_version",
            "prompt_version",
            "source_sha256",
            "provider_qualified_model",
            "observed_present",
            "paper_disposition",
        ]
    )
    for root, latest in _root_and_latest(events):
        writer.writerow(
            [
                root.event_id,
                root.observed_date.isoformat(),
                latest.status,
                root.agent_class,
                root.condition.id,
                _axis_text(root),
                latest.framework.name,
                latest.framework.version,
                latest.framework.commit,
                latest.browser.version or "",
                latest.browser.revision or "",
                latest.browser.playwright_version or "",
                latest.prompt_version,
                latest.source.source_sha256,
                latest.provider_qualified_model,
                str(latest.marker_assertion.observed_present).lower(),
                latest.paper_disposition,
            ]
        )
    return output.getvalue().encode("utf-8")


def render_surprise_views(log_path: Path, *, output_dir: Path) -> None:
    """Replace generated views deterministically without modifying the canonical stream."""

    try:
        events = _read_events_strict(log_path)
    except SurpriseLogError:
        raise
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(output_dir / "timeline.md", _timeline(events), replace=True)
    atomic_write(output_dir / "limitations-table.md", _limitations(events), replace=True)
    atomic_write(output_dir / "framework-matrix.csv", _framework_matrix(events), replace=True)
