from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from webagents.capture.archive import RunArchive, canonical_json_bytes, scan_model_observations
from webagents.dom.browser_use_adapter import (
    InstrumentedChatModel,
    ObservationCaptureError,
    build_provider_request,
    extract_serialized_dom,
)
from webagents.schemas import (
    FrameworkIdentity,
    ImplementationIdentity,
    RunInput,
    RunManifest,
    RuntimeIdentity,
    RunTranscript,
)

RUN_ID = "01KZPD5RF0AEVYVGP4QJK7Y89Z"


class Message:
    def __init__(self, text: str) -> None:
        self.text = text


def _archive_and_transcript(tmp_path: Path) -> tuple[RunArchive, RunTranscript]:
    now = datetime.now(UTC)
    input_data = RunInput(
        page_url="http://localhost:3000", task_prompt="Read marker", model="openrouter/openai/gpt-5.2"
    )
    manifest = RunManifest(
        run_id=RUN_ID,
        agent_class="dom_extraction",
        input=input_data,
        runtime=RuntimeIdentity(started_at=now, host_platform="test", python="3.12"),
        implementation=ImplementationIdentity(
            repository_commit=None,
            source_state="unversioned",
            framework_name="browser-use",
            framework_version="0.13.7",
            framework_commit="f0aa3a8bb03779c71a5aa262d389e3bfe6b77cdc",
            prompt_version="test",
        ),
    )
    transcript = RunTranscript(
        run_id=RUN_ID,
        agent_class="dom_extraction",
        framework=FrameworkIdentity(version="0.13.7", commit="f0aa3a8bb03779c71a5aa262d389e3bfe6b77cdc"),
        input=input_data,
        started_at=now,
        status="failure",
    )
    return RunArchive.create(tmp_path, manifest), transcript


def test_extract_serialized_dom_preserves_exact_whitespace_and_unicode() -> None:
    message = Message("prefix\n<browser_state>\nline 1\r\n  café &lt;x&gt;\n</browser_state>\nsuffix")
    assert extract_serialized_dom([message]) == "line 1\r\n  café &lt;x&gt;".encode()


def test_foundry_request_uses_openai_v1_chat_shape() -> None:
    pytest.importorskip("browser_use")
    from browser_use.llm.messages import UserMessage

    model = SimpleNamespace(
        _webagents_provider="foundry",
        provider="openai",
        model="gpt-5-mini",
        temperature=None,
        frequency_penalty=None,
        max_completion_tokens=128,
        top_p=None,
        seed=None,
        service_tier=None,
        reasoning_models=[],
    )
    request = json.loads(build_provider_request(model, [UserMessage(content="hello")], None))
    assert request == {
        "max_completion_tokens": 128,
        "messages": [{"content": "hello", "role": "user"}],
        "model": "gpt-5-mini",
    }


def test_kimi_request_puts_schema_in_prompt_without_response_format() -> None:
    pytest.importorskip("browser_use")
    from browser_use.llm.messages import SystemMessage, UserMessage

    class Output(BaseModel):
        action: str

    model = SimpleNamespace(
        _webagents_provider="foundry",
        provider="openai",
        model="Kimi-K2.6",
        temperature=0.2,
        frequency_penalty=0.3,
        max_completion_tokens=4096,
        top_p=None,
        seed=None,
        service_tier=None,
        reasoning_models=[],
        add_schema_to_system_prompt=True,
        dont_force_structured_output=True,
        remove_min_items_from_schema=False,
        remove_defaults_from_schema=False,
    )
    request = json.loads(
        build_provider_request(
            model,
            [SystemMessage(content="Return JSON."), UserMessage(content="hello")],
            Output,
        )
    )
    assert "response_format" not in request
    assert "<json_schema>" in request["messages"][0]["content"]
    assert '"action"' in request["messages"][0]["content"]


@pytest.mark.asyncio
async def test_instrumentation_commits_once_before_each_actual_call(tmp_path: Path) -> None:
    browser_use = pytest.importorskip("browser_use")
    del browser_use
    from browser_use.llm.messages import UserMessage
    from browser_use.llm.views import ChatInvokeUsage

    class FakeOpenRouter:
        provider = "openrouter"
        model = "openai/gpt-5.2"
        name = model
        temperature = None
        top_p = None
        seed = None
        extra_body = None

        def __init__(self) -> None:
            self.calls = 0
            self.received: list[dict[str, object]] = []

            owner = self

            class Completions:
                async def create(self, **body: object) -> object:
                    owner.calls += 1
                    owner.received.append(body)
                    return SimpleNamespace(
                        id=f"response-{owner.calls}",
                        created=1,
                        model=owner.model,
                        system_fingerprint=None,
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(content="ok"),
                                finish_reason="stop",
                            )
                        ],
                    )

            self.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

        def get_client(self) -> object:
            return self.client

        def _get_usage(self, response: object) -> ChatInvokeUsage:
            del response
            return ChatInvokeUsage(
                prompt_tokens=10,
                prompt_cached_tokens=None,
                prompt_cache_creation_tokens=None,
                prompt_image_tokens=None,
                completion_tokens=2,
                total_tokens=12,
            )

    archive, transcript = _archive_and_transcript(tmp_path)
    delegate = FakeOpenRouter()
    instrumented = InstrumentedChatModel(delegate, archive, transcript)
    assert instrumented.model_name == "openai/gpt-5.2"
    messages = [UserMessage(content="<browser_state>\nmarker\n</browser_state>")]
    await instrumented.ainvoke(messages)
    await instrumented.ainvoke(messages)

    assert delegate.calls == 2
    assert [(step.step, step.attempt) for step in transcript.steps] == [(0, 0), (0, 1)]
    assert len(list((archive.run_dir / "observations").glob("step-*/metadata.json"))) == 2
    request = json.loads((archive.run_dir / "observations/step-000/model-request.json").read_bytes())
    assert request["messages"][0]["content"] == "<browser_state>\nmarker\n</browser_state>"
    assert (
        canonical_json_bytes(delegate.received[0])
        == (archive.run_dir / "observations/step-000/model-request.json").read_bytes()
    )
    assert transcript.steps[0].model_response is not None
    assert transcript.steps[0].model_response.text == "ok"
    assert transcript.steps[0].usage.input_tokens == 10
    scorer_records = scan_model_observations(tmp_path).records
    assert len(scorer_records) == 2
    assert all(record.observation_bytes == b"marker" for record in scorer_records)


@pytest.mark.asyncio
async def test_capture_failure_prevents_provider_invocation(tmp_path: Path) -> None:
    pytest.importorskip("browser_use")
    from browser_use.llm.messages import UserMessage

    class NeverCalled:
        provider = "openrouter"
        model = "openai/gpt-5.2"
        name = model
        temperature = None
        top_p = None
        seed = None
        extra_body = None
        calls = 0

        def get_client(self) -> object:
            self.calls += 1
            raise AssertionError("provider client must not be created")

    archive, transcript = _archive_and_transcript(tmp_path)
    delegate = NeverCalled()
    instrumented = InstrumentedChatModel(delegate, archive, transcript)
    with pytest.raises(ObservationCaptureError):
        await instrumented.ainvoke([UserMessage(content="no current browser state")])
    assert delegate.calls == 0
    assert not (archive.run_dir / "observations").exists()
