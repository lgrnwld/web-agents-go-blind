from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from webagents.capture.archive import RunArchive, canonical_json_bytes
from webagents.control.config import load_all_task_revisions, load_control_spec
from webagents.control.schemas import (
    ControlReceipt,
    ImplementationIdentity,
    ModelSpec,
    TaskIdentity,
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
from webagents.sweeps.config import (
    ResolvedDomSweepBundle,
    load_dom_light_spec,
    load_sweep_spec,
)
from webagents.sweeps.dom_scheduler import execute_resolved_dom_sweep
from webagents.sweeps.matrix import expand_dom_light_matrix, validate_dom_light_matrix
from webagents.sweeps.schemas import DomLightSweepSpec, ResolvedDomLightSweep

ROOT = Path(__file__).resolve().parents[3]
SHA = "c" * 64


def _models() -> list[ModelSpec]:
    return [
        ModelSpec(
            id="primary-model",
            runner_model="openrouter/fixture/primary-v1",
            expected_provider="openrouter",
            expected_model="fixture/primary-v1",
        ),
        ModelSpec(
            id="confirming-a",
            runner_model="openrouter/fixture/confirming-a-v1",
            expected_provider="openrouter",
            expected_model="fixture/confirming-a-v1",
        ),
        ModelSpec(
            id="confirming-b",
            runner_model="openrouter/fixture/confirming-b-v1",
            expected_provider="openrouter",
            expected_model="fixture/confirming-b-v1",
        ),
    ]


def _resolved(*, miniature: bool) -> ResolvedDomLightSweep:
    control = load_control_spec(ROOT / "benchmarks/control/control.yaml")
    tasks = load_all_task_revisions(ROOT / "benchmarks/control/control.yaml", control)
    task = tasks[(control.tasks[0].id, control.tasks[0].active_revision)]
    models = _models()
    if miniature:
        spec = DomLightSweepSpec.model_validate(
            {
                "sweep_id": "test-dom-light",
                "control_receipt": "unused/control-receipt.json",
                "primary_model": models[0].runner_model,
                "primary_model_rationale": (
                    "Frozen common fixture model selected before observing containment outcomes."
                ),
                "reuse_level0_controls": False,
                "allocation": {
                    "primary_valid_trials_per_cell": 10,
                    "additional_valid_trials_per_cell": 1,
                    "max_infrastructure_retries": 1,
                },
                "runtime": {"max_steps": 3, "timeout_seconds": 10.0},
                "analysis": {"randomization_seed": 17},
                "conditions": [
                    {
                        "id": "level-0",
                        "kind": "flat",
                        "expected_reachability": "present",
                        "dom_expected_reachability": "present",
                    },
                    {
                        "id": "iframe-cross-depth-1",
                        "kind": "iframe",
                        "iframe_origin": "cross",
                        "iframe_depth": 1,
                        "expected_reachability": "absent",
                        "dom_expected_reachability": "absent",
                    },
                ],
                "axes": [
                    {
                        "id": "iframe-cross-origin-depth",
                        "levels": [
                            {"id": "depth-0", "condition_id": "level-0"},
                            {"id": "depth-1", "condition_id": "iframe-cross-depth-1"},
                        ],
                    }
                ],
            }
        )
    else:
        spec = load_dom_light_spec(ROOT / "benchmarks/sweeps/dom-extraction-light.yaml")
        spec = spec.model_copy(update={"primary_model": models[0].runner_model})
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
        validation_id="TEST-DOM-CONTROL",
        control_spec_sha256=SHA,
        matrix_sha256=SHA,
        results_sha256=SHA,
        implementation=implementation,
        classes=["dom_extraction", "accessibility_tree"],
        models=[model.runner_model for model in models],
        tasks=[TaskIdentity(id=task.revision.task_id, revision=task.revision.revision)],
        policy={"repeats": 3, "required_successes": 3},
        passed_at=datetime.now(UTC),
    )
    condition_payload = {
        "conditions": [item.model_dump(mode="json") for item in spec.conditions],
        "axes": [item.model_dump(mode="json") for item in spec.axes],
    }
    return ResolvedDomLightSweep(
        spec=spec,
        control_receipt=receipt,
        control_receipt_sha256=SHA,
        control_spec_sha256=SHA,
        implementation=implementation,
        primary_model=models[0],
        additional_models=models[1:],
        tasks=[task],
        conditions_sha256=hashlib.sha256(canonical_json_bytes(condition_payload)).hexdigest(),
        resolved_at=datetime.now(UTC),
    )


def test_shared_inventory_and_asymmetric_matrix_are_exact() -> None:
    ax = load_sweep_spec(ROOT / "benchmarks/sweeps/accessibility-tree-full.yaml")
    dom = load_dom_light_spec(ROOT / "benchmarks/sweeps/dom-extraction-light.yaml")
    assert ax.conditions == dom.conditions
    assert ax.axes == dom.axes

    resolved = _resolved(miniature=False)
    rows = expand_dom_light_matrix(resolved)
    assert len(rows) == 1 * 11 * (10 + 2)
    counts = Counter((row.allocation_role, row.model_id, row.condition_id) for row in rows)
    assert {value for (role, _, _), value in counts.items() if role == "primary"} == {10}
    assert {value for (role, _, _), value in counts.items() if role == "confirmation"} == {1}
    assert {row.model_id for row in rows if row.allocation_role == "confirmation"} == {
        "confirming-a",
        "confirming-b",
    }
    assert max(row.schedule_index for row in rows if row.allocation_role == "confirmation") > 11
    with pytest.raises(ValueError, match="expected"):
        validate_dom_light_matrix(resolved, rows[:-1])


def test_dom_schema_rejects_adaptive_or_underpowered_allocation() -> None:
    spec = load_dom_light_spec(ROOT / "benchmarks/sweeps/dom-extraction-light.yaml")
    invalid = spec.model_dump(mode="json")
    invalid["allocation"]["additional_valid_trials_per_cell"] = 2
    with pytest.raises(ValidationError, match="exactly one"):
        DomLightSweepSpec.model_validate(invalid)


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


def _fake_dom_runner(marker: str, answer: str):
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
        cross = page_url.endswith("iframe-cross-depth-1")
        divergent_model = model.endswith("confirming-b-v1")
        visible = not cross or divergent_model
        started = datetime.now(UTC)
        transcript = RunTranscript(
            run_id=run_id,
            agent_class="dom_extraction",
            framework=FrameworkIdentity(name="browser-use", version="0.13.7", commit="fixture"),
            input=RunInput(page_url=page_url, task_prompt=task_prompt, model=model),
            started_at=started,
            status="success" if visible else "failure",
        )
        manifest = RunManifest(
            run_id=run_id,
            agent_class="dom_extraction",
            input=transcript.input,
            runtime=RuntimeIdentity(started_at=started, host_platform="test", python="3.12"),
            implementation=RunImplementationIdentity(
                repository_commit=None,
                source_state="unversioned",
                framework_name="browser-use",
                framework_version="0.13.7",
                framework_commit="fixture",
                prompt_version="fixture",
            ),
        )
        archive = RunArchive.create(archive_root, manifest)
        observation_text = f"Source: {answer} {marker}" if visible else "Cross-origin frame unavailable"
        observation = observation_text.encode()
        request = canonical_json_bytes(
            {"messages": [{"content": f"<browser_state>\n{observation_text}\n</browser_state>"}]}
        )
        captured = datetime.now(UTC)
        refs = archive.record_model_call(
            step=0,
            attempt=0,
            observation_kind="serialized_dom",
            observation_bytes=observation,
            model_request_bytes=request,
            metadata=ModelCallMetadata(
                run_id=run_id,
                step=0,
                attempt=0,
                agent_class="dom_extraction",
                observation_kind="serialized_dom",
                captured_at=captured,
                model_call_started_at=captured,
            ),
        )
        response = answer if visible else "I cannot access the cross-origin target"
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
async def test_dom_scheduler_keeps_confirmations_separate_and_logs_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = _resolved(miniature=True)
    bundle = ResolvedDomSweepBundle(
        resolved=resolved,
        receipt_path=ROOT / "unused",
        project_root=ROOT,
    )
    task = resolved.tasks[0]
    monkeypatch.setattr("webagents.sweeps.dom_scheduler.StructuralFixtureHarness", _MemoryHarness)
    report = await execute_resolved_dom_sweep(
        bundle,
        archive_root=tmp_path / "artifacts",
        output_root=tmp_path / "sweeps",
        runner=_fake_dom_runner(
            task.revision.level0.reachability_marker,
            task.revision.level0.checker.expected,
        ),
        preflight_credentials=False,
    )
    assert report.verdict == "complete"
    assert report.primary_required_cells == 2
    assert report.primary_complete_cells == 2
    assert report.confirmation_required_cells == 4
    assert report.confirmation_complete_cells == 4
    assert report.valid_trial_count == 24
    assert all(cell.valid_trials == 10 for cell in report.primary_aggregates)
    assert len(report.model_confirmations) == 4
    assert report.divergent_confirmation_count == 1
    divergent = next(item for item in report.model_confirmations if item.divergent)
    assert divergent.model_id == "confirming-b"
    assert divergent.condition_id == "iframe-cross-depth-1"
    assert report.surprise_candidate_count == 2

    output = tmp_path / "sweeps/test-dom-light"
    assert (output / "sweep-receipt.json").is_file()
    assert len((output / "matrix.jsonl").read_text().splitlines()) == 24
    assert len((output / "results.jsonl").read_text().splitlines()) == 24
    assert len((output / "surprise-candidates.jsonl").read_text().splitlines()) == 2
    assert b"wilson" not in (output / "model-confirmations.json").read_bytes().lower()


@pytest.mark.asyncio
async def test_missing_confirmation_blocks_completion_without_replacement_trials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = _resolved(miniature=True)
    bundle = ResolvedDomSweepBundle(
        resolved=resolved,
        receipt_path=ROOT / "unused",
        project_root=ROOT,
    )
    task = resolved.tasks[0]
    base_runner = _fake_dom_runner(
        task.revision.level0.reachability_marker,
        task.revision.level0.checker.expected,
    )

    async def runner(*args: object, **kwargs: object) -> RunTranscript:
        page_url = str(args[0])
        model = str(args[2])
        if model.endswith("confirming-a-v1") and page_url.endswith("iframe-cross-depth-1"):
            raise ConnectionError("injected provider transport failure")
        return await base_runner(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("webagents.sweeps.dom_scheduler.StructuralFixtureHarness", _MemoryHarness)
    report = await execute_resolved_dom_sweep(
        bundle,
        archive_root=tmp_path / "artifacts",
        output_root=tmp_path / "sweeps",
        runner=runner,
        preflight_credentials=False,
    )

    assert report.verdict == "incomplete"
    assert report.primary_complete_cells == report.primary_required_cells == 2
    assert report.confirmation_required_cells == 4
    assert report.confirmation_complete_cells == 3
    assert report.valid_trial_count == 23
    assert report.attempt_count == 25

    output = tmp_path / "sweeps/test-dom-light"
    assert len((output / "matrix.jsonl").read_text().splitlines()) == 24
    attempts = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert len(attempts) == 25
    excluded_ids = [item["logical_trial_id"] for item in attempts if item["classification"] != "VALID_OUTCOME"]
    assert len(excluded_ids) == 2
    assert len(set(excluded_ids)) == 1
    assert len((output / "exclusions.jsonl").read_text().splitlines()) == 2
    assert not (output / "sweep-receipt.json").exists()
