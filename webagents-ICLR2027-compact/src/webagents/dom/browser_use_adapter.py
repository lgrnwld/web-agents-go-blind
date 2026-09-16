"""The sole adapter allowed to import and depend on Browser Use internals."""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from webagents.capture.archive import RunArchive, canonical_json_bytes
from webagents.schemas import (
    ActionRecord,
    ActionResultRecord,
    FrameworkIdentity,
    ModelCallMetadata,
    ModelResponse,
    RunEvent,
    RunStep,
    RunTranscript,
    TokenUsage,
    ToolCall,
)

FRAMEWORK_NAME = "browser-use"
FRAMEWORK_VERSION = "0.13.7"
FRAMEWORK_COMMIT = "f0aa3a8bb03779c71a5aa262d389e3bfe6b77cdc"
PROMPT_VERSION = f"browser-use-{FRAMEWORK_VERSION}-dom-no-vision-v2"
DOM_ONLY_EXCLUDED_ACTIONS = [
    "screenshot",
    "extract",
    "evaluate",
    "find_elements",
    "find_text",
    "search_page",
    "dropdown_options",
    "read_file",
    "save_as_pdf",
]

logger = logging.getLogger(__name__)


class FrameworkIdentityError(RuntimeError):
    """Raised when the imported Browser Use package is not the pinned source."""


class ObservationCaptureError(RuntimeError):
    """Raised before provider invocation if the exact DOM observation cannot be archived."""


def configure_browser_use_environment(runtime_dir: Path) -> None:
    """Keep framework state run-local and disable out-of-scope telemetry/cloud sync."""

    os.environ["BROWSER_USE_CONFIG_DIR"] = str((runtime_dir / "config").resolve())
    os.environ["XDG_CACHE_HOME"] = str((runtime_dir / "cache").resolve())
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
    os.environ["BROWSER_USE_SETUP_LOGGING"] = "false"


def _disable_display_probe_for_headless() -> None:
    """Avoid native GUI probing before the explicitly headless profile is constructed."""

    if sys.platform != "darwin":
        return
    # At the pin, BrowserSession creates a default profile at import time and probes NSScreen
    # even when the caller will immediately supply headless=True. NSScreen may terminate a
    # non-GUI process, so keep this narrow compatibility shim inside the pinned adapter.
    from browser_use.browser import profile as profile_module

    profile_module.get_display_size = lambda: None


def make_browser_use_model(*, provider: str, model: str, settings: dict[str, Any]) -> Any:
    if provider == "openrouter":
        from browser_use.llm import ChatOpenRouter

        client = ChatOpenRouter(model=model, **settings)
        setattr(client, "_webagents_provider", provider)
        return client
    if provider == "foundry":
        from browser_use.llm import ChatOpenAI

        client = ChatOpenAI(model=model, **settings)
        setattr(client, "_webagents_provider", provider)
        return client
    raise ValueError(f"unsupported provider: {provider}")


def _provider_name(model: Any) -> str:
    return str(getattr(model, "_webagents_provider", model.provider))


def check_framework_identity(lock_path: Path | None = None) -> FrameworkIdentity:
    """Fail closed unless version, installed VCS source, and lock all match the pin."""

    try:
        distribution = importlib.metadata.distribution(FRAMEWORK_NAME)
    except importlib.metadata.PackageNotFoundError as exc:
        raise FrameworkIdentityError("browser-use is not installed; run `uv sync --frozen`") from exc

    version = distribution.version
    if version != FRAMEWORK_VERSION:
        raise FrameworkIdentityError(f"browser-use version mismatch: expected {FRAMEWORK_VERSION}, imported {version}")

    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text is None:
        raise FrameworkIdentityError("browser-use has no direct_url.json; the pinned Git source cannot be verified")
    try:
        direct_url = json.loads(direct_url_text)
        vcs_info = direct_url["vcs_info"]
        commit_id = vcs_info["commit_id"]
        requested_revision = vcs_info.get("requested_revision")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise FrameworkIdentityError("browser-use direct_url.json does not describe a Git installation") from exc
    if commit_id != FRAMEWORK_COMMIT or requested_revision != FRAMEWORK_COMMIT:
        raise FrameworkIdentityError(
            "browser-use source mismatch: "
            f"expected requested/imported {FRAMEWORK_COMMIT}, found {requested_revision}/{commit_id}"
        )

    if lock_path is not None:
        try:
            lock_text = lock_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FrameworkIdentityError(f"cannot read lockfile: {lock_path}") from exc
        locked_source = (
            'source = { git = "https://github.com/browser-use/browser-use.git'
            f"?rev={FRAMEWORK_COMMIT}#{FRAMEWORK_COMMIT}" + '" }'
        )
        if locked_source not in lock_text:
            raise FrameworkIdentityError("uv.lock does not contain the full pinned Browser Use commit")

    identity = FrameworkIdentity(version=version, commit=commit_id)
    logger.info("verified browser-use version=%s commit=%s", version, commit_id)
    return identity


def _message_text(message: Any) -> str:
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.text for part in content if getattr(part, "type", None) == "text")
    return ""


def extract_serialized_dom(messages: list[Any]) -> bytes:
    """Extract the exact model-visible bytes between the current browser-state tags."""

    start_marker = "<browser_state>\n"
    end_marker = "\n</browser_state>"
    matches: list[str] = []
    for message in reversed(messages):
        text = _message_text(message)
        start = text.find(start_marker)
        if start == -1:
            continue
        content_start = start + len(start_marker)
        end = text.find(end_marker, content_start)
        if end == -1:
            raise ObservationCaptureError("current model request has an unterminated <browser_state> block")
        matches.append(text[content_start:end])
        break
    if len(matches) != 1:
        raise ObservationCaptureError("current model request does not contain exactly one serialized DOM observation")
    return matches[0].encode("utf-8")


def _optimized_schema(output_format: type[BaseModel], model: Any) -> dict[str, Any]:
    from browser_use.llm.schema import SchemaOptimizer

    return SchemaOptimizer.create_optimized_json_schema(
        output_format,
        remove_min_items=getattr(model, "remove_min_items_from_schema", False),
        remove_defaults=getattr(model, "remove_defaults_from_schema", False),
    )


def _openai_model_params(model: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for attr, wire_name in (
        ("temperature", "temperature"),
        ("frequency_penalty", "frequency_penalty"),
        ("max_completion_tokens", "max_completion_tokens"),
        ("top_p", "top_p"),
        ("seed", "seed"),
        ("service_tier", "service_tier"),
    ):
        value = getattr(model, attr, None)
        if value is not None:
            params[wire_name] = value
    reasoning_models = getattr(model, "reasoning_models", None)
    if reasoning_models and any(str(name).lower() in str(model.model).lower() for name in reasoning_models):
        params["reasoning_effort"] = getattr(model, "reasoning_effort", "low")
        params.pop("temperature", None)
        params.pop("frequency_penalty", None)
    return params


def build_provider_request(model: Any, messages: list[Any], output_format: type[BaseModel] | None) -> bytes:
    """Mirror the pinned provider adapters and serialize their final request once."""

    provider = _provider_name(model)
    if provider == "openrouter":
        from browser_use.llm.openrouter.serializer import OpenRouterMessageSerializer

        body: dict[str, Any] = {
            "model": str(model.model),
            "messages": OpenRouterMessageSerializer.serialize_messages(messages),
            "temperature": model.temperature,
            "top_p": model.top_p,
            "seed": model.seed,
        }
        if output_format is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_output",
                    "strict": True,
                    "schema": _optimized_schema(output_format, model),
                },
            }
        body.update(model.extra_body or {})
        return canonical_json_bytes(body)

    if provider == "foundry":
        from browser_use.llm.openai.serializer import OpenAIMessageSerializer

        openai_messages = OpenAIMessageSerializer.serialize_messages(messages)
        body = {"model": str(model.model), "messages": openai_messages, **_openai_model_params(model)}
        if output_format is not None:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_output",
                    "strict": True,
                    "schema": _optimized_schema(output_format, model),
                },
            }
            if model.add_schema_to_system_prompt and openai_messages and openai_messages[0]["role"] == "system":
                schema_json = canonical_json_bytes(response_format["json_schema"]).decode("utf-8")
                schema_text = f"\n<json_schema>\n{schema_json}\n</json_schema>"
                content = openai_messages[0]["content"]
                if isinstance(content, str):
                    openai_messages[0]["content"] = content + schema_text
                else:
                    openai_messages[0]["content"] = [
                        *content,
                        {"text": schema_text, "type": "text"},
                    ]
            if not model.dont_force_structured_output:
                body["response_format"] = response_format
        return canonical_json_bytes(body)

    raise ObservationCaptureError(f"unsupported instrumented provider: {provider}")


async def _invoke_provider_from_captured_request(
    model: Any,
    request_bytes: bytes,
    output_format: type[BaseModel] | None,
) -> tuple[Any, str, dict[str, Any]]:
    """Send the captured request object and retain the provider's original response text."""

    from browser_use.llm.views import ChatInvokeCompletion

    body = json.loads(request_bytes)
    response = await model.get_client().chat.completions.create(**body)
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ValueError("provider returned no chat-completion choices")
    choice = choices[0]
    raw_text = choice.message.content or ""
    if output_format is not None:
        if not raw_text:
            raise ValueError("provider returned no structured response text")
        completion = output_format.model_validate_json(raw_text)
    else:
        completion = raw_text
    usage = model._get_usage(response)
    metadata = {
        "response_id": getattr(response, "id", None),
        "created": getattr(response, "created", None),
        "model": getattr(response, "model", str(model.model)),
        "system_fingerprint": getattr(response, "system_fingerprint", None),
    }
    return (
        ChatInvokeCompletion(
            completion=completion,
            usage=usage,
            stop_reason=getattr(choice, "finish_reason", None),
        ),
        raw_text,
        metadata,
    )


def _actions_from_completion(completion: Any) -> tuple[list[ToolCall], list[ActionRecord]]:
    tool_calls: list[ToolCall] = []
    actions: list[ActionRecord] = []
    for action in getattr(completion, "action", None) or []:
        raw = action.model_dump(exclude_none=True, mode="json") if hasattr(action, "model_dump") else {}
        for name, parameters in raw.items():
            if parameters is None:
                parameters = {}
            elif not isinstance(parameters, dict):
                parameters = {"value": parameters}
            tool_calls.append(ToolCall(name=name, arguments=parameters))
            actions.append(ActionRecord(name=name, parameters=parameters))
    return tool_calls, actions


class InstrumentedChatModel:
    """Browser Use model protocol wrapper that commits evidence before each provider call."""

    def __init__(self, delegate: Any, archive: RunArchive, transcript: RunTranscript) -> None:
        self._delegate = delegate
        self._archive = archive
        self._transcript = transcript
        self._step_provider: Callable[[], int] = lambda: 0
        self._attempts: defaultdict[int, int] = defaultdict(int)

    @property
    def model(self) -> str:
        return str(self._delegate.model)

    @property
    def model_name(self) -> str:
        """Preserve Browser Use's legacy BaseChatModel compatibility alias."""

        return self.model

    @property
    def provider(self) -> str:
        return _provider_name(self._delegate)

    @property
    def name(self) -> str:
        return str(self._delegate.name)

    def set_step_provider(self, provider: Callable[[], int]) -> None:
        self._step_provider = provider

    async def ainvoke(self, messages: list[Any], output_format: type[BaseModel] | None = None, **kwargs: Any) -> Any:
        del kwargs  # Browser Use passes session_id; the pinned provider adapters do not put it on the wire.
        step = max(0, self._step_provider())
        attempt = self._attempts[step]
        self._attempts[step] += 1
        captured_at = datetime.now(UTC)

        try:
            observation_bytes = extract_serialized_dom(messages)
            request_bytes = build_provider_request(self._delegate, messages, output_format)
            refs = self._archive.record_model_call(
                step=step,
                attempt=attempt,
                observation_kind="serialized_dom",
                observation_bytes=observation_bytes,
                model_request_bytes=request_bytes,
                metadata=ModelCallMetadata(
                    run_id=self._transcript.run_id,
                    step=step,
                    attempt=attempt,
                    agent_class="dom_extraction",
                    observation_kind="serialized_dom",
                    captured_at=captured_at,
                    model_call_started_at=datetime.now(UTC),
                ),
            )
        except Exception as exc:
            raise ObservationCaptureError(f"capture failed before provider invocation: {exc}") from exc

        transcript_step = RunStep(
            step=step,
            attempt=attempt,
            timestamp=captured_at,
            url=self._transcript.input.page_url,
            observation=refs.observation,
            model_request=refs.model_request,
        )
        self._transcript.steps.append(transcript_step)
        self._archive.append_event(
            RunEvent(
                sequence=self._archive.next_event_sequence,
                timestamp=captured_at,
                kind="model_request_committed",
                step=step,
                attempt=attempt,
            )
        )
        self._archive.checkpoint_transcript(self._transcript)

        try:
            response, raw_text, raw_provider_metadata = await _invoke_provider_from_captured_request(
                self._delegate,
                request_bytes,
                output_format,
            )
        except BaseException as exc:
            transcript_step.error = f"{type(exc).__name__}: {exc}"
            self._archive.append_event(
                RunEvent(
                    sequence=self._archive.next_event_sequence,
                    timestamp=datetime.now(UTC),
                    kind="model_response_error",
                    step=step,
                    attempt=attempt,
                    detail={"error_type": type(exc).__name__, "message": str(exc)},
                )
            )
            self._archive.checkpoint_transcript(self._transcript)
            raise

        usage = response.usage
        tool_calls, actions = _actions_from_completion(response.completion)
        transcript_step.model_response = ModelResponse(
            text=raw_text,
            tool_calls=tool_calls,
            provider_metadata={
                "provider": self.provider,
                "model": self.model,
                "stop_reason": response.stop_reason,
                "stop_details": response.stop_details,
                **raw_provider_metadata,
            },
        )
        transcript_step.actions = actions
        if usage is not None:
            transcript_step.usage = TokenUsage(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
            )
        self._archive.append_event(
            RunEvent(
                sequence=self._archive.next_event_sequence,
                timestamp=datetime.now(UTC),
                kind="model_response_received",
                step=step,
                attempt=attempt,
            )
        )
        self._archive.checkpoint_transcript(self._transcript)
        return response


def _result_record(result: Any) -> ActionResultRecord:
    metadata = result.metadata if isinstance(getattr(result, "metadata", None), dict) else {}
    return ActionResultRecord(
        extracted_content=getattr(result, "extracted_content", None),
        error=getattr(result, "error", None),
        is_done=bool(getattr(result, "is_done", False)),
        success=getattr(result, "success", None),
        metadata=metadata,
    )


def merge_history_into_transcript(history: Any, transcript: RunTranscript) -> None:
    """Translate Browser Use history while retaining per-invocation capture records."""

    last_attempt_by_step: dict[int, RunStep] = {}
    for step in transcript.steps:
        current = last_attempt_by_step.get(step.step)
        if current is None or step.attempt > current.attempt:
            last_attempt_by_step[step.step] = step

    for index, item in enumerate(history.history):
        transcript_step = last_attempt_by_step.get(index)
        if transcript_step is None:
            continue
        transcript_step.url = item.state.url or transcript_step.url
        transcript_step.action_results = [_result_record(result) for result in item.result]
        if transcript_step.error is None:
            transcript_step.error = next((result.error for result in item.result if result.error), None)


async def run_browser_use_dom(
    *,
    page_url: str,
    task_prompt: str,
    model: Any,
    max_steps: int,
    archive: RunArchive,
    transcript: RunTranscript,
    cross_origin_iframes: bool = True,
) -> Any:
    """Run the pinned framework with a DOM-only model-visible observation path."""

    _disable_display_probe_for_headless()
    from browser_use import Agent, BrowserProfile
    from browser_use.tools.service import Tools

    instrumented = InstrumentedChatModel(model, archive, transcript)
    tools = Tools(exclude_actions=DOM_ONLY_EXCLUDED_ACTIONS)
    profile = BrowserProfile(
        headless=True,
        user_data_dir=None,
        enable_default_extensions=False,
        highlight_elements=False,
        cross_origin_iframes=cross_origin_iframes,
    )
    agent = Agent(
        task=task_prompt,
        llm=cast(Any, instrumented),
        browser_profile=profile,
        tools=tools,
        initial_actions=[{"navigate": {"url": page_url, "new_tab": False}}],
        directly_open_url=False,
        use_vision=False,
        use_judge=False,
        message_compaction=False,
        generate_gif=False,
        calculate_cost=False,
        enable_signal_handler=False,
    )
    instrumented.set_step_provider(lambda: max(0, agent.state.n_steps - 1))
    browser_identity_recorded = False

    async def record_browser_identity(current_agent: Any) -> None:
        nonlocal browser_identity_recorded
        if browser_identity_recorded:
            return
        version_info = await current_agent.browser_session.cdp_client.send.Browser.getVersion()
        product = version_info.get("product") or version_info.get("userAgent")
        if not product:
            raise RuntimeError("CDP Browser.getVersion returned no browser product identity")
        archive.update_manifest(
            archive.manifest.model_copy(
                update={
                    "implementation": archive.manifest.implementation.model_copy(
                        update={"browser_version": str(product)}
                    )
                }
            )
        )
        archive.append_event(
            RunEvent(
                sequence=archive.next_event_sequence,
                timestamp=datetime.now(UTC),
                kind="browser_identity_recorded",
                detail={"browser_version": str(product)},
            )
        )
        browser_identity_recorded = True

    try:
        history = await agent.run(max_steps=max_steps, on_step_start=record_browser_identity)
        merge_history_into_transcript(history, transcript)
        return history
    finally:
        # Agent.run closes itself, and close is idempotent at the pinned revision.
        await agent.close()


def history_outcome(history: Any) -> tuple[bool, str | None, bool]:
    successful = history.is_successful() is True
    return successful, history.final_result(), history.is_successful() is True
