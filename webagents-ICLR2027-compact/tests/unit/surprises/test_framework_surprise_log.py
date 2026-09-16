from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from webagents.capture.archive import RunArchive, canonical_json_bytes
from webagents.schemas import (
    CompletionRecord,
    FinalResult,
    FrameworkIdentity,
    ModelCallMetadata,
    ModelResponse,
    RunInput,
    RunManifest,
    RunStep,
    RuntimeIdentity,
    RunTranscript,
)
from webagents.schemas import ImplementationIdentity as RunImplementationIdentity
from webagents.surprises.log import (
    SurpriseLogError,
    add_sweep_candidate,
    adjudicate_surprise,
    append_surprise_event,
    correct_surprise,
    event_sha256,
    load_expectation_registry,
    verify_surprise_log,
)
from webagents.surprises.render import render_surprise_views
from webagents.surprises.schemas import FrameworkSurpriseEvent
from webagents.sweeps.schemas import SurpriseCandidate, SweepTrialResult

ROOT = Path(__file__).resolve().parents[3]
REGISTRY = ROOT / "evidence/framework-surprises/expectations.yaml"
MARKER = "SURPRISE_MARKER_42"
SOURCE_SHA = "a" * 64
FIXTURE_SHA = "b" * 64
LOGGED_AT = datetime(2026, 8, 20, 15, 0, tzinfo=UTC)


def _create_run(
    archive_root: Path,
    run_id: str,
    *,
    started_at: datetime,
    marker_present: bool = True,
    framework_version: str = "0.13.7",
) -> Path:
    input_value = RunInput(
        page_url="http://127.0.0.1:8123/iframe-cross-depth-1",
        task_prompt="Read the known synthetic value.",
        model="openrouter/fixture/model-v1",
    )
    implementation = RunImplementationIdentity(
        repository_commit="fixture-commit",
        source_state="versioned",
        framework_name="browser-use",
        framework_version=framework_version,
        framework_commit=f"browser-use-{framework_version}",
        playwright_version="1.61.0",
        browser_version="149.0.7827.55",
        prompt_version="dom-observation-v1",
    )
    manifest = RunManifest(
        run_id=run_id,
        agent_class="dom_extraction",
        input=input_value,
        runtime=RuntimeIdentity(started_at=started_at, host_platform="fixture-macos", python="3.12"),
        implementation=implementation,
    )
    archive = RunArchive.create(archive_root, manifest)
    observation_text = f"Target: {MARKER}" if marker_present else "Cross-origin frame unavailable"
    captured = started_at + timedelta(seconds=1)
    refs = archive.record_model_call(
        step=0,
        attempt=0,
        observation_kind="serialized_dom",
        observation_bytes=observation_text.encode(),
        model_request_bytes=canonical_json_bytes(
            {"messages": [{"content": f"<browser_state>\n{observation_text}\n</browser_state>"}]}
        ),
        metadata=ModelCallMetadata(
            run_id=run_id,
            step=0,
            attempt=0,
            agent_class="dom_extraction",
            observation_kind="serialized_dom",
            captured_at=captured,
            model_call_started_at=captured,
            transform={"name": "browser-use-dom-serialization", "version": "1"},
        ),
    )
    finished = started_at + timedelta(seconds=2)
    transcript = RunTranscript(
        run_id=run_id,
        agent_class="dom_extraction",
        framework=FrameworkIdentity(
            name="browser-use", version=framework_version, commit=implementation.framework_commit
        ),
        input=input_value,
        started_at=started_at,
        finished_at=finished,
        status="success",
        steps=[
            RunStep(
                step=0,
                timestamp=captured,
                url=input_value.page_url,
                observation=refs.observation,
                model_request=refs.model_request,
                model_response=ModelResponse(
                    text="fixture answer",
                    provider_metadata={
                        "id": f"response-{run_id}",
                        "provider": "openrouter",
                        "model": "fixture/model-v1",
                    },
                ),
            )
        ],
        final=FinalResult(answer="fixture answer", success_claimed=True),
    )
    archive.checkpoint_transcript(transcript)
    transcript_bytes = (archive.run_dir / "transcript.json").read_bytes()
    archive.complete(
        CompletionRecord(
            run_id=run_id,
            status="success",
            finished_at=finished,
            transcript_sha256=hashlib.sha256(transcript_bytes).hexdigest(),
        )
    )
    return archive.run_dir


def _create_sweep(
    archive_root: Path,
    run_dir: Path,
    *,
    observed_at: datetime,
    sweep_id: str = "fixture-dom-light",
    logical_id: str = "1" * 24,
) -> Path:
    sweep_dir = archive_root / f"sweeps/dom-light/{sweep_id}"
    sweep_dir.mkdir(parents=True)
    resolved = {
        "implementation": {"source_sha256": SOURCE_SHA},
        "spec": {
            "agent_class": "dom_extraction",
            "sweep_id": sweep_id,
            "axes": [
                {
                    "id": "iframe-cross-origin-depth",
                    "levels": [
                        {"id": "depth-0", "condition_id": "level-0"},
                        {"id": "depth-1", "condition_id": "iframe-cross-depth-1"},
                    ],
                }
            ],
        },
        "tasks": [
            {
                "revision": {
                    "task_id": "read-known-value",
                    "revision": 1,
                    "level0": {"reachability_marker": MARKER},
                }
            }
        ],
    }
    matrix = {
        "logical_trial_id": logical_id,
        "task_id": "read-known-value",
        "task_revision": 1,
        "task_definition_sha256": FIXTURE_SHA,
        "model_id": "fixture-model",
        "runner_model": "openrouter/fixture/model-v1",
        "condition_id": "iframe-cross-depth-1",
    }
    manifest_bytes = (run_dir / "manifest.json").read_bytes()
    transcript_bytes = (run_dir / "transcript.json").read_bytes()
    result = SweepTrialResult(
        logical_trial_id=logical_id,
        allocation_role="confirmation",
        task_id="read-known-value",
        task_revision=1,
        model_id="fixture-model",
        condition_id="iframe-cross-depth-1",
        repeat=1,
        infrastructure_attempt=0,
        run_id=run_dir.name,
        classification="VALID_OUTCOME",
        counts_toward_cell=True,
        runner_status="success",
        runner_success_claimed=True,
        archive_valid=True,
        reachable=True,
        task_success=True,
        self_reported_blindness=False,
        capture_complete=True,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        transcript_sha256=hashlib.sha256(transcript_bytes).hexdigest(),
        started_at=observed_at - timedelta(seconds=2),
        finished_at=observed_at,
    )
    candidate = SurpriseCandidate(
        observed_at=observed_at,
        sweep_id=sweep_id,
        logical_trial_id=logical_id,
        run_id=run_dir.name,
        task_id="read-known-value",
        model_id="fixture-model",
        condition_id="iframe-cross-depth-1",
        expectation="absent",
        observed_reachable=True,
        summary="DOM expectation was absent; exact serialized observation recorded present.",
        manifest_sha256=result.manifest_sha256 or "",
    )
    (sweep_dir / "resolved-spec.json").write_bytes(canonical_json_bytes(resolved))
    (sweep_dir / "conditions.json").write_bytes(canonical_json_bytes({"inventory": "fixture"}))
    (sweep_dir / "matrix.jsonl").write_bytes(canonical_json_bytes(matrix) + b"\n")
    (sweep_dir / "results.jsonl").write_bytes(canonical_json_bytes(result.model_dump(mode="json")) + b"\n")
    (sweep_dir / "surprise-candidates.jsonl").write_bytes(
        canonical_json_bytes(candidate.model_dump(mode="json")) + b"\n"
    )
    return sweep_dir


def _add_candidate(tmp_path: Path) -> tuple[Path, Path, Path, FrameworkSurpriseEvent]:
    archive_root = tmp_path / "artifacts"
    observed = datetime(2026, 8, 20, 14, 22, 31, tzinfo=UTC)
    run_dir = _create_run(archive_root, "DOM6-FIXTURE-SOURCE", started_at=observed - timedelta(seconds=2))
    sweep_dir = _create_sweep(archive_root, run_dir, observed_at=observed)
    log_path = tmp_path / "evidence/framework-surprises/events.jsonl"
    event = add_sweep_candidate(
        expectation_id="dom-cross-origin-opaque",
        sweep_dir=sweep_dir,
        run_dir=run_dir,
        archive_root=archive_root,
        registry_path=REGISTRY,
        log_path=log_path,
        summary="Browser Use exposed the synthetic cross-origin target marker.",
        reporter="test-researcher",
        logged_at=LOGGED_AT,
    )
    return archive_root, run_dir, log_path, event


def test_registry_is_frozen_and_candidate_requires_verified_bound_evidence(tmp_path: Path) -> None:
    registry = load_expectation_registry(REGISTRY)
    assert registry.expectation_set == "structural-v1"
    assert registry.frozen_at == datetime(2026, 8, 16, tzinfo=UTC)

    archive_root, _, log_path, event = _add_candidate(tmp_path)
    assert event.event_id == "SURPRISE-20260820-001"
    assert event.observed_date == date(2026, 8, 20)
    assert event.marker_assertion.observed_present is True
    assert event.marker_assertion.expected_present is False
    assert {item.role for item in event.evidence} >= {
        "manifest",
        "observation",
        "sweep_resolved_spec",
        "sweep_matrix",
        "sweep_candidate",
    }
    report = verify_surprise_log(log_path, archive_root=archive_root, registry_path=REGISTRY)
    assert report.valid, report.diagnostics
    assert report.candidate_count == report.unresolved_count == 1

    with pytest.raises(SurpriseLogError, match="duplicate event ID"):
        append_surprise_event(event, log_path=log_path)


def test_confirmed_adjudication_is_append_only_and_views_are_deterministic(tmp_path: Path) -> None:
    archive_root, _, log_path, candidate = _add_candidate(tmp_path)
    original = log_path.read_bytes()
    reproduction = _create_run(
        archive_root,
        "DOM7-FIXTURE-REPRODUCTION",
        started_at=datetime(2026, 8, 20, 14, 40, tzinfo=UTC),
    )
    adjudication = adjudicate_surprise(
        candidate.event_id,
        status="confirmed",
        log_path=log_path,
        registry_path=REGISTRY,
        archive_root=archive_root,
        reproductions_dir=log_path.parent / "reproductions",
        reporter="test-researcher",
        summary="The isolated reproduction exposed the same cross-origin marker.",
        reproduction_run_dir=reproduction,
        paper_disposition="results_and_limitations",
        logged_at=LOGGED_AT + timedelta(hours=1),
    )
    assert log_path.read_bytes().startswith(original)
    assert adjudication.status == "confirmed"
    assert adjudication.updates == candidate.event_id
    assert adjudication.previous_event_sha256 == candidate.event_sha256
    assert (log_path.parent / "reproductions" / f"{candidate.event_id}.json").is_file()

    report = verify_surprise_log(log_path, archive_root=archive_root, registry_path=REGISTRY)
    assert report.valid, report.diagnostics
    assert report.event_count == 2
    assert report.confirmed_count == 1
    assert report.unresolved_count == 0

    generated = log_path.parent / "generated"
    render_surprise_views(log_path, output_dir=generated)
    first = {path.name: path.read_bytes() for path in generated.iterdir()}
    render_surprise_views(log_path, output_dir=generated)
    second = {path.name: path.read_bytes() for path in generated.iterdir()}
    assert first == second
    assert b"confirmed" in first["limitations-table.md"]
    assert b"0.13.7" in first["framework-matrix.csv"]


def test_verifier_detects_historical_mutation_and_evidence_hash_mismatch(tmp_path: Path) -> None:
    archive_root, run_dir, log_path, _ = _add_candidate(tmp_path)
    original = log_path.read_bytes()
    log_path.write_bytes(original.replace(b"Browser Use exposed", b"Browser use exposed", 1))
    report = verify_surprise_log(log_path, archive_root=archive_root, registry_path=REGISTRY)
    assert not report.valid
    assert "chain_event_mismatch" in {item.code for item in report.diagnostics}

    log_path.write_bytes(original)
    observation = run_dir / "observations/step-000/observation.txt"
    observation.write_bytes(observation.read_bytes() + b" changed")
    report = verify_surprise_log(log_path, archive_root=archive_root, registry_path=REGISTRY)
    assert not report.valid
    codes = {item.code for item in report.diagnostics}
    assert "evidence_hash_mismatch" in codes


def test_partial_capture_is_surfaced_explicitly(tmp_path: Path) -> None:
    archive_root, run_dir, log_path, _ = _add_candidate(tmp_path)
    metadata_path = run_dir / "observations/step-000/metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["capture_completeness"] = "partial"
    metadata["missing_targets"] = ["cross-origin-frame"]
    metadata_path.write_bytes(canonical_json_bytes(metadata))
    report = verify_surprise_log(log_path, archive_root=archive_root, registry_path=REGISTRY)
    assert not report.valid
    codes = {item.code for item in report.diagnostics}
    assert "evidence_hash_mismatch" in codes
    assert any("capture" in code for code in codes)


def test_schema_rejects_wrong_observed_date_and_secret_summary(tmp_path: Path) -> None:
    archive_root, run_dir, log_path, event = _add_candidate(tmp_path)
    invalid = event.model_dump(mode="json")
    invalid["observed_date"] = "2026-08-19"
    with pytest.raises(ValidationError, match="observed_date"):
        FrameworkSurpriseEvent.model_validate_json(canonical_json_bytes(invalid))

    sweep_dir = archive_root / "sweeps/dom-light/fixture-dom-light"
    second_log = tmp_path / "second/events.jsonl"
    with pytest.raises(SurpriseLogError, match="secret-bearing"):
        add_sweep_candidate(
            expectation_id="dom-cross-origin-opaque",
            sweep_dir=sweep_dir,
            run_dir=run_dir,
            archive_root=archive_root,
            registry_path=REGISTRY,
            log_path=second_log,
            summary="See https://researcher:password@example.test/private for the unexpected result.",
            reporter="test-researcher",
            logged_at=LOGGED_AT,
        )


def test_noncanonical_json_and_invalid_update_links_fail_verification(tmp_path: Path) -> None:
    archive_root, _, log_path, event = _add_candidate(tmp_path)
    value = json.loads(log_path.read_bytes())
    log_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    report = verify_surprise_log(log_path, archive_root=archive_root, registry_path=REGISTRY)
    assert not report.valid
    assert "invalid_event" in {item.code for item in report.diagnostics}

    log_path.write_bytes(canonical_json_bytes(event.model_dump(mode="json")) + b"\n")
    invalid_value = event.model_dump(mode="json")
    invalid_value.update(
        {
            "event_id": "SURPRISE-20260820-002",
            "event_type": "adjudication",
            "status": "awaiting_followup",
            "updates": "SURPRISE-20260820-999",
            "summary": "This update points to an event that does not exist.",
            "previous_event_sha256": event.event_sha256,
            "event_sha256": "0" * 64,
        }
    )
    invalid_link = FrameworkSurpriseEvent.model_validate_json(canonical_json_bytes(invalid_value))
    invalid_link = invalid_link.model_copy(update={"event_sha256": event_sha256(invalid_link)})
    with pytest.raises(SurpriseLogError, match="already present"):
        append_surprise_event(invalid_link, log_path=log_path)


def test_correction_appends_and_preserves_superseded_bytes(tmp_path: Path) -> None:
    archive_root, _, log_path, candidate = _add_candidate(tmp_path)
    original = log_path.read_bytes()
    correction = correct_surprise(
        candidate.event_id,
        log_path=log_path,
        registry_path=REGISTRY,
        reporter="test-researcher",
        summary="Corrected paper disposition after evidence review; raw behavior is unchanged.",
        paper_disposition="limitations",
        logged_at=LOGGED_AT + timedelta(hours=1),
    )
    assert log_path.read_bytes().startswith(original)
    assert correction.event_type == "correction"
    assert correction.supersedes == candidate.event_id
    assert correction.updates == candidate.event_id
    report = verify_surprise_log(log_path, archive_root=archive_root, registry_path=REGISTRY)
    assert report.valid, report.diagnostics


def test_framework_revision_flip_appears_as_two_matrix_identities(tmp_path: Path) -> None:
    archive_root, _, log_path, _ = _add_candidate(tmp_path)
    observed = datetime(2026, 8, 20, 16, 0, tzinfo=UTC)
    second_run = _create_run(
        archive_root,
        "DOM7-FIXTURE-NEW-FRAMEWORK",
        started_at=observed - timedelta(seconds=2),
        framework_version="0.14.0",
    )
    second_sweep = _create_sweep(
        archive_root,
        second_run,
        observed_at=observed,
        sweep_id="fixture-dom-light-upgrade",
        logical_id="2" * 24,
    )
    add_sweep_candidate(
        expectation_id="dom-cross-origin-opaque",
        sweep_dir=second_sweep,
        run_dir=second_run,
        archive_root=archive_root,
        registry_path=REGISTRY,
        log_path=log_path,
        summary="The upgraded Browser Use fixture also exposed the cross-origin marker.",
        reporter="test-researcher",
        logged_at=LOGGED_AT + timedelta(hours=2),
    )
    report = verify_surprise_log(log_path, archive_root=archive_root, registry_path=REGISTRY)
    assert report.valid, report.diagnostics
    generated = log_path.parent / "generated"
    render_surprise_views(log_path, output_dir=generated)
    matrix = (generated / "framework-matrix.csv").read_text()
    assert "0.13.7" in matrix
    assert "0.14.0" in matrix
