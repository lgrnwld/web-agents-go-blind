from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from webagents.schemas import RunRequest, RunTranscript


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page_url", "file:///tmp/page.html"),
        ("page_url", "javascript:alert(1)"),
        ("page_url", "http://user:password@localhost:3000"),
        ("task_prompt", "  \n"),
        ("model", "gpt-5.2"),
        ("model", "unknown/model"),
        ("max_steps", 0),
        ("run_id", "../escape"),
    ],
)
def test_run_request_rejects_invalid_input(field: str, value: object) -> None:
    values: dict[str, object] = {
        "page_url": "http://localhost:3000/case",
        "task_prompt": "Read X",
        "model": "openrouter/openai/gpt-5.2",
        "max_steps": 30,
    }
    values[field] = value
    with pytest.raises(ValidationError):
        RunRequest.model_validate(values)


def test_transcript_fixture_round_trips_and_rejects_unknown_fields() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "transcript-1.0.json"
    raw = fixture.read_text(encoding="utf-8")
    transcript = RunTranscript.model_validate_json(raw)
    assert RunTranscript.model_validate_json(transcript.model_dump_json()) == transcript

    incompatible = json.loads(raw)
    incompatible["future_field"] = True
    with pytest.raises(ValidationError):
        RunTranscript.model_validate(incompatible)
