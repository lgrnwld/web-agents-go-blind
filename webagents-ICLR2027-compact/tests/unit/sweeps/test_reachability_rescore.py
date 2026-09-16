from __future__ import annotations

from datetime import UTC, datetime

from webagents.schemas import (
    ArtifactReference,
    FrameworkIdentity,
    ObservationRecord,
    ObservationReference,
    RunInput,
    RunStep,
    RunTranscript,
)
from webagents.sweeps.reachability import marker_reachable_on_task_page

SHA = "a" * 64
TASK_URL = "http://127.0.0.1:1234/fixture/copy"
MARKER = b"CONTROL_COPY_VALUE_R2"


def _step(step: int, url: str) -> RunStep:
    return RunStep(
        step=step,
        timestamp=datetime.now(UTC),
        url=url,
        observation=ObservationReference(
            artifact_path=f"observations/step-{step:03d}/observation.txt",
            sha256=SHA,
            byte_length=1,
            kind="serialized_dom",
        ),
        model_request=ArtifactReference(
            artifact_path=f"observations/step-{step:03d}/model-request.json",
            sha256=SHA,
            byte_length=1,
        ),
    )


def _transcript(*urls: str) -> RunTranscript:
    return RunTranscript(
        run_id="test-run",
        agent_class="dom_extraction",
        framework=FrameworkIdentity(name="browser-use", version="test", commit="test"),
        input=RunInput(page_url=TASK_URL, task_prompt="copy", model="foundry/test"),
        started_at=datetime.now(UTC),
        status="failure",
        steps=[_step(index, url) for index, url in enumerate(urls)],
    )


def _record(step: int, content: bytes) -> ObservationRecord:
    return ObservationRecord(
        run_id="test-run",
        agent_class="dom_extraction",
        step=step,
        attempt=0,
        observation_kind="serialized_dom",
        observation_bytes=content,
        sha256=SHA,
        capture_completeness="complete",
        transform={"name": "none", "version": None},
    )


def test_marker_on_initial_task_page_counts() -> None:
    transcript = _transcript(TASK_URL, f"{TASK_URL}/submit")
    records = [_record(0, MARKER), _record(1, MARKER)]
    assert marker_reachable_on_task_page(records, transcript, MARKER) is True


def test_marker_only_on_terminal_result_does_not_count() -> None:
    transcript = _transcript(TASK_URL, f"{TASK_URL}/submit")
    records = [_record(0, b"Destination Submit"), _record(1, MARKER)]
    assert marker_reachable_on_task_page(records, transcript, MARKER) is False


def test_terminal_url_in_dom_observation_handles_stale_transcript_url() -> None:
    transcript = _transcript(TASK_URL)
    terminal_observation = f"Current URL: {TASK_URL}/submit\n".encode() + MARKER
    assert marker_reachable_on_task_page([_record(0, terminal_observation)], transcript, MARKER) is False


def test_marker_on_an_unrelated_page_does_not_count() -> None:
    transcript = _transcript("https://example.invalid/")
    assert marker_reachable_on_task_page([_record(0, MARKER)], transcript, MARKER) is False
