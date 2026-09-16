from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from webagents import archive_cli
from webagents.capture import archive as archive_module
from webagents.capture.archive import (
    ArchiveError,
    RunArchive,
    canonical_json_bytes,
    scan_model_observations,
    verify_run,
)
from webagents.schemas import (
    FrameworkIdentity,
    ImplementationIdentity,
    ModelCallMetadata,
    RunInput,
    RunManifest,
    RuntimeIdentity,
    RunTranscript,
)


def _manifest(
    run_id: str,
    agent_class: Literal["dom_extraction", "accessibility_tree"] = "dom_extraction",
) -> RunManifest:
    now = datetime.now(UTC)
    return RunManifest(
        run_id=run_id,
        agent_class=agent_class,
        input=RunInput(
            page_url="http://localhost:3000/case",
            task_prompt="Find marker",
            model="openrouter/openai/gpt-5.2",
        ),
        runtime=RuntimeIdentity(started_at=now, host_platform="test", python="3.12"),
        implementation=ImplementationIdentity(
            repository_commit=None,
            source_state="unversioned",
            framework_name="fixture",
            framework_version="1",
            framework_commit="fixture-revision",
            prompt_version="fixture-prompt",
        ),
    )


def _metadata(
    run_id: str,
    *,
    step: int = 0,
    attempt: int = 0,
    agent_class: Literal["dom_extraction", "accessibility_tree"] = "dom_extraction",
    completeness: Literal["complete", "partial"] = "complete",
    missing: list[str] | None = None,
) -> ModelCallMetadata:
    now = datetime.now(UTC)
    return ModelCallMetadata(
        run_id=run_id,
        step=step,
        attempt=attempt,
        agent_class=agent_class,
        observation_kind="serialized_dom" if agent_class == "dom_extraction" else "ax_tree",
        captured_at=now,
        model_call_started_at=now,
        capture_completeness=completeness,
        missing_targets=missing or [],
    )


def _dom_request(observation: bytes, **extra: object) -> bytes:
    content = f"<browser_state>\n{observation.decode()}\n</browser_state>"
    return canonical_json_bytes({**extra, "messages": [{"content": content}]})


def _ax_request(observation: bytes) -> bytes:
    return canonical_json_bytes({"messages": [{"content": observation.decode(), "role": "user"}]})


def _transcript(manifest: RunManifest) -> RunTranscript:
    return RunTranscript(
        run_id=manifest.run_id,
        agent_class=manifest.agent_class,
        framework=FrameworkIdentity(
            name="browser-use" if manifest.agent_class == "dom_extraction" else "playwright",
            version="1",
            commit="fixture-revision",
        ),
        input=manifest.input,
        started_at=manifest.runtime.started_at,
        status="failure",
    )


def _record_dom(archive: RunArchive, marker: bytes, *, step: int = 0, attempt: int = 0) -> None:
    archive.record_model_call(
        step=step,
        attempt=attempt,
        observation_kind="serialized_dom",
        observation_bytes=marker,
        model_request_bytes=_dom_request(marker),
        metadata=_metadata(archive.manifest.run_id, step=step, attempt=attempt),
    )


def test_payload_contract_covers_unicode_crlf_markup_empty_and_large_ax(tmp_path: Path) -> None:
    dom_manifest = _manifest("DOM-BYTE-CONTRACT")
    dom = RunArchive.create(tmp_path, dom_manifest)
    for attempt, observation in enumerate(
        (
            'café\r\n&lt;input value="✓"&gt;'.encode(),
            b"",
            b'{"literal":"<browser_state> markup"}',
        )
    ):
        _record_dom(dom, observation, attempt=attempt)

    ax_manifest = _manifest("AX-BYTE-CONTRACT", "accessibility_tree")
    ax = RunArchive.create(tmp_path, ax_manifest)
    tree = canonical_json_bytes({"nodes": [{"name": "雪" * 20_000, "role": "StaticText"}]})
    ax.record_model_call(
        step=0,
        attempt=0,
        observation_kind="ax_tree",
        observation_bytes=tree,
        model_request_bytes=_ax_request(tree),
        metadata=_metadata("AX-BYTE-CONTRACT", agent_class="accessibility_tree"),
    )
    assert (ax.run_dir / "observations/step-000/observation.json").read_bytes() == tree


def test_writer_rejects_identity_mismatch_traversal_noncanonical_json_and_transport_secrets(tmp_path: Path) -> None:
    bad_manifest = _manifest("SAFE-ID").model_copy(update={"run_id": "../escape"})
    with pytest.raises(ArchiveError, match="unsafe run ID"):
        RunArchive.create(tmp_path, bad_manifest)

    archive = RunArchive.create(tmp_path, _manifest("VALIDATION-RUN"))
    with pytest.raises(ArchiveError, match="metadata does not match"):
        archive.record_model_call(
            step=0,
            attempt=0,
            observation_kind="serialized_dom",
            observation_bytes=b"marker",
            model_request_bytes=_dom_request(b"marker"),
            metadata=_metadata("WRONG-RUN"),
        )
    with pytest.raises(ArchiveError, match="canonical JSON"):
        archive.record_model_call(
            step=0,
            attempt=0,
            observation_kind="serialized_dom",
            observation_bytes=b"marker",
            model_request_bytes=b'{ "messages": [{"content":"<browser_state>\\nmarker\\n</browser_state>"}]}',
            metadata=_metadata("VALIDATION-RUN"),
        )
    with pytest.raises(ArchiveError, match="transport-secret"):
        archive.record_model_call(
            step=0,
            attempt=0,
            observation_kind="serialized_dom",
            observation_bytes=b"marker",
            model_request_bytes=_dom_request(b"marker", authorization="Bearer secret"),
            metadata=_metadata("VALIDATION-RUN"),
        )
    assert not (archive.run_dir / "observations").exists()


def test_cardinality_retries_concurrency_and_noncontiguous_diagnostics(tmp_path: Path) -> None:
    manifest = _manifest("CARDINALITY-RUN")
    archive = RunArchive.create(tmp_path, manifest)
    assert not (archive.run_dir / "observations").exists()
    _record_dom(archive, b"first", step=0, attempt=0)

    outcomes: list[str] = []

    def claim_retry() -> None:
        try:
            _record_dom(archive, b"retry", step=0, attempt=1)
            outcomes.append("committed")
        except ArchiveError:
            outcomes.append("duplicate")

    threads = [threading.Thread(target=claim_retry) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["committed", "duplicate"]
    assert len(list((archive.run_dir / "observations").glob("step-*/metadata.json"))) == 2

    _record_dom(archive, b"gap", step=2, attempt=0)
    archive.checkpoint_transcript(_transcript(manifest))
    codes = {item.code for item in verify_run(archive.run_dir, require_complete=False)}
    assert "noncontiguous_steps" in codes


def test_interrupted_write_is_detectable_and_capture_not_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = RunArchive.create(tmp_path, _manifest("INTERRUPTED-RUN"))
    real_atomic_write = archive_module._atomic_write

    def fail_request(path: Path, data: bytes, *, replace: bool) -> None:
        if path.name == "model-request.json":
            raise OSError("simulated crash boundary")
        real_atomic_write(path, data, replace=replace)

    monkeypatch.setattr(archive_module, "_atomic_write", fail_request)
    with pytest.raises(ArchiveError, match="failed to commit"):
        _record_dom(archive, b"never committed")
    codes = {item.code for item in verify_run(archive.run_dir, require_complete=False)}
    assert "uncommitted_model_call" in codes
    assert not list((archive.run_dir / "observations").glob("step-*/metadata.json"))
    leftover = archive.run_dir / ".transcript.json.crash.tmp"
    leftover.write_bytes(b"partial")
    assert "uncommitted_temp_file" in {item.code for item in verify_run(archive.run_dir, require_complete=False)}


def test_scorer_scan_returns_both_kinds_and_excludes_corruption_with_diagnostics(tmp_path: Path) -> None:
    dom_manifest = _manifest("SCORER-DOM")
    dom = RunArchive.create(tmp_path, dom_manifest)
    _record_dom(dom, b"DOM_GROUND_TRUTH")
    dom.checkpoint_transcript(_transcript(dom_manifest))

    ax_manifest = _manifest("SCORER-AX", "accessibility_tree")
    ax = RunArchive.create(tmp_path, ax_manifest)
    observation = canonical_json_bytes({"nodes": [{"name": "AX_GROUND_TRUTH"}]})
    ax.record_model_call(
        step=0,
        attempt=0,
        observation_kind="ax_tree",
        observation_bytes=observation,
        model_request_bytes=_ax_request(observation),
        metadata=_metadata("SCORER-AX", agent_class="accessibility_tree", completeness="partial", missing=["oopif"]),
    )
    ax.checkpoint_transcript(_transcript(ax_manifest))

    scan = scan_model_observations(tmp_path)
    assert {record.observation_kind for record in scan.records} == {"serialized_dom", "ax_tree"}
    assert any(b"GROUND_TRUTH" in record.observation_bytes for record in scan.records)
    assert next(record for record in scan.records if record.run_id == "SCORER-AX").capture_completeness == "partial"

    (dom.run_dir / "observations/step-000/observation.txt").write_bytes(b"corrupt")
    corrupted = scan_model_observations(tmp_path)
    assert {record.run_id for record in corrupted.records} == {"SCORER-AX"}
    assert "observation_hash_mismatch" in {item.code for item in corrupted.diagnostics}


def test_verifier_cli_is_machine_readable_and_nonzero_on_corruption(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _manifest("CLI-VERIFY-RUN")
    archive = RunArchive.create(tmp_path, manifest)
    _record_dom(archive, b"marker")
    archive.checkpoint_transcript(_transcript(manifest))
    assert archive_cli.main(["verify", str(archive.run_dir), "--allow-incomplete"]) == 0
    valid = json.loads(capsys.readouterr().out)
    assert valid["valid"] is True
    assert {item["code"] for item in valid["diagnostics"]} == {"incomplete_run"}

    (archive.run_dir / "observations/step-000/model-request.json").write_bytes(b"{}")
    assert archive_cli.main(["verify", str(archive.run_dir), "--allow-incomplete"]) == 1
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["valid"] is False
    assert "model_request_hash_mismatch" in {item["code"] for item in invalid["diagnostics"]}


def test_archive_1_0_fixture_remains_scorer_compatible(tmp_path: Path) -> None:
    fixture_path = Path(__file__).parents[1] / "fixtures" / "archive-call-1.0.json"
    fixture = json.loads(fixture_path.read_bytes())
    manifest = _manifest(fixture["run_id"])
    archive = RunArchive.create(tmp_path, manifest)
    observation = fixture["observation"].encode("utf-8")
    request = canonical_json_bytes(fixture["model_request"])
    assert hashlib.sha256(observation).hexdigest() == fixture["expected_observation_sha256"]
    assert hashlib.sha256(request).hexdigest() == fixture["expected_model_request_sha256"]
    archive.record_model_call(
        step=0,
        attempt=0,
        observation_kind=fixture["observation_kind"],
        observation_bytes=observation,
        model_request_bytes=request,
        metadata=_metadata(fixture["run_id"]),
    )
    scan = scan_model_observations(tmp_path)
    assert scan.diagnostics == []
    assert len(scan.records) == 1
    assert scan.records[0].observation_bytes == observation
