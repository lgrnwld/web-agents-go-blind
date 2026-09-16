"""Reachability scoring restricted to the original benchmark task page."""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable

from webagents.schemas import ObservationRecord, RunTranscript

REACHABILITY_SCORING_POLICY = "initial-task-page-marker-v2"


def _normalized_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _terminal_result_url(page_url: str) -> str:
    return f"{page_url.rstrip('/')}/submit"


def marker_reachable_on_task_page(
    records: Iterable[ObservationRecord],
    transcript: RunTranscript,
    marker: bytes,
) -> bool:
    """Return whether ``marker`` appeared before navigation to task feedback.

    Copy-task fixtures answer a form POST at ``<task-url>/submit``. Historical
    fixtures repeated the source and marker on that feedback page, so scanning
    every observation could turn a failed blind submission into a reachability
    success. Both the transcript URL and the exact observation bytes are checked
    because Browser Use 0.13.7 can retain the pre-action URL on the post-action
    observation step.
    """

    if not marker:
        raise ValueError("reachability marker must not be empty")
    steps = {(step.step, step.attempt): step for step in transcript.steps}
    normalized_task = _normalized_url(transcript.input.page_url)
    terminal_url = _terminal_result_url(transcript.input.page_url)
    normalized_terminal = _normalized_url(terminal_url)
    terminal_bytes = terminal_url.encode("utf-8")
    for record in records:
        step = steps.get((record.step, record.attempt))
        if step is None:
            raise ValueError(
                f"observation ({record.step}, {record.attempt}) is absent from transcript {transcript.run_id}"
            )
        on_task_page = _normalized_url(step.url) == normalized_task
        on_terminal_result = (
            _normalized_url(step.url) == normalized_terminal or terminal_bytes in record.observation_bytes
        )
        if on_task_page and not on_terminal_result and marker in record.observation_bytes:
            return True
    return False
