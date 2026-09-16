from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from webagents.ax import runner
from webagents.ax.targets import CDPConnection, TargetCollector
from webagents.capture.archive import RunArchive, canonical_json_bytes
from webagents.providers import ProviderCallResult
from webagents.schemas import RunRequest


class FakePage:
    url = "https://fixture.invalid/task"

    async def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds == 0


class FinishProvider:
    provider = "openrouter"
    model = "fixture/model"

    def __init__(self, archive: RunArchive, observation: bytes) -> None:
        self.archive = archive
        self.observation = observation
        self.calls = 0

    async def invoke(self, request_bytes: bytes) -> ProviderCallResult:
        self.calls += 1
        step_dir = self.archive.run_dir / "observations" / "step-000"
        assert (step_dir / "metadata.json").is_file()
        assert (step_dir / "observation.json").read_bytes() == self.observation
        assert (step_dir / "model-request.json").read_bytes() == request_bytes
        assert json.loads(request_bytes)["messages"][1]["content"] == self.observation.decode()
        return ProviderCallResult(
            text='{"action":"finish","answer":"AX label","success":true}',
            input_tokens=10,
            output_tokens=5,
        )


@pytest.mark.asyncio
async def test_every_provider_call_follows_exact_archived_ax_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = RunRequest(
        page_url="https://fixture.invalid/task",
        task_prompt="Read label",
        model="openrouter/fixture/model",
        max_steps=2,
    )
    transcript = runner._base_transcript(request, "AX-LOOP-TEST", datetime.now(UTC), "1228")
    archive = RunArchive.create(tmp_path, runner._make_manifest(transcript, "1228"))
    observation_value = {
        "capture_version": "ax-cdp-full-v1",
        "completeness": "complete",
        "targets": [],
    }
    observation = canonical_json_bytes(observation_value)

    async def fake_capture(collector: object) -> tuple[dict[str, Any], bytes, bool, list[str]]:
        del collector
        return observation_value, observation, True, []

    monkeypatch.setattr(runner, "capture_ax_tree", fake_capture)
    provider = FinishProvider(archive, observation)
    await runner._agent_loop(
        page=FakePage(),
        connection=cast(CDPConnection, object()),  # finish never enters CDP action execution
        collector=cast(TargetCollector, object()),
        provider=provider,
        request=request,
        archive=archive,
        transcript=transcript,
        settle_milliseconds=0,
    )
    assert provider.calls == 1
    assert transcript.status == "success"
    assert transcript.agent_class == "accessibility_tree"
    assert transcript.steps[0].observation.kind == "ax_tree"
    assert transcript.steps[0].usage.input_tokens == 10
