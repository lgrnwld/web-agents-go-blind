from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from webagents.capture.archive import RunArchive, canonical_json_bytes
from webagents.control.config import load_all_task_revisions, load_control_spec
from webagents.control.schemas import (
    ControlReceipt,
    ImplementationIdentity,
    ModelSpec,
    TaskIdentity,
)
from webagents.control.schemas import (
    MatrixRow as ControlMatrixRow,
)
from webagents.control.schemas import (
    TrialResult as ControlTrialResult,
)
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
from webagents.schemas import (
    ImplementationIdentity as RunImplementationIdentity,
)
from webagents.sweeps.config import ResolvedSweepBundle
from webagents.sweeps.scheduler import _eligible_control_reuse, execute_resolved_sweep
from webagents.sweeps.schemas import (
    AccessibilitySweepSpec,
    ResolvedAccessibilitySweep,
)

ROOT = Path(__file__).resolve().parents[3]
SHA = "b" * 64


def _bundle() -> ResolvedSweepBundle:
    control = load_control_spec(ROOT / "benchmarks/control/control.yaml")
    tasks = load_all_task_revisions(ROOT / "benchmarks/control/control.yaml", control)
    task = tasks[(control.tasks[0].id, control.tasks[0].active_revision)]
    model = ModelSpec(
        id="fixture-model",
        runner_model="openrouter/fixture/model-v1",
        expected_provider="openrouter",
        expected_model="fixture/model-v1",
    )
    spec = AccessibilitySweepSpec.model_validate(
        {
            "sweep_id": "test-ax-full",
            "control_receipt": "unused/control-receipt.json",
            "primary_model": model.runner_model,
            "reuse_level0_controls": False,
            "allocation": {"valid_trials_per_cell": 10, "max_infrastructure_retries": 1},
            "runtime": {"max_steps": 3, "timeout_seconds": 10.0},
            "analysis": {"randomization_seed": 7},
            "conditions": [
                {"id": "level-0", "kind": "flat", "expected_reachability": "present"},
                {"id": "rendered-canvas", "kind": "canvas", "expected_reachability": "absent"},
            ],
            "axes": [
                {
                    "id": "rendered-target",
                    "levels": [
                        {"id": "dom", "condition_id": "level-0"},
                        {"id": "canvas", "condition_id": "rendered-canvas"},
                    ],
                }
            ],
        }
    )
    implementation = ImplementationIdentity(
        source_sha256=SHA,
        transcript_schema_version="1.0",
        archive_schema_version="1.0",
        dom_framework="browser-use/test",
        dom_prompt_version="test",
        ax_framework="playwright/test",
        ax_prompt_version="test",
    )
    receipt = ControlReceipt(
        validation_id="TEST-CONTROL",
        control_spec_sha256=SHA,
        matrix_sha256=SHA,
        results_sha256=SHA,
        implementation=implementation,
        classes=["dom_extraction", "accessibility_tree"],
        models=[model.runner_model],
        tasks=[TaskIdentity(id=task.revision.task_id, revision=task.revision.revision)],
        policy={"repeats": 3, "required_successes": 3},
        passed_at=datetime.now(UTC),
    )
    resolved = ResolvedAccessibilitySweep(
        spec=spec,
        control_receipt=receipt,
        control_receipt_sha256=SHA,
        control_spec_sha256=SHA,
        implementation=implementation,
        models=[model],
        tasks=[task],
        conditions_sha256=hashlib.sha256(
            canonical_json_bytes(
                {
                    "conditions": [item.model_dump(mode="json") for item in spec.conditions],
                    "axes": [item.model_dump(mode="json") for item in spec.axes],
                }
            )
        ).hexdigest(),
        resolved_at=datetime.now(UTC),
    )
    return ResolvedSweepBundle(resolved=resolved, receipt_path=ROOT / "unused", project_root=ROOT)


class _MemoryHarness:
    def __init__(self, spec: object, tasks: object, conditions: object) -> None:
        del spec, tasks, conditions

    def __enter__(self) -> _MemoryHarness:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    def preflight(self) -> None:
        pass

    def reset(self, task: object) -> None:
        del task

    def condition_url(self, task: object, condition: object) -> str:
        del task
        return f"http://fixture.invalid/{condition.id}"  # type: ignore[attr-defined]

    def read_json_checker(self, task: object) -> object:
        del task
        return None


def _fake_runner(marker: str, answer: str, *, partial_capture: bool = False):
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
        canvas = page_url.endswith("rendered-canvas")
        visible = not canvas
        started = datetime.now(UTC)
        transcript = RunTranscript(
            run_id=run_id,
            agent_class="accessibility_tree",
            framework=FrameworkIdentity(name="playwright", version="1.61.0", commit="chromium-test"),
            input=RunInput(page_url=page_url, task_prompt=task_prompt, model=model),
            started_at=started,
            status="success" if visible else "failure",
        )
        manifest = RunManifest(
            run_id=run_id,
            agent_class="accessibility_tree",
            input=transcript.input,
            runtime=RuntimeIdentity(started_at=started, host_platform="test", python="3.12"),
            implementation=RunImplementationIdentity(
                repository_commit=None,
                source_state="unversioned",
                framework_name="playwright-cdp-accessibility",
                framework_version="1.61.0",
                framework_commit="chromium-test",
                prompt_version="fixture",
            ),
        )
        archive = RunArchive.create(archive_root, manifest)
        observation = canonical_json_bytes(
            {"nodes": [{"name": f"Source: {answer} {marker}" if visible else "Rendered target"}]}
        )
        request = canonical_json_bytes({"messages": [{"content": observation.decode()}]})
        captured = datetime.now(UTC)
        refs = archive.record_model_call(
            step=0,
            attempt=0,
            observation_kind="ax_tree",
            observation_bytes=observation,
            model_request_bytes=request,
            metadata=ModelCallMetadata(
                run_id=run_id,
                step=0,
                attempt=0,
                agent_class="accessibility_tree",
                observation_kind="ax_tree",
                captured_at=captured,
                model_call_started_at=captured,
                capture_completeness="partial" if partial_capture else "complete",
                missing_targets=["fixture-oopif"] if partial_capture else [],
            ),
        )
        response = answer if visible else "I cannot see the rendered value"
        transcript.steps = [
            RunStep(
                step=0,
                timestamp=captured,
                url=page_url,
                observation=refs.observation,
                model_request=refs.model_request,
                model_response=ModelResponse(text=response),
            )
        ]
        transcript.final = FinalResult(
            answer=answer if visible else None,
            success_claimed=visible,
            error=None if visible else response,
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


@pytest.mark.asyncio
async def test_full_scheduler_counts_unreachable_as_valid_and_emits_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle()
    task = bundle.resolved.tasks[0]
    monkeypatch.setattr("webagents.sweeps.scheduler.StructuralFixtureHarness", _MemoryHarness)
    report = await execute_resolved_sweep(
        bundle,
        archive_root=tmp_path / "artifacts",
        output_root=tmp_path / "sweeps",
        runner=_fake_runner(
            task.revision.level0.reachability_marker,
            task.revision.level0.checker.expected,
        ),
        preflight_credentials=False,
    )
    assert report.verdict == "complete"
    assert report.required_cells == 2
    assert report.complete_cells == 2
    assert report.valid_trial_count == 20
    baseline = next(item for item in report.aggregates if item.condition_id == "level-0")
    canvas = next(item for item in report.aggregates if item.condition_id == "rendered-canvas")
    assert baseline.reachability.successes == 10
    assert baseline.task_success.successes == 10
    assert canvas.reachability.successes == 0
    assert canvas.task_success.successes == 0
    assert canvas.self_reported_blindness.successes == 10
    output = tmp_path / "sweeps/test-ax-full"
    assert (output / "sweep-receipt.json").is_file()
    assert len((output / "matrix.jsonl").read_text().splitlines()) == 20
    assert len((output / "results.jsonl").read_text().splitlines()) == 20
    assert (output / "exclusions.jsonl").read_bytes() == b""


@pytest.mark.asyncio
async def test_exhausted_infrastructure_retries_leave_sweep_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle()
    task = bundle.resolved.tasks[0]
    monkeypatch.setattr("webagents.sweeps.scheduler.StructuralFixtureHarness", _MemoryHarness)
    good_runner = _fake_runner(task.revision.level0.reachability_marker, task.revision.level0.checker.expected)
    calls = 0

    async def partial_then_raise(*args: object, **kwargs: object) -> RunTranscript:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ConnectionError("temporary browser launch failure")
        return await good_runner(*args, **kwargs)  # type: ignore[arg-type]

    report = await execute_resolved_sweep(
        bundle,
        archive_root=tmp_path / "artifacts",
        output_root=tmp_path / "sweeps",
        runner=partial_then_raise,
        preflight_credentials=False,
    )
    assert report.verdict == "incomplete"
    assert "INFRASTRUCTURE_FAILURE" in report.blocking_classifications
    assert not (tmp_path / "sweeps/test-ax-full/sweep-receipt.json").exists()
    assert (tmp_path / "sweeps/test-ax-full/exclusions.jsonl").read_bytes()


@pytest.mark.asyncio
async def test_partial_capture_is_excluded_retried_and_blocks_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle()
    task = bundle.resolved.tasks[0]
    monkeypatch.setattr("webagents.sweeps.scheduler.StructuralFixtureHarness", _MemoryHarness)
    report = await execute_resolved_sweep(
        bundle,
        archive_root=tmp_path / "artifacts",
        output_root=tmp_path / "sweeps",
        runner=_fake_runner(
            task.revision.level0.reachability_marker,
            task.revision.level0.checker.expected,
            partial_capture=True,
        ),
        preflight_credentials=False,
    )
    assert report.verdict == "blocked"
    assert report.blocking_classifications == ["EVIDENCE_FAILURE"]
    assert report.valid_trial_count == 0
    assert report.attempt_count == 2
    output = tmp_path / "sweeps/test-ax-full"
    assert len((output / "exclusions.jsonl").read_text().splitlines()) == 2
    assert not (output / "sweep-receipt.json").exists()


@pytest.mark.asyncio
async def test_control_reuse_requires_verified_matching_ax_evidence(tmp_path: Path) -> None:
    initial = _bundle()
    resolved = initial.resolved.model_copy(
        update={"spec": initial.resolved.spec.model_copy(update={"reuse_level0_controls": True})}
    )
    control_dir = tmp_path / "control/CONTROL-REUSE"
    control_dir.mkdir(parents=True)
    bundle = ResolvedSweepBundle(
        resolved=resolved,
        receipt_path=control_dir / "control-receipt.json",
        project_root=ROOT,
    )
    task = resolved.tasks[0]
    model = resolved.models[0]
    run_id = "CONTROL-REUSE-AX-RUN"
    transcript = await _fake_runner(
        task.revision.level0.reachability_marker,
        task.revision.level0.checker.expected,
    )(
        "http://fixture.invalid/control-level-0",
        task.revision.level0.prompt,
        model.runner_model,
        run_id=run_id,
        max_steps=3,
        archive_root=tmp_path / "artifacts",
        timeout_seconds=10.0,
    )
    run_dir = tmp_path / "artifacts/runs" / run_id
    logical_id = "control-logical-trial"
    row = ControlMatrixRow(
        logical_trial_id=logical_id,
        class_id="accessibility_tree",
        runner="ax",
        task_id=task.revision.task_id,
        task_revision=task.revision.revision,
        task_definition_sha256=task.definition_sha256,
        model_id=model.id,
        runner_model=model.runner_model,
        repeat=1,
        schedule_index=0,
    )
    result = ControlTrialResult(
        logical_trial_id=logical_id,
        class_id="accessibility_tree",
        task_id=task.revision.task_id,
        task_revision=task.revision.revision,
        model_id=model.id,
        repeat=1,
        infrastructure_attempt=0,
        run_id=run_id,
        classification="PASS",
        runner_status="success",
        runner_success_claimed=True,
        archive_valid=True,
        reachable=True,
        capture_complete=True,
        checker_passed=True,
        manifest_sha256=hashlib.sha256((run_dir / "manifest.json").read_bytes()).hexdigest(),
        transcript_sha256=hashlib.sha256((run_dir / "transcript.json").read_bytes()).hexdigest(),
        started_at=transcript.started_at,
        finished_at=transcript.finished_at or datetime.now(UTC),
    )
    (control_dir / "resolved-spec.json").write_bytes(
        canonical_json_bytes({"control": {"policy": {"max_steps": 3, "timeout_seconds": 10.0}}})
    )
    (control_dir / "matrix.jsonl").write_bytes(canonical_json_bytes(row.model_dump(mode="json")) + b"\n")
    (control_dir / "results.jsonl").write_bytes(canonical_json_bytes(result.model_dump(mode="json")) + b"\n")

    eligible = _eligible_control_reuse(bundle, tmp_path / "artifacts")
    key = (task.revision.task_id, task.revision.revision, model.id)
    assert [item.run_id for item in eligible[key]] == [run_id]

    (run_dir / "observations/step-000/observation.json").write_bytes(b"mutated")
    assert _eligible_control_reuse(bundle, tmp_path / "artifacts") == {}
