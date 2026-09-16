from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml
from pydantic import ValidationError

from webagents.capture.archive import RunArchive, canonical_json_bytes
from webagents.control.cli import _validation_exit
from webagents.control.config import load_all_task_revisions, load_control_spec, load_task_revision
from webagents.control.harness import FixtureHarness, render_level0_page
from webagents.control.matrix import expand_matrix
from webagents.control.receipt import ControlAdmissionError, assert_control_admitted
from webagents.control.schemas import ControlPolicy, SweepIdentity
from webagents.control.validator import _run_id, validate_level0_control
from webagents.schemas import (
    CompletionRecord,
    FinalResult,
    FrameworkIdentity,
    ImplementationIdentity,
    ModelCallMetadata,
    RunInput,
    RunManifest,
    RunStep,
    RuntimeIdentity,
    RunTranscript,
)


def _write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _control_data(*, repeats: int = 1, ladder: list[int] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "classes": [
            {"id": "dom_extraction", "runner": "dom"},
            {"id": "accessibility_tree", "runner": "ax"},
        ],
        "models": [
            {
                "id": "fixture-model",
                "runner_model": "openrouter/fixture/model-v1",
                "expected_provider": "openrouter",
                "expected_model": "fixture/model-v1",
            }
        ],
        "tasks": [
            {
                "id": "read-known-value",
                "active_revision": 1,
                "simplification_ladder": ladder or [],
            }
        ],
        "policy": {
            "repeats": repeats,
            "required_successes": repeats,
            "max_infrastructure_retries": 1,
            "max_steps": 3,
            "timeout_seconds": 10.0,
            "schedule_seed": 42,
        },
        "harness": {"kind": "builtin", "host": "127.0.0.1", "port": 0, "health_path": "/health"},
    }


def _task_data(revision: int, prompt: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": "read-known-value",
        "revision": revision,
        "semantic_goal": "Read one value.",
        "level0": {
            "page_path": f"/cases/read-known-value/r{revision}/level-0",
            "reset_path": f"/cases/read-known-value/r{revision}/reset",
            "prompt": prompt or "Return the requested page value exactly.",
            "reachability_marker": f"CONTROL_READ_R{revision}",
            "checker": {"name": "final_answer_equals", "version": "1.0", "expected": "VALUE-123"},
            "fixture": {
                "kind": "read_value",
                "source_value": "VALUE-123",
                "source_label": "Source",
                "destination_label": "Destination",
                "submit_label": "Submit",
            },
        },
    }


def _write_fixture_specs(root: Path, *, repeats: int = 1, ladder: list[int] | None = None) -> Path:
    spec_path = root / "benchmarks/control/control.yaml"
    _write_yaml(spec_path, _control_data(repeats=repeats, ladder=ladder))
    _write_yaml(root / "benchmarks/tasks/read-known-value/r1.yaml", _task_data(1))
    for revision in ladder or []:
        _write_yaml(root / f"benchmarks/tasks/read-known-value/r{revision}.yaml", _task_data(revision))
    return spec_path


def _fake_runner(agent_class: Literal["dom_extraction", "accessibility_tree"], *, fail_revision: int | None = None):
    async def run(
        page_url: str,
        task_prompt: str,
        model: str,
        *,
        run_id: str,
        max_steps: int,
        archive_root: Path,
        timeout_seconds: float,
    ) -> RunTranscript:
        del max_steps, timeout_seconds
        revision_match = re.search(r"/r(\d+)/", page_url)
        assert revision_match is not None
        revision = int(revision_match.group(1))
        html = f"<span id=source>VALUE-123</span><span>CONTROL_READ_R{revision}</span>"
        success = revision != fail_revision
        answer = "VALUE-123" if success else "WRONG"
        started = datetime.now(UTC)
        framework = (
            FrameworkIdentity(version="0.13.7", commit="fixture")
            if agent_class == "dom_extraction"
            else FrameworkIdentity(name="playwright", version="1.61.0", commit="chromium-fixture")
        )
        transcript = RunTranscript(
            run_id=run_id,
            agent_class=agent_class,
            framework=framework,
            input=RunInput(page_url=page_url, task_prompt=task_prompt, model=model),
            started_at=started,
            status="success" if success else "failure",
        )
        manifest = RunManifest(
            run_id=run_id,
            agent_class=agent_class,
            input=transcript.input,
            runtime=RuntimeIdentity(started_at=started, host_platform="test", python="3.12"),
            implementation=ImplementationIdentity(
                repository_commit=None,
                source_state="unversioned",
                framework_name=framework.name,
                framework_version=framework.version,
                framework_commit=framework.commit,
                prompt_version="fixture",
            ),
        )
        archive = RunArchive.create(archive_root, manifest)
        if agent_class == "dom_extraction":
            observation = html.encode("utf-8")
            request_bytes = canonical_json_bytes(
                {"messages": [{"content": f"<browser_state>\n{html}\n</browser_state>"}]}
            )
            kind: Literal["serialized_dom", "ax_tree"] = "serialized_dom"
        else:
            observation = canonical_json_bytes({"nodes": [{"name": html}]})
            request_bytes = canonical_json_bytes({"messages": [{"content": observation.decode("utf-8")}]})
            kind = "ax_tree"
        captured = datetime.now(UTC)
        refs = archive.record_model_call(
            step=0,
            attempt=0,
            observation_kind=kind,
            observation_bytes=observation,
            model_request_bytes=request_bytes,
            metadata=ModelCallMetadata(
                run_id=run_id,
                step=0,
                attempt=0,
                agent_class=agent_class,
                observation_kind=kind,
                captured_at=captured,
                model_call_started_at=captured,
            ),
        )
        transcript.steps = [
            RunStep(
                step=0,
                timestamp=captured,
                url=page_url,
                observation=refs.observation,
                model_request=refs.model_request,
            )
        ]
        transcript.final = FinalResult(
            answer=answer,
            success_claimed=success,
            error=None if success else "fixture task failure",
        )
        transcript.finished_at = datetime.now(UTC)
        archive.checkpoint_transcript(transcript)
        transcript_bytes = (archive.run_dir / "transcript.json").read_bytes()
        archive.complete(
            CompletionRecord(
                run_id=run_id,
                status=transcript.status,
                finished_at=transcript.finished_at,
                transcript_sha256=hashlib.sha256(transcript_bytes).hexdigest(),
            )
        )
        return transcript

    return run


class _MemoryHarness:
    def __init__(self, spec: object, tasks: object) -> None:
        del spec, tasks
        self.base_url = "http://fixture.invalid"

    def __enter__(self) -> _MemoryHarness:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def preflight_task(self, task: object) -> None:
        del task

    def reset(self, task: object) -> None:
        del task

    def read_json_checker(self, task: object) -> object:
        del task
        return None


def test_control_roster_and_matrix_are_complete_and_deterministic(tmp_path: Path) -> None:
    spec_path = _write_fixture_specs(tmp_path, repeats=3)
    spec = load_control_spec(spec_path)
    tasks = load_all_task_revisions(spec_path, spec)
    active = [tasks[("read-known-value", 1)]]
    first = expand_matrix(spec, active, phase=0)
    second = expand_matrix(spec, active, phase=0)
    assert first == second
    assert len(first) == 2 * 1 * 1 * 3
    assert len({row.logical_trial_id for row in first}) == len(first)
    assert _run_id("control-run-20260830T010000Z", first[0], 0) == _run_id("control-run-20260830T010000Z", first[0], 0)
    assert _run_id("control-run-20260830T010000Z", first[0], 0) != _run_id("control-run-20260830T020000Z", first[0], 0)

    with pytest.raises(ValidationError, match="strict"):
        ControlPolicy(
            repeats=3,
            required_successes=2,
            max_infrastructure_retries=0,
            max_steps=3,
            timeout_seconds=1.0,
            schedule_seed=1,
        )


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("WEBAGENT_RUN_CONTROL_INTEGRATION") != "1",
    reason="set WEBAGENT_RUN_CONTROL_INTEGRATION=1 to bind the loopback fixture server",
)
def test_builtin_harness_resets_and_checks_copy_state() -> None:
    root = Path(__file__).resolve().parents[3]
    spec_path = root / "benchmarks/control/control.yaml"
    spec = load_control_spec(spec_path)
    tasks = load_all_task_revisions(spec_path, spec)
    copy_roster = next(item for item in spec.tasks if item.id == "copy-known-value")
    task = tasks[(copy_roster.id, copy_roster.active_revision)]
    with FixtureHarness(spec.harness, tasks.values()) as harness:
        harness.preflight_task(task)
        submit_url = harness.url(f"{task.revision.level0.page_path}/submit")
        payload = urllib.parse.urlencode({"destination": "BRAVO-4826"}).encode()
        urllib.request.urlopen(urllib.request.Request(submit_url, data=payload, method="POST"), timeout=2).read()
        assert harness.read_json_checker(task) == "BRAVO-4826"
        harness.reset(task)
        assert harness.read_json_checker(task) is None


def test_saved_control_page_is_terminal_without_repeating_target_evidence() -> None:
    root = Path(__file__).resolve().parents[3]
    spec_path = root / "benchmarks/control/control.yaml"
    spec = load_control_spec(spec_path)
    tasks = load_all_task_revisions(spec_path, spec)
    copy_roster = next(item for item in spec.tasks if item.id == "copy-known-value")
    task = tasks[(copy_roster.id, copy_roster.active_revision)]

    page = render_level0_page(task, saved=True)

    assert "Saved" in page
    assert task.revision.level0.reachability_marker not in page
    assert task.revision.level0.fixture.source_value not in page
    assert "<form" not in page
    assert "<input" not in page
    assert "<button" not in page


def test_large_target_copy_revision_is_explicit_and_prior_revisions_are_unchanged() -> None:
    root = Path(__file__).resolve().parents[3]
    spec_path = root / "benchmarks/control/control.yaml"
    spec = load_control_spec(spec_path)
    tasks = load_all_task_revisions(spec_path, spec)
    copy_roster = next(item for item in spec.tasks if item.id == "copy-known-value")

    assert copy_roster.active_revision == 5
    assert copy_roster.simplification_ladder == []
    revision_5 = tasks[(copy_roster.id, 5)]
    revision_4 = load_task_revision(spec_path, copy_roster, 4)
    revision_3 = load_task_revision(spec_path, copy_roster, 3)
    assert revision_5.revision.level0.fixture.kind == "copy_value_form_large_target"
    assert revision_4.revision.level0.fixture.kind == "copy_value_form_large_target"
    assert revision_3.revision.level0.fixture.kind == "copy_value_form"
    assert "press Enter" in revision_5.revision.level0.prompt
    assert "click Submit" in revision_4.revision.level0.prompt

    calibrated_page = render_level0_page(revision_5)
    prior_page = render_level0_page(revision_3)
    assert 'data-fixture-presentation="large-target-v1"' in calibrated_page
    assert 'class="large-target-form"' in calibrated_page
    assert "min-height: 56px" in calibrated_page
    assert 'data-fixture-presentation="large-target-v1"' not in prior_page
    assert 'class="large-target-form"' not in prior_page


@pytest.mark.asyncio
async def test_complete_gate_emits_receipt_and_admission_rehashes_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("webagents.control.validator.FixtureHarness", _MemoryHarness)
    spec_path = _write_fixture_specs(tmp_path)
    runners = {
        "dom": _fake_runner("dom_extraction"),
        "ax": _fake_runner("accessibility_tree"),
    }
    report = await validate_level0_control(
        spec_path,
        archive_root=tmp_path / "artifacts",
        output_root=tmp_path / "control-results",
        validation_id="CONTROL-PASS",
        runners=runners,
        preflight_credentials=False,
    )
    assert report.verdict == "pass"
    assert report.trial_count == 2
    validation_dir = tmp_path / "control-results/CONTROL-PASS"
    receipt_path = validation_dir / "control-receipt.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_bytes())
    identity = SweepIdentity(
        control_spec_sha256=receipt["control_spec_sha256"],
        implementation=receipt["implementation"],
        classes=receipt["classes"],
        models=receipt["models"],
        tasks=receipt["tasks"],
        policy=receipt["policy"],
    )
    assert_control_admitted(receipt_path, identity)

    with (validation_dir / "results.jsonl").open("ab") as handle:
        handle.write(b"mutation")
    with pytest.raises(ControlAdmissionError, match="evidence"):
        assert_control_admitted(receipt_path, identity)


@pytest.mark.asyncio
async def test_task_failure_advances_uniformly_to_simpler_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("webagents.control.validator.FixtureHarness", _MemoryHarness)
    spec_path = _write_fixture_specs(tmp_path, ladder=[2])
    runners = {
        "dom": _fake_runner("dom_extraction", fail_revision=1),
        "ax": _fake_runner("accessibility_tree", fail_revision=1),
    }
    report = await validate_level0_control(
        spec_path,
        archive_root=tmp_path / "artifacts",
        output_root=tmp_path / "control-results",
        validation_id="CONTROL-SIMPLIFY",
        runners=runners,
        preflight_credentials=False,
    )
    assert report.verdict == "pass"
    assert [(task.task_revision, task.admitted) for task in report.task_results] == [(1, False), (2, True)]
    assert [(task.id, task.revision) for task in report.active_tasks] == [("read-known-value", 2)]
    matrix_lines = (tmp_path / "control-results/CONTROL-SIMPLIFY/matrix.jsonl").read_text().splitlines()
    assert len(matrix_lines) == 4


@pytest.mark.asyncio
async def test_infrastructure_retries_block_without_simplifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("webagents.control.validator.FixtureHarness", _MemoryHarness)
    spec_path = _write_fixture_specs(tmp_path, ladder=[2])

    async def unavailable(*args: object, **kwargs: object) -> RunTranscript:
        del args, kwargs
        raise ConnectionError("provider unavailable")

    report = await validate_level0_control(
        spec_path,
        archive_root=tmp_path / "artifacts",
        output_root=tmp_path / "control-results",
        validation_id="CONTROL-BLOCKED",
        runners={"dom": unavailable, "ax": unavailable},
        preflight_credentials=False,
    )
    assert report.verdict == "blocked"
    assert report.blocking_classifications == ["INFRASTRUCTURE_FAILURE"]
    assert report.trial_count == 4
    assert report.task_results[0].next_revision is None
    assert [(task.id, task.revision) for task in report.active_tasks] == [("read-known-value", 1)]
    assert _validation_exit(report) == 2
    assert not (tmp_path / "control-results/CONTROL-BLOCKED/control-receipt.json").exists()


@pytest.mark.asyncio
async def test_unsafe_validation_id_is_rejected_before_output(tmp_path: Path) -> None:
    spec_path = _write_fixture_specs(tmp_path)
    with pytest.raises(ValueError, match="unsafe"):
        await validate_level0_control(
            spec_path,
            archive_root=tmp_path / "artifacts",
            output_root=tmp_path / "control-results",
            validation_id="../escape",
            runners={"dom": _fake_runner("dom_extraction"), "ax": _fake_runner("accessibility_tree")},
            preflight_credentials=False,
        )
    assert not (tmp_path / "escape").exists()
