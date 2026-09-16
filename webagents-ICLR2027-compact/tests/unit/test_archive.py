from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from webagents.capture.archive import (
    ArchiveError,
    RunArchive,
    canonical_json_bytes,
    iter_model_observations,
    verify_run,
)
from webagents.schemas import (
    CompletionRecord,
    FrameworkIdentity,
    ImplementationIdentity,
    ModelCallMetadata,
    RunInput,
    RunManifest,
    RuntimeIdentity,
    RunTranscript,
)

RUN_ID = "01KZPD5RF0AEVYVGP4QJK7Y89Z"


def _manifest() -> RunManifest:
    now = datetime.now(UTC)
    return RunManifest(
        run_id=RUN_ID,
        agent_class="dom_extraction",
        input=RunInput(page_url="http://localhost:3000", task_prompt="Read X", model="openrouter/openai/gpt-5.2"),
        runtime=RuntimeIdentity(started_at=now, host_platform="test", python="3.12.0"),
        implementation=ImplementationIdentity(
            repository_commit=None,
            source_state="unversioned",
            framework_name="browser-use",
            framework_version="0.13.7",
            framework_commit="f0aa3a8bb03779c71a5aa262d389e3bfe6b77cdc",
            prompt_version="test",
        ),
    )


def _metadata(step: int = 0, attempt: int = 0) -> ModelCallMetadata:
    now = datetime.now(UTC)
    return ModelCallMetadata(
        run_id=RUN_ID,
        step=step,
        attempt=attempt,
        agent_class="dom_extraction",
        observation_kind="serialized_dom",
        captured_at=now,
        model_call_started_at=now,
    )


def _transcript() -> RunTranscript:
    return RunTranscript(
        run_id=RUN_ID,
        agent_class="dom_extraction",
        framework=FrameworkIdentity(version="0.13.7", commit="f0aa3a8bb03779c71a5aa262d389e3bfe6b77cdc"),
        input=_manifest().input,
        started_at=datetime.now(UTC),
        status="failure",
    )


def _dom_request(observation: bytes, **extra: object) -> bytes:
    content = f"<browser_state>\n{observation.decode()}\n</browser_state>"
    return canonical_json_bytes({**extra, "messages": [{"content": content}]})


def test_archive_commits_exact_bytes_and_loader_verifies_them(tmp_path: Path) -> None:
    archive = RunArchive.create(tmp_path, _manifest())
    observation = "DOM marker: café\r\n<input>".encode()
    request = _dom_request(observation)
    refs = archive.record_model_call(
        step=0,
        attempt=0,
        observation_kind="serialized_dom",
        observation_bytes=observation,
        model_request_bytes=request,
        metadata=_metadata(),
    )
    assert refs.observation.sha256 == hashlib.sha256(observation).hexdigest()
    assert (archive.run_dir / refs.observation.artifact_path).read_bytes() == observation
    assert list(iter_model_observations(tmp_path))[0].observation_bytes == observation
    archive.checkpoint_transcript(_transcript())
    assert not [item for item in verify_run(archive.run_dir, require_complete=False) if item.severity == "error"]

    with pytest.raises(ArchiveError):
        archive.record_model_call(
            step=0,
            attempt=0,
            observation_kind="serialized_dom",
            observation_bytes=b"replacement",
            model_request_bytes=request,
            metadata=_metadata(),
        )
    assert (archive.run_dir / refs.observation.artifact_path).read_bytes() == observation


def test_archive_detects_mutation_and_duplicate_run(tmp_path: Path) -> None:
    archive = RunArchive.create(tmp_path, _manifest())
    archive.record_model_call(
        step=0,
        attempt=0,
        observation_kind="serialized_dom",
        observation_bytes=b"marker",
        model_request_bytes=_dom_request(b"marker"),
        metadata=_metadata(),
    )
    (archive.run_dir / "observations/step-000/observation.txt").write_bytes(b"Marker")
    codes = {diagnostic.code for diagnostic in verify_run(archive.run_dir, require_complete=False)}
    assert "observation_hash_mismatch" in codes
    with pytest.raises(ArchiveError):
        RunArchive.create(tmp_path, _manifest())


def test_verifier_reports_uncommitted_model_call_directory(tmp_path: Path) -> None:
    archive = RunArchive.create(tmp_path, _manifest())
    archive.checkpoint_transcript(_transcript())
    (archive.run_dir / "observations/step-000").mkdir(parents=True)
    (archive.run_dir / "observations/step-000/observation.txt").write_text("partial")
    codes = {diagnostic.code for diagnostic in verify_run(archive.run_dir, require_complete=False)}
    assert "uncommitted_model_call" in codes


def test_completion_hash_must_match_checkpoint(tmp_path: Path) -> None:
    archive = RunArchive.create(tmp_path, _manifest())
    transcript = _transcript()
    transcript.finished_at = datetime.now(UTC)
    archive.checkpoint_transcript(transcript)
    transcript_bytes = (archive.run_dir / "transcript.json").read_bytes()
    archive.complete(
        CompletionRecord(
            run_id=RUN_ID,
            status="failure",
            finished_at=datetime.now(UTC),
            transcript_sha256=hashlib.sha256(transcript_bytes).hexdigest(),
        )
    )
    assert verify_run(archive.run_dir) == []


def test_model_request_never_contains_environment_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "super-secret-api-key")
    archive = RunArchive.create(tmp_path, _manifest())
    archive.record_model_call(
        step=0,
        attempt=0,
        observation_kind="serialized_dom",
        observation_bytes=b"public synthetic content",
        model_request_bytes=_dom_request(b"public synthetic content", model="openai/gpt-5.2"),
        metadata=_metadata(),
    )
    assert b"super-secret-api-key" not in b"".join(
        path.read_bytes() for path in archive.run_dir.rglob("*") if path.is_file()
    )
