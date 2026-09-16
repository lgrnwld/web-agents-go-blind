from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from webagents.dom import runner
from webagents.dom.browser_use_adapter import FrameworkIdentityError
from webagents.schemas import FrameworkIdentity


class FakeHistory:
    def __init__(self, *, successful: bool, answer: str | None = None) -> None:
        self.successful = successful
        self.answer = answer

    def is_successful(self) -> bool:
        return self.successful

    def final_result(self) -> str | None:
        return self.answer


def _patch_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner,
        "check_framework_identity",
        lambda lock: FrameworkIdentity(version="0.13.7", commit="f0aa3a8bb03779c71a5aa262d389e3bfe6b77cdc"),
    )
    monkeypatch.setattr(runner, "resolve_model", lambda model: SimpleNamespace(client=object()))


@pytest.mark.asyncio
async def test_runner_success_and_max_step_failure_are_valid_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_startup(monkeypatch)

    async def success(**kwargs: object) -> FakeHistory:
        return FakeHistory(successful=True, answer="marker copied")

    monkeypatch.setattr(runner, "run_browser_use_dom", success)
    transcript = await runner._run_dom_agent(
        "http://localhost:3000",
        "Copy marker",
        "openrouter/openai/gpt-5.2",
        run_id=None,
        max_steps=3,
        archive_root=tmp_path,
        timeout_seconds=1,
    )
    assert transcript.status == "success"
    assert transcript.final.answer == "marker copied"
    assert (tmp_path / "runs" / transcript.run_id / "completion.json").is_file()

    async def exhausted(**kwargs: object) -> FakeHistory:
        return FakeHistory(successful=False)

    monkeypatch.setattr(runner, "run_browser_use_dom", exhausted)
    failed = await runner._run_dom_agent(
        "http://localhost:3000",
        "Copy marker",
        "openrouter/openai/gpt-5.2",
        run_id=None,
        max_steps=1,
        archive_root=tmp_path,
        timeout_seconds=1,
    )
    assert failed.status == "failure"
    assert failed.finished_at is not None


@pytest.mark.asyncio
async def test_runner_timeout_preserves_parseable_partial_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_startup(monkeypatch)

    async def hangs(**kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "run_browser_use_dom", hangs)
    transcript = await runner._run_dom_agent(
        "http://localhost:3000",
        "Copy marker",
        "openrouter/openai/gpt-5.2",
        run_id=None,
        max_steps=3,
        archive_root=tmp_path,
        timeout_seconds=0.01,
    )
    assert transcript.status == "timeout"
    assert transcript.model_validate_json(transcript.model_dump_json()) == transcript


@pytest.mark.asyncio
async def test_runner_classifies_framework_and_browser_failures_as_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner, "check_framework_identity", lambda lock: (_ for _ in ()).throw(FrameworkIdentityError("mismatch"))
    )
    mismatch = await runner._run_dom_agent(
        "http://localhost:3000",
        "Copy marker",
        "openrouter/openai/gpt-5.2",
        run_id=None,
        max_steps=3,
        archive_root=tmp_path,
        timeout_seconds=1,
    )
    assert mismatch.status == "infrastructure_error"
    assert "mismatch" in (mismatch.final.error or "")

    _patch_startup(monkeypatch)

    async def browser_crash(**kwargs: object) -> None:
        raise ConnectionError("browser disconnected")

    monkeypatch.setattr(runner, "run_browser_use_dom", browser_crash)
    crash = await runner._run_dom_agent(
        "http://localhost:3000",
        "Copy marker",
        "openrouter/openai/gpt-5.2",
        run_id=None,
        max_steps=3,
        archive_root=tmp_path,
        timeout_seconds=1,
    )
    assert crash.status == "infrastructure_error"
    assert "browser disconnected" in (crash.final.error or "")
