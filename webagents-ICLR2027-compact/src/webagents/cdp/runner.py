"""Standalone explicit CDP DOMSnapshot/frame-traversal agent loop."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import platform
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webagents.ax.runner import (
    CHROMIUM_REVISION,
    DEFAULT_MAX_STEPS,
    DEFAULT_SETTLE_MILLISECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    PLAYWRIGHT_VERSION,
    PlaywrightIdentityError,
    check_playwright_identity,
)
from webagents.ax.targets import CDPConnection, CDPError, TargetCollector, wait_for_debugger_url
from webagents.capture.archive import ArchiveError, RunArchive
from webagents.cdp.actions import FinishAction, action_record, execute_action, parse_action
from webagents.cdp.observer import OBSERVATION_VERSION, capture_cdp_snapshot
from webagents.cdp.prompt import PROMPT_VERSION, build_model_request
from webagents.providers import ProviderClient, ProviderConfigurationError, resolve_provider
from webagents.runtime import generate_run_id, project_root, repository_identity
from webagents.schemas import (
    ActionResultRecord,
    CompletionRecord,
    FinalResult,
    FrameworkIdentity,
    ImplementationIdentity,
    ModelCallMetadata,
    ModelResponse,
    RunEvent,
    RunInput,
    RunManifest,
    RunRequest,
    RunStep,
    RuntimeIdentity,
    RunTranscript,
    TokenUsage,
    ToolCall,
)


def _reserve_debug_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _base_transcript(request: RunRequest, run_id: str, started_at: datetime) -> RunTranscript:
    return RunTranscript(
        run_id=run_id,
        agent_class="cdp_frame_traversal",
        framework=FrameworkIdentity(
            name="playwright",
            version=PLAYWRIGHT_VERSION,
            commit=f"chromium-{CHROMIUM_REVISION}",
        ),
        input=RunInput(page_url=request.page_url, task_prompt=request.task_prompt, model=request.model),
        started_at=started_at,
        status="failure",
    )


def _make_manifest(transcript: RunTranscript) -> RunManifest:
    repository_commit, source_state = repository_identity()
    return RunManifest(
        run_id=transcript.run_id,
        agent_class="cdp_frame_traversal",
        input=transcript.input,
        runtime=RuntimeIdentity(
            started_at=transcript.started_at,
            host_platform=platform.platform(),
            python=sys.version.split()[0],
        ),
        implementation=ImplementationIdentity(
            repository_commit=repository_commit,
            source_state=source_state,
            framework_name="playwright-cdp-dom-snapshot",
            framework_version=PLAYWRIGHT_VERSION,
            framework_commit=f"chromium-{CHROMIUM_REVISION}",
            playwright_version=PLAYWRIGHT_VERSION,
            prompt_version=PROMPT_VERSION,
        ),
    )


def _typed_result(result: ActionResultRecord) -> dict[str, Any]:
    return {
        "success": result.success,
        "error": result.error,
        "code": result.metadata.get("code", "unknown"),
    }


async def _agent_loop(
    *,
    page: Any,
    connection: CDPConnection,
    collector: TargetCollector,
    provider: ProviderClient,
    request: RunRequest,
    archive: RunArchive,
    transcript: RunTranscript,
    settle_milliseconds: int,
    observation_policy: str,
) -> None:
    previous_result: dict[str, Any] | None = None
    for step_number in range(request.max_steps):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(settle_milliseconds)
        _, observation_bytes, refs, complete, missing = await capture_cdp_snapshot(
            collector,
            generation=step_number,
            observation_policy=observation_policy,
            top_level_url=request.page_url,
        )
        _, request_bytes = build_model_request(
            model=provider.model,
            task_prompt=request.task_prompt,
            observation_bytes=observation_bytes,
            previous_action_result=previous_result,
        )
        captured_at = datetime.now(UTC)
        artifacts = archive.record_model_call(
            step=step_number,
            attempt=0,
            observation_kind="cdp_dom_snapshot",
            observation_bytes=observation_bytes,
            model_request_bytes=request_bytes,
            metadata=ModelCallMetadata(
                run_id=transcript.run_id,
                step=step_number,
                attempt=0,
                agent_class="cdp_frame_traversal",
                observation_kind="cdp_dom_snapshot",
                captured_at=captured_at,
                model_call_started_at=datetime.now(UTC),
                transform={"name": OBSERVATION_VERSION, "version": "1"},
                capture_completeness="complete" if complete else "partial",
                missing_targets=missing,
            ),
        )
        transcript_step = RunStep(
            step=step_number,
            timestamp=captured_at,
            url=page.url,
            observation=artifacts.observation,
            model_request=artifacts.model_request,
        )
        transcript.steps.append(transcript_step)
        archive.append_event(
            RunEvent(
                sequence=archive.next_event_sequence,
                timestamp=captured_at,
                kind="model_request_committed",
                step=step_number,
                attempt=0,
                detail={"capture_completeness": "complete" if complete else "partial"},
            )
        )
        archive.checkpoint_transcript(transcript)

        try:
            response = await provider.invoke(request_bytes)
        except BaseException as exc:
            refs.invalidate()
            transcript_step.error = f"{type(exc).__name__}: {exc}"
            archive.checkpoint_transcript(transcript)
            raise
        transcript_step.model_response = ModelResponse(
            text=response.text,
            provider_metadata=response.metadata or {"provider": provider.provider, "model": provider.model},
        )
        transcript_step.usage = TokenUsage(
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
        archive.append_event(
            RunEvent(
                sequence=archive.next_event_sequence,
                timestamp=datetime.now(UTC),
                kind="model_response_received",
                step=step_number,
                attempt=0,
            )
        )

        try:
            action = parse_action(response.text)
        except ValueError as exc:
            refs.invalidate()
            result = ActionResultRecord(error=str(exc), success=False, metadata={"code": "invalid_action"})
            transcript_step.action_results = [result]
            transcript_step.error = str(exc)
            previous_result = _typed_result(result)
            archive.checkpoint_transcript(transcript)
            continue

        record = action_record(action)
        transcript_step.actions = [record]
        transcript_step.model_response.tool_calls = [ToolCall(name=record.name, arguments=record.parameters)]
        result = await execute_action(
            action,
            refs=refs,
            generation=step_number,
            connection=connection,
            page=page,
        )
        transcript_step.action_results = [result]
        transcript_step.error = result.error
        previous_result = _typed_result(result)
        archive.append_event(
            RunEvent(
                sequence=archive.next_event_sequence,
                timestamp=datetime.now(UTC),
                kind="action_executed",
                step=step_number,
                attempt=0,
                detail={"action": record.name, "success": result.success},
            )
        )
        archive.checkpoint_transcript(transcript)
        if isinstance(action, FinishAction):
            transcript.status = "success" if action.success else "failure"
            transcript.final = FinalResult(
                answer=action.answer,
                success_claimed=action.success,
                error=None if action.success else "agent reported unsuccessful completion",
            )
            return

    transcript.status = "failure"
    transcript.final = FinalResult(error=f"maximum step count exhausted ({request.max_steps})")


async def _run_cdp_agent(
    page_url: str,
    task_prompt: str,
    model: str,
    *,
    run_id: str | None,
    max_steps: int,
    archive_root: Path,
    timeout_seconds: float,
    observation_policy: str = "unrestricted",
) -> RunTranscript:
    if observation_policy not in {"unrestricted", "same_origin_only"}:
        raise ValueError("observation_policy must be unrestricted or same_origin_only")
    request = RunRequest(
        page_url=page_url,
        task_prompt=task_prompt,
        model=model,
        run_id=run_id,
        max_steps=max_steps,
    )
    actual_run_id = request.run_id or generate_run_id()
    started_at = datetime.now(UTC)
    transcript = _base_transcript(request, actual_run_id, started_at)
    transcript.input = transcript.input.model_copy(update={"observation_policy": observation_policy})
    archive: RunArchive | None = None
    browser: Any = None
    context: Any = None
    playwright_manager: Any = None
    connection: CDPConnection | None = None
    collector: TargetCollector | None = None
    provider: ProviderClient | None = None
    capture_failed = False

    try:
        archive = RunArchive.create(archive_root, _make_manifest(transcript))
        archive.append_event(
            RunEvent(sequence=0, timestamp=started_at, kind="run_started", detail={"max_steps": max_steps})
        )
        archive.checkpoint_transcript(transcript)
        lock_path = project_root() / "uv.lock"
        check_playwright_identity(lock_path if lock_path.is_file() else None)
        provider = resolve_provider(request.model)

        from playwright.async_api import async_playwright

        debug_port = _reserve_debug_port()
        playwright_manager = await async_playwright().start()
        browser = await playwright_manager.chromium.launch(
            headless=True,
            args=[
                f"--remote-debugging-port={debug_port}",
                "--remote-debugging-address=127.0.0.1",
                "--site-per-process",
            ],
        )
        context = await browser.new_context()
        page = await context.new_page()
        archive.update_manifest(
            archive.manifest.model_copy(
                update={
                    "implementation": archive.manifest.implementation.model_copy(
                        update={"browser_version": browser.version}
                    )
                }
            )
        )
        archive.append_event(
            RunEvent(
                sequence=archive.next_event_sequence,
                timestamp=datetime.now(UTC),
                kind="browser_identity_recorded",
                detail={"browser_version": browser.version, "chromium_revision": CHROMIUM_REVISION},
            )
        )
        await page.goto(request.page_url, wait_until="domcontentloaded")
        debugger_url = await wait_for_debugger_url(debug_port)
        connection = await CDPConnection.open(debugger_url)
        collector = TargetCollector(connection)
        await collector.initialize()
        settle = int(os.environ.get("WEBAGENT_CDP_SETTLE_MILLISECONDS", DEFAULT_SETTLE_MILLISECONDS))
        if settle < 0 or settle > 30_000:
            raise ValueError("WEBAGENT_CDP_SETTLE_MILLISECONDS must be between 0 and 30000")
        async with asyncio.timeout(timeout_seconds):
            await _agent_loop(
                page=page,
                connection=connection,
                collector=collector,
                provider=provider,
                request=request,
                archive=archive,
                transcript=transcript,
                settle_milliseconds=settle,
                observation_policy=observation_policy,
            )
    except TimeoutError:
        transcript.status = "timeout"
        transcript.final = FinalResult(error=f"wall-clock timeout after {timeout_seconds:g} seconds")
    except (CDPError, ArchiveError) as exc:
        capture_failed = True
        transcript.status = "infrastructure_error"
        transcript.final = FinalResult(error=str(exc))
    except (PlaywrightIdentityError, ProviderConfigurationError) as exc:
        transcript.status = "infrastructure_error"
        transcript.final = FinalResult(error=str(exc))
    except Exception as exc:
        transcript.status = "infrastructure_error"
        transcript.final = FinalResult(error=f"{type(exc).__name__}: {exc}")
    finally:
        if collector is not None:
            with contextlib.suppress(Exception):
                await collector.close()
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        if playwright_manager is not None:
            with contextlib.suppress(Exception):
                await playwright_manager.stop()
        if provider is not None:
            close_provider = getattr(provider, "close", None)
            if close_provider is not None:
                with contextlib.suppress(Exception):
                    await close_provider()
        transcript.finished_at = datetime.now(UTC)
        if archive is not None:
            try:
                archive.append_event(
                    RunEvent(
                        sequence=archive.next_event_sequence,
                        timestamp=transcript.finished_at,
                        kind="run_finished",
                        detail={"status": transcript.status},
                    )
                )
                archive.checkpoint_transcript(transcript)
                transcript_bytes = (archive.run_dir / "transcript.json").read_bytes()
                archive.complete(
                    CompletionRecord(
                        run_id=transcript.run_id,
                        status="capture_error" if capture_failed else transcript.status,
                        finished_at=transcript.finished_at,
                        transcript_sha256=hashlib.sha256(transcript_bytes).hexdigest(),
                    )
                )
            except ArchiveError as exc:
                transcript.status = "infrastructure_error"
                transcript.final = FinalResult(error=f"archive finalization failed: {exc}")
    return transcript


async def run_cdp_agent(
    page_url: str,
    task_prompt: str,
    model: str,
    *,
    run_id: str | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    archive_root: Path | None = None,
    timeout_seconds: float | None = None,
    observation_policy: str = "unrestricted",
) -> RunTranscript:
    """Run the independent CDP DOMSnapshot/frame-traversal agent."""

    resolved_archive_root = archive_root or Path(os.environ.get("WEBAGENT_ARCHIVE_ROOT", "artifacts"))
    resolved_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else float(os.environ.get("WEBAGENT_RUN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    )
    if resolved_timeout <= 0:
        raise ValueError("WEBAGENT_RUN_TIMEOUT_SECONDS must be positive")
    return await _run_cdp_agent(
        page_url,
        task_prompt,
        model,
        run_id=run_id,
        max_steps=max_steps,
        archive_root=resolved_archive_root,
        timeout_seconds=resolved_timeout,
        observation_policy=observation_policy,
    )
