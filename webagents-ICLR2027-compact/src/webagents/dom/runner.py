"""Lifecycle and error-normalizing runner for the DOM-extraction agent."""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

from webagents.capture.archive import ArchiveError, RunArchive
from webagents.dom.browser_use_adapter import (
    FRAMEWORK_COMMIT,
    FRAMEWORK_VERSION,
    PROMPT_VERSION,
    FrameworkIdentityError,
    ObservationCaptureError,
    check_framework_identity,
    configure_browser_use_environment,
    history_outcome,
    run_browser_use_dom,
)
from webagents.models import ModelConfigurationError, resolve_model
from webagents.runtime import generate_run_id, project_root, repository_identity
from webagents.schemas import (
    CompletionRecord,
    FinalResult,
    FrameworkIdentity,
    ImplementationIdentity,
    RunEvent,
    RunInput,
    RunManifest,
    RunRequest,
    RuntimeIdentity,
    RunTranscript,
)

DEFAULT_MAX_STEPS = 30
DEFAULT_TIMEOUT_SECONDS = 600.0
def _base_transcript(request: RunRequest, run_id: str, started_at: datetime) -> RunTranscript:
    return RunTranscript(
        run_id=run_id,
        agent_class="dom_extraction",
        framework=FrameworkIdentity(version=FRAMEWORK_VERSION, commit=FRAMEWORK_COMMIT),
        input=RunInput(page_url=request.page_url, task_prompt=request.task_prompt, model=request.model),
        started_at=started_at,
        status="failure",
    )


def _make_manifest(transcript: RunTranscript) -> RunManifest:
    repository_commit, source_state = repository_identity()
    return RunManifest(
        run_id=transcript.run_id,
        agent_class="dom_extraction",
        input=transcript.input,
        runtime=RuntimeIdentity(
            started_at=transcript.started_at,
            host_platform=platform.platform(),
            python=sys.version.split()[0],
        ),
        implementation=ImplementationIdentity(
            repository_commit=repository_commit,
            source_state=source_state,
            framework_name="browser-use",
            framework_version=FRAMEWORK_VERSION,
            framework_commit=FRAMEWORK_COMMIT,
            prompt_version=PROMPT_VERSION,
        ),
    )


async def _run_dom_agent(
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
    capture_failed = False

    try:
        archive = RunArchive.create(archive_root, _make_manifest(transcript))
        archive.append_event(
            RunEvent(sequence=0, timestamp=started_at, kind="run_started", detail={"max_steps": max_steps})
        )
        archive.checkpoint_transcript(transcript)

        lock_path = project_root() / "uv.lock"
        identity = check_framework_identity(lock_path if lock_path.is_file() else None)
        transcript.framework = identity
        configure_browser_use_environment(archive.run_dir / "runtime")
        resolved = resolve_model(request.model)

        async with asyncio.timeout(timeout_seconds):
            history = await run_browser_use_dom(
                page_url=request.page_url,
                task_prompt=request.task_prompt,
                model=resolved.client,
                max_steps=request.max_steps,
                archive=archive,
                transcript=transcript,
                cross_origin_iframes=observation_policy == "unrestricted",
            )
        success, answer, success_claimed = history_outcome(history)
        transcript.status = "success" if success else "failure"
        transcript.final = FinalResult(
            answer=answer,
            success_claimed=success_claimed,
            error=None if success else "agent did not claim successful task completion",
        )
    except TimeoutError:
        transcript.status = "timeout"
        transcript.final = FinalResult(error=f"wall-clock timeout after {timeout_seconds:g} seconds")
    except ObservationCaptureError as exc:
        capture_failed = True
        transcript.status = "infrastructure_error"
        transcript.final = FinalResult(error=str(exc))
    except (FrameworkIdentityError, ModelConfigurationError, ArchiveError) as exc:
        transcript.status = "infrastructure_error"
        transcript.final = FinalResult(error=str(exc))
    except Exception as exc:
        transcript.status = "infrastructure_error"
        transcript.final = FinalResult(error=f"{type(exc).__name__}: {exc}")
    finally:
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


async def run_dom_agent(
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
    """Run a pinned, DOM-only Browser Use agent and return its structured transcript."""

    resolved_archive_root = archive_root or Path(os.environ.get("WEBAGENT_ARCHIVE_ROOT", "artifacts"))
    resolved_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else float(os.environ.get("WEBAGENT_RUN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    )
    if resolved_timeout <= 0:
        raise ValueError("WEBAGENT_RUN_TIMEOUT_SECONDS must be positive")
    if observation_policy not in {"unrestricted", "same_origin_only"}:
        raise ValueError("observation_policy must be unrestricted or same_origin_only")
    return await _run_dom_agent(
        page_url,
        task_prompt,
        model,
        run_id=run_id,
        max_steps=max_steps,
        archive_root=resolved_archive_root,
        timeout_seconds=resolved_timeout,
        observation_policy=observation_policy,
    )
