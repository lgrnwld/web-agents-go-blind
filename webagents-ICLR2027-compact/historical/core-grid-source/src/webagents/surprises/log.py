"""Candidate promotion, append-only event chaining, adjudication, and verification."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ValidationError

from webagents.capture import scan_run_observations
from webagents.capture.archive import canonical_json_bytes
from webagents.io import atomic_write
from webagents.schemas import RunManifest, RunTranscript
from webagents.surprises.schemas import (
    EVENT_ID_PATTERN,
    AxisLevelIdentity,
    BrowserIdentity,
    ConditionIdentity,
    EnvironmentIdentity,
    EvidenceReference,
    EvidenceRole,
    ExpectationRegistry,
    FrameworkExpectation,
    FrameworkIdentity,
    FrameworkSurpriseEvent,
    MarkerAssertion,
    ProviderResponseIdentity,
    ReproductionRecord,
    SchemaIdentity,
    SourceIdentity,
    SurpriseDiagnostic,
    SurpriseLogReport,
    TaskIdentity,
    TransformIdentity,
)
from webagents.sweeps.schemas import SurpriseCandidate, SweepTrialResult

_ZERO_SHA = "0" * 64
_SECRET_KEYS = {
    "api-key",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "proxy-authorization",
    "secret",
    "set-cookie",
    "token",
    "x-api-key",
}
_TOKEN_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
)


class SurpriseLogError(RuntimeError):
    """The surprise-log operation would violate provenance or append-only rules."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def expectation_registry_sha256(registry: ExpectationRegistry) -> str:
    return _sha256(canonical_json_bytes(registry.model_dump(mode="json")))


def load_expectation_registry(path: Path) -> ExpectationRegistry:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        registry = ExpectationRegistry.model_validate(value)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise SurpriseLogError(f"invalid expectation registry {path}: {exc}") from exc
    return registry


def _event_payload(event: FrameworkSurpriseEvent) -> dict[str, Any]:
    value = event.model_dump(mode="json")
    del value["event_sha256"]
    return value


def event_sha256(event: FrameworkSurpriseEvent) -> str:
    return _sha256(canonical_json_bytes(_event_payload(event)))


def _canonical_event_line(event: FrameworkSurpriseEvent) -> bytes:
    return canonical_json_bytes(event.model_dump(mode="json")) + b"\n"


def _read_event_lines(log_path: Path) -> list[tuple[int, bytes]]:
    if not log_path.exists():
        return []
    data = log_path.read_bytes()
    if data and not data.endswith(b"\n"):
        raise SurpriseLogError("events.jsonl ends with an incomplete line")
    return [(number, line + b"\n") for number, line in enumerate(data.splitlines(), start=1)]


def _read_events_strict(log_path: Path) -> list[FrameworkSurpriseEvent]:
    events: list[FrameworkSurpriseEvent] = []
    seen: set[str] = set()
    previous: str | None = None
    for number, line in _read_event_lines(log_path):
        try:
            event = FrameworkSurpriseEvent.model_validate_json(line)
        except Exception as exc:
            raise SurpriseLogError(f"invalid event at line {number}: {exc}") from exc
        if _canonical_event_line(event) != line:
            raise SurpriseLogError(f"event at line {number} is not canonical JSON")
        _assert_no_secret_material(event.model_dump(mode="json"))
        if event.event_id in seen:
            raise SurpriseLogError(f"duplicate event ID at line {number}: {event.event_id}")
        if event.previous_event_sha256 != previous or event.event_sha256 != event_sha256(event):
            raise SurpriseLogError(f"event hash chain is invalid at line {number}")
        if event.updates is not None and event.updates not in seen:
            raise SurpriseLogError(f"invalid update link at line {number}: {event.updates}")
        events.append(event)
        seen.add(event.event_id)
        previous = event.event_sha256
    return events


@contextmanager
def _log_lock(log_path: Path) -> Iterator[None]:
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = log_path.parent / ".events.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_locked(event: FrameworkSurpriseEvent, *, log_path: Path) -> None:
    events = _read_events_strict(log_path)
    if any(item.event_id == event.event_id for item in events):
        raise SurpriseLogError(f"duplicate event ID: {event.event_id}")
    if event.event_type == "candidate" and any(
        item.event_type == "candidate"
        and item.sweep_id == event.sweep_id
        and item.logical_trial_id == event.logical_trial_id
        for item in events
    ):
        raise SurpriseLogError("this sweep logical trial already has a candidate event")
    if event.updates is not None and not any(item.event_id == event.updates for item in events):
        raise SurpriseLogError("updates must reference an event already present in the log")
    if event.event_type == "adjudication" and any(
        item.event_type == "adjudication" and item.updates == event.updates for item in events
    ):
        raise SurpriseLogError("candidate already has an adjudication")
    expected_previous = events[-1].event_sha256 if events else None
    if event.previous_event_sha256 != expected_previous:
        raise SurpriseLogError("event previous_event_sha256 does not match the current log head")
    if event.event_sha256 != event_sha256(event):
        raise SurpriseLogError("event_sha256 does not match the canonical event payload")
    line = _canonical_event_line(event)
    with log_path.open("ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(log_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def append_surprise_event(event: FrameworkSurpriseEvent, *, log_path: Path) -> None:
    """Append one preconstructed, correctly chained event without rewriting history."""

    _assert_no_secret_material(event.model_dump(mode="json"))
    with _log_lock(log_path):
        _append_locked(event, log_path=log_path)


def _next_event_id(events: list[FrameworkSurpriseEvent], logged_at: datetime) -> str:
    day = logged_at.astimezone(UTC).strftime("%Y%m%d")
    prefix = f"SURPRISE-{day}-"
    sequence = max(
        (int(event.event_id.rsplit("-", 1)[1]) for event in events if event.event_id.startswith(prefix)),
        default=0,
    )
    return f"{prefix}{sequence + 1:03d}"


def _build_and_append(
    *,
    log_path: Path,
    logged_at: datetime,
    builder: Callable[[str, str | None], FrameworkSurpriseEvent],
) -> FrameworkSurpriseEvent:
    with _log_lock(log_path):
        events = _read_events_strict(log_path)
        event_id = _next_event_id(events, logged_at)
        previous = events[-1].event_sha256 if events else None
        event = builder(event_id, previous)
        _assert_no_secret_material(event.model_dump(mode="json"))
        _append_locked(event, log_path=log_path)
        return event


def _event_with_hash(value: dict[str, Any]) -> FrameworkSurpriseEvent:
    draft = FrameworkSurpriseEvent.model_validate_json(
        canonical_json_bytes(_jsonable({**value, "event_sha256": _ZERO_SHA}))
    )
    return draft.model_copy(update={"event_sha256": event_sha256(draft)})


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    return value


def _expectation(registry: ExpectationRegistry, expectation_id: str) -> FrameworkExpectation:
    match = next((item for item in registry.expectations if item.id == expectation_id), None)
    if match is None:
        raise SurpriseLogError(f"unknown frozen expectation: {expectation_id}")
    return match


def _jsonl_values(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_bytes().splitlines()
        return [json.loads(line) for line in lines]
    except Exception as exc:
        raise SurpriseLogError(f"cannot parse {path}: {exc}") from exc


def _safe_archive_path(archive_root: Path, run_dir: Path) -> tuple[Path, str]:
    root = archive_root.resolve()
    run = run_dir.resolve()
    if not run.is_dir() or run.is_symlink() or run.parent != root / "runs":
        raise SurpriseLogError("run_dir must be a direct, nonsymlinked child of archive_root/runs")
    return run, run.name


def _role(path: Path) -> Literal["manifest", "transcript", "completion", "observation", "model_request", "metadata"]:
    if path.name == "manifest.json":
        return "manifest"
    if path.name == "transcript.json":
        return "transcript"
    if path.name == "completion.json":
        return "completion"
    if path.name.startswith("observation."):
        return "observation"
    if path.name == "model-request.json":
        return "model_request"
    if path.name == "metadata.json":
        return "metadata"
    raise SurpriseLogError(f"unexpected evidence artifact: {path}")


def _collect_run_evidence(
    run_dir: Path,
    *,
    archive_root: Path,
    require_complete: bool,
) -> tuple[RunManifest, RunTranscript, list[EvidenceReference], list[TransformIdentity], bool]:
    run, run_id = _safe_archive_path(archive_root, run_dir)
    scan = scan_run_observations(run, require_complete=require_complete)
    errors = [item for item in scan.diagnostics if item.severity == "error"]
    if errors:
        detail = "; ".join(f"{item.code}:{item.path}" for item in errors)
        raise SurpriseLogError(f"run archive verification failed: {detail}")
    if not scan.records:
        raise SurpriseLogError("run archive contains no model-visible observations")
    try:
        manifest = RunManifest.model_validate_json((run / "manifest.json").read_bytes())
        transcript = RunTranscript.model_validate_json((run / "transcript.json").read_bytes())
    except Exception as exc:
        raise SurpriseLogError(f"run identity files are invalid: {exc}") from exc
    capture_complete = all(item.capture_completeness == "complete" for item in scan.records)
    if require_complete and not capture_complete:
        raise SurpriseLogError("candidate source must have complete observation capture")

    files = [run / "manifest.json", run / "transcript.json"]
    completion = run / "completion.json"
    if completion.is_file():
        files.append(completion)
    for step_dir in sorted((run / "observations").glob("step-*")):
        for name in ("metadata.json", "observation.txt", "observation.json", "model-request.json"):
            path = step_dir / name
            if path.is_file():
                files.append(path)
    evidence: list[EvidenceReference] = []
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(run):
            raise SurpriseLogError(f"unsafe evidence artifact: {path}")
        data = path.read_bytes()
        evidence.append(
            EvidenceReference(
                run_id=run_id,
                artifact_path=path.relative_to(archive_root.resolve()).as_posix(),
                role=_role(path),
                sha256=_sha256(data),
                byte_length=len(data),
            )
        )
    transform_values = {(str(record.transform["name"]), record.transform.get("version")) for record in scan.records}
    transforms = [
        TransformIdentity(name=name, version=str(version) if version is not None else None)
        for name, version in sorted(transform_values, key=lambda item: (item[0], str(item[1] or "")))
    ]
    return manifest, transcript, evidence, transforms, capture_complete


def _collect_sweep_evidence(
    sweep_dir: Path,
    *,
    archive_root: Path,
    run_id: str,
) -> list[EvidenceReference]:
    root = archive_root.resolve()
    sweep = sweep_dir.resolve()
    if not sweep.is_dir() or sweep.is_symlink() or not sweep.is_relative_to(root):
        raise SurpriseLogError("sweep_dir must be a nonsymlinked directory beneath archive_root")
    names_and_roles: tuple[tuple[str, EvidenceRole], ...] = (
        ("resolved-spec.json", "sweep_resolved_spec"),
        ("conditions.json", "sweep_conditions"),
        ("matrix.jsonl", "sweep_matrix"),
        ("results.jsonl", "sweep_results"),
        ("surprise-candidates.jsonl", "sweep_candidate"),
    )
    output: list[EvidenceReference] = []
    for name, role in names_and_roles:
        path = sweep / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(sweep):
            raise SurpriseLogError(f"missing or unsafe sweep evidence: {path}")
        data = path.read_bytes()
        output.append(
            EvidenceReference(
                run_id=run_id,
                artifact_path=path.relative_to(root).as_posix(),
                role=role,
                sha256=_sha256(data),
                byte_length=len(data),
            )
        )
    return output


def _provider_responses(transcript: RunTranscript) -> list[ProviderResponseIdentity]:
    provider, model = transcript.input.model.split("/", 1)
    output: list[ProviderResponseIdentity] = []
    for step in transcript.steps:
        metadata = step.model_response.provider_metadata if step.model_response is not None else {}
        response_id = metadata.get("id") or metadata.get("response_id") or metadata.get("request_id")
        output.append(
            ProviderResponseIdentity(
                step=step.step,
                provider=str(metadata.get("provider") or provider),
                model=str(metadata.get("model") or model),
                response_id=str(response_id) if response_id is not None else None,
            )
        )
    return output


def _browser_identity(manifest: RunManifest) -> BrowserIdentity:
    implementation = manifest.implementation
    revision = None
    if implementation.framework_commit.startswith("chromium-"):
        revision = implementation.framework_commit.removeprefix("chromium-")
    return BrowserIdentity(
        name="chromium" if implementation.browser_version or implementation.playwright_version else None,
        revision=revision,
        version=implementation.browser_version,
        playwright_version=implementation.playwright_version,
    )


def _fixture_route(page_url: str) -> str:
    parts = urlsplit(page_url)
    route = parts.path or "/"
    return f"{route}?{parts.query}" if parts.query else route


def _marker_present(run_dir: Path, marker: str) -> bool:
    scan = scan_run_observations(run_dir, require_complete=True)
    marker_bytes = marker.encode("utf-8")
    return any(marker_bytes in record.observation_bytes for record in scan.records)


def _source_identity(resolved: dict[str, Any], manifest: RunManifest) -> SourceIdentity:
    source_sha = resolved.get("implementation", {}).get("source_sha256")
    if not isinstance(source_sha, str):
        raise SurpriseLogError("resolved sweep lacks implementation.source_sha256")
    return SourceIdentity(
        repository_commit=manifest.implementation.repository_commit,
        source_state=manifest.implementation.source_state,
        source_sha256=source_sha,
    )


def _axis_levels(spec: dict[str, Any], condition_id: str) -> list[AxisLevelIdentity]:
    return [
        AxisLevelIdentity(axis=axis["id"], level=level["id"])
        for axis in spec["axes"]
        for level in axis["levels"]
        if level["condition_id"] == condition_id
    ]


def _validate_expectation_for_candidate(
    expectation: FrameworkExpectation,
    candidate: SurpriseCandidate,
    agent_class: str,
) -> None:
    if expectation.agent_class != agent_class or candidate.condition_id not in expectation.condition_ids:
        raise SurpriseLogError("expectation does not cover the candidate class and condition")
    if candidate.trigger == "expectation_mismatch":
        if expectation.metric != "reachability" or expectation.expected_outcome != candidate.expectation:
            raise SurpriseLogError("candidate mismatch does not match the frozen reachability expectation")
        if expectation.expected_outcome == "open_question":
            raise SurpriseLogError("an open-question expectation cannot trigger a reachability mismatch")
    elif expectation.metric != "model_directional_consistency":
        raise SurpriseLogError("model divergence requires the frozen directional-consistency expectation")


def add_sweep_candidate(
    *,
    expectation_id: str,
    sweep_dir: Path,
    run_dir: Path,
    archive_root: Path,
    registry_path: Path,
    log_path: Path,
    summary: str,
    reporter: str,
    logged_at: datetime | None = None,
) -> FrameworkSurpriseEvent:
    """Promote one scheduler recommendation after exact archive and sweep verification."""

    registry = load_expectation_registry(registry_path)
    expectation = _expectation(registry, expectation_id)
    registry_sha = expectation_registry_sha256(registry)
    try:
        resolved_bytes = (sweep_dir / "resolved-spec.json").read_bytes()
        resolved = json.loads(resolved_bytes)
        spec = resolved["spec"]
        agent_class = spec["agent_class"]
        run_id = run_dir.name
        candidates = [
            SurpriseCandidate.model_validate_json(canonical_json_bytes(item))
            for item in _jsonl_values(sweep_dir / "surprise-candidates.jsonl")
        ]
        candidate = next(item for item in candidates if item.run_id == run_id)
        results = [
            SweepTrialResult.model_validate_json(canonical_json_bytes(item))
            for item in _jsonl_values(sweep_dir / "results.jsonl")
        ]
        result = next(
            item for item in results if item.run_id == run_id and item.logical_trial_id == candidate.logical_trial_id
        )
        matrix = _jsonl_values(sweep_dir / "matrix.jsonl")
        row = next(item for item in matrix if item["logical_trial_id"] == candidate.logical_trial_id)
    except (OSError, KeyError, StopIteration, json.JSONDecodeError, ValidationError) as exc:
        raise SurpriseLogError(f"candidate is not bound to complete sweep evidence: {exc}") from exc
    if not result.counts_toward_cell or result.capture_complete is not True or not result.archive_valid:
        raise SurpriseLogError("only valid outcomes with complete verified capture may become candidates")
    _validate_expectation_for_candidate(expectation, candidate, agent_class)
    manifest, transcript, evidence, transforms, capture_complete = _collect_run_evidence(
        run_dir, archive_root=archive_root, require_complete=True
    )
    evidence.extend(_collect_sweep_evidence(sweep_dir, archive_root=archive_root, run_id=run_id))
    manifest_ref = next(item for item in evidence if item.role == "manifest")
    if manifest_ref.sha256 != candidate.manifest_sha256:
        raise SurpriseLogError("candidate manifest hash does not match the source archive")
    if manifest.agent_class != agent_class or manifest.input.model != row["runner_model"]:
        raise SurpriseLogError("source run identity does not match the scheduled matrix row")
    task = next(
        item
        for item in resolved["tasks"]
        if item["revision"]["task_id"] == row["task_id"] and item["revision"]["revision"] == row["task_revision"]
    )
    marker = task["revision"]["level0"]["reachability_marker"]
    observed_present = _marker_present(run_dir, marker)
    if observed_present != candidate.observed_reachable or observed_present != result.reachable:
        raise SurpriseLogError("scheduler candidate does not match the exact archived marker assertion")
    expected_present = expectation.expected_outcome == "present" if expectation.metric == "reachability" else None
    now = (logged_at or datetime.now(UTC)).astimezone(UTC)
    observed_at = candidate.observed_at.astimezone(UTC)

    def build(event_id: str, previous: str | None) -> FrameworkSurpriseEvent:
        return _event_with_hash(
            {
                "event_id": event_id,
                "event_type": "candidate",
                "observed_at": observed_at,
                "observed_date": observed_at.date(),
                "logged_at": now,
                "expectation_set": registry.expectation_set,
                "expectation_set_sha256": registry_sha,
                "expectation_id": expectation.id,
                "summary": summary,
                "agent_class": agent_class,
                "framework": FrameworkIdentity(
                    name=manifest.implementation.framework_name,
                    version=manifest.implementation.framework_version,
                    commit=manifest.implementation.framework_commit,
                ),
                "browser": _browser_identity(manifest),
                "source": _source_identity(resolved, manifest),
                "schemas": SchemaIdentity(
                    archive=manifest.archive_schema_version,
                    transcript=transcript.schema_version,
                ),
                "prompt_version": manifest.implementation.prompt_version,
                "provider_qualified_model": manifest.input.model,
                "provider_responses": _provider_responses(transcript),
                "environment": EnvironmentIdentity(
                    host_platform=manifest.runtime.host_platform,
                    python=manifest.runtime.python,
                ),
                "task": TaskIdentity(id=row["task_id"], revision=row["task_revision"]),
                "condition": ConditionIdentity(
                    id=candidate.condition_id, axes=_axis_levels(spec, candidate.condition_id)
                ),
                "sweep_id": candidate.sweep_id,
                "logical_trial_id": candidate.logical_trial_id,
                "fixture_sha256": row["task_definition_sha256"],
                "fixture_route": _fixture_route(manifest.input.page_url),
                "expected_behavior": expectation.statement,
                "observed_behavior": candidate.summary,
                "marker_assertion": MarkerAssertion(
                    marker_utf8=marker,
                    marker_sha256=_sha256(marker.encode("utf-8")),
                    expected_present=expected_present,
                    observed_present=observed_present,
                ),
                "capture_complete": capture_complete,
                "transforms": transforms,
                "evidence": evidence,
                "status": "candidate",
                "impact": ["limitations", "result_interpretation"],
                "paper_disposition": "pending",
                "reporter": reporter,
                "previous_event_sha256": previous,
            }
        )

    return _build_and_append(log_path=log_path, logged_at=now, builder=build)


def _identity_matches(candidate: FrameworkSurpriseEvent, manifest: RunManifest) -> bool:
    implementation = manifest.implementation
    return (
        manifest.agent_class == candidate.agent_class
        and manifest.input.model == candidate.provider_qualified_model
        and implementation.framework_name == candidate.framework.name
        and implementation.framework_version == candidate.framework.version
        and implementation.framework_commit == candidate.framework.commit
        and implementation.prompt_version == candidate.prompt_version
        and implementation.playwright_version == candidate.browser.playwright_version
        and implementation.browser_version == candidate.browser.version
        and implementation.repository_commit == candidate.source.repository_commit
        and implementation.source_state == candidate.source.source_state
        and _fixture_route(manifest.input.page_url) == candidate.fixture_route
    )


def adjudicate_surprise(
    candidate_id: str,
    *,
    status: Literal[
        "confirmed",
        "not_reproduced",
        "fixture_issue",
        "evidence_issue",
        "model_specific",
        "implementation_change",
        "awaiting_followup",
    ],
    log_path: Path,
    registry_path: Path,
    archive_root: Path,
    reproductions_dir: Path,
    reporter: str,
    summary: str,
    reproduction_run_dir: Path | None = None,
    paper_disposition: Literal[
        "pending", "results", "limitations", "results_and_limitations", "not_applicable"
    ] = "pending",
    source_sha256: str | None = None,
    logged_at: datetime | None = None,
) -> FrameworkSurpriseEvent:
    """Append an adjudication and, when supplied, an immutable reproduction record."""

    registry = load_expectation_registry(registry_path)
    registry_sha = expectation_registry_sha256(registry)
    events = _read_events_strict(log_path)
    candidate = next(
        (item for item in events if item.event_id == candidate_id and item.event_type == "candidate"),
        None,
    )
    if candidate is None:
        raise SurpriseLogError(f"unknown candidate event: {candidate_id}")
    if any(item.updates == candidate_id and item.event_type == "adjudication" for item in events):
        raise SurpriseLogError("candidate already has an adjudication; append a correction instead")
    if candidate.expectation_set_sha256 != registry_sha:
        raise SurpriseLogError("candidate expectation registry does not match the current frozen registry")
    _expectation(registry, candidate.expectation_id)
    requires_reproduction = status in {
        "confirmed",
        "not_reproduced",
        "model_specific",
        "implementation_change",
    }
    if requires_reproduction and reproduction_run_dir is None:
        raise SurpriseLogError(f"status {status} requires a verified reproduction run")

    manifest: RunManifest | None = None
    transcript: RunTranscript | None = None
    evidence = candidate.evidence
    transforms = candidate.transforms
    capture_complete = candidate.capture_complete
    observed_at = candidate.observed_at
    observed_present = candidate.marker_assertion.observed_present
    reproduction_id: str | None = None
    reproduction_record_sha256: str | None = None
    identity_matches = True
    reproduced = False
    if reproduction_run_dir is not None:
        manifest, transcript, evidence, transforms, capture_complete = _collect_run_evidence(
            reproduction_run_dir,
            archive_root=archive_root,
            require_complete=status != "evidence_issue",
        )
        observed_present = _marker_present(reproduction_run_dir, candidate.marker_assertion.marker_utf8)
        reproduced = observed_present == candidate.marker_assertion.observed_present
        identity_matches = _identity_matches(candidate, manifest)
        observed_at = (transcript.finished_at or manifest.runtime.started_at).astimezone(UTC)
        reproduction_id = f"REPRO-{candidate_id}-{manifest.run_id}"
        if status == "confirmed" and (not identity_matches or not reproduced):
            raise SurpriseLogError("confirmed requires the same identity and reproduced marker behavior")
        if status == "not_reproduced" and (not identity_matches or reproduced):
            raise SurpriseLogError("not_reproduced requires the same identity and a flipped marker assertion")
        if status == "implementation_change" and identity_matches:
            raise SurpriseLogError("implementation_change requires a different framework/browser/source identity")
        record = ReproductionRecord(
            reproduction_id=reproduction_id,
            candidate_event_id=candidate_id,
            run_id=manifest.run_id,
            recorded_at=(logged_at or datetime.now(UTC)).astimezone(UTC),
            identity_matches_candidate=identity_matches,
            marker_observed_present=observed_present,
            candidate_behavior_reproduced=reproduced,
            evidence=evidence,
        )
        record_bytes = canonical_json_bytes(record.model_dump(mode="json"))
        reproduction_record_sha256 = _sha256(record_bytes)
        reproduction_path = reproductions_dir / f"{candidate_id}.json"
        try:
            atomic_write(reproduction_path, record_bytes)
        except FileExistsError as exc:
            raise SurpriseLogError(f"reproduction record already exists: {reproduction_path}") from exc

    now = (logged_at or datetime.now(UTC)).astimezone(UTC)
    actual_framework = candidate.framework
    actual_browser = candidate.browser
    actual_source = candidate.source
    actual_schemas = candidate.schemas
    actual_prompt = candidate.prompt_version
    actual_model = candidate.provider_qualified_model
    actual_provider_responses = candidate.provider_responses
    actual_environment = candidate.environment
    if manifest is not None and transcript is not None:
        actual_framework = FrameworkIdentity(
            name=manifest.implementation.framework_name,
            version=manifest.implementation.framework_version,
            commit=manifest.implementation.framework_commit,
        )
        actual_browser = _browser_identity(manifest)
        actual_source = SourceIdentity(
            repository_commit=manifest.implementation.repository_commit,
            source_state=manifest.implementation.source_state,
            source_sha256=source_sha256 or candidate.source.source_sha256,
        )
        if status == "implementation_change" and source_sha256 is None:
            raise SurpriseLogError("implementation_change requires the reproduction source_sha256")
        actual_schemas = SchemaIdentity(
            archive=manifest.archive_schema_version,
            transcript=transcript.schema_version,
        )
        actual_prompt = manifest.implementation.prompt_version
        actual_model = manifest.input.model
        actual_provider_responses = _provider_responses(transcript)
        actual_environment = EnvironmentIdentity(
            host_platform=manifest.runtime.host_platform,
            python=manifest.runtime.python,
        )

    def build(event_id: str, previous: str | None) -> FrameworkSurpriseEvent:
        return _event_with_hash(
            {
                **candidate.model_dump(
                    mode="json",
                    exclude={
                        "event_id",
                        "event_type",
                        "observed_at",
                        "observed_date",
                        "logged_at",
                        "summary",
                        "framework",
                        "browser",
                        "source",
                        "schemas",
                        "prompt_version",
                        "provider_qualified_model",
                        "provider_responses",
                        "environment",
                        "observed_behavior",
                        "marker_assertion",
                        "capture_complete",
                        "transforms",
                        "evidence",
                        "reproduction_id",
                        "reproduction_record_sha256",
                        "status",
                        "paper_disposition",
                        "reporter",
                        "updates",
                        "supersedes",
                        "previous_event_sha256",
                        "event_sha256",
                    },
                ),
                "event_id": event_id,
                "event_type": "adjudication",
                "observed_at": observed_at,
                "observed_date": observed_at.astimezone(UTC).date(),
                "logged_at": now,
                "summary": summary,
                "framework": actual_framework,
                "browser": actual_browser,
                "source": actual_source,
                "schemas": actual_schemas,
                "prompt_version": actual_prompt,
                "provider_qualified_model": actual_model,
                "provider_responses": actual_provider_responses,
                "environment": actual_environment,
                "observed_behavior": (
                    f"Minimal reproduction marker presence was {observed_present}." if reproduction_id else summary
                ),
                "marker_assertion": candidate.marker_assertion.model_copy(
                    update={"observed_present": observed_present}
                ),
                "capture_complete": capture_complete,
                "transforms": transforms,
                "evidence": evidence,
                "reproduction_id": reproduction_id,
                "reproduction_record_sha256": reproduction_record_sha256,
                "status": status,
                "paper_disposition": paper_disposition,
                "reporter": reporter,
                "updates": candidate_id,
                "supersedes": None,
                "previous_event_sha256": previous,
                "expectation_set_sha256": registry_sha,
            }
        )

    return _build_and_append(log_path=log_path, logged_at=now, builder=build)


def correct_surprise(
    target_id: str,
    *,
    log_path: Path,
    registry_path: Path,
    reporter: str,
    summary: str,
    paper_disposition: Literal["pending", "results", "limitations", "results_and_limitations", "not_applicable"],
    logged_at: datetime | None = None,
) -> FrameworkSurpriseEvent:
    """Append a correction while preserving the exact bytes of the superseded event."""

    registry = load_expectation_registry(registry_path)
    registry_sha = expectation_registry_sha256(registry)
    events = _read_events_strict(log_path)
    target = next((item for item in events if item.event_id == target_id), None)
    if target is None:
        raise SurpriseLogError(f"unknown event to correct: {target_id}")
    if target.expectation_set_sha256 != registry_sha:
        raise SurpriseLogError("target expectation registry does not match the frozen registry")
    now = (logged_at or datetime.now(UTC)).astimezone(UTC)

    def build(event_id: str, previous: str | None) -> FrameworkSurpriseEvent:
        value = target.model_dump(
            mode="json",
            exclude={
                "event_id",
                "event_type",
                "logged_at",
                "summary",
                "paper_disposition",
                "reporter",
                "updates",
                "supersedes",
                "previous_event_sha256",
                "event_sha256",
            },
        )
        return _event_with_hash(
            {
                **value,
                "event_id": event_id,
                "event_type": "correction",
                "logged_at": now,
                "summary": summary,
                "paper_disposition": paper_disposition,
                "reporter": reporter,
                "updates": target_id,
                "supersedes": target_id,
                "previous_event_sha256": previous,
                "expectation_set_sha256": registry_sha,
            }
        )

    return _build_and_append(log_path=log_path, logged_at=now, builder=build)


def _secret_locations(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if str(key).lower() in _SECRET_KEYS:
                found.append(child_path)
            found.extend(_secret_locations(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_secret_locations(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        if any(pattern.search(value) for pattern in _TOKEN_PATTERNS):
            found.append(path)
        for token in value.split():
            if token.startswith(("http://", "https://")):
                parts = urlsplit(token.rstrip(".,);]"))
                if parts.username is not None or parts.password is not None:
                    found.append(path)
    return found


def _assert_no_secret_material(value: Any) -> None:
    found = _secret_locations(value)
    if found:
        raise SurpriseLogError(f"secret-bearing material is forbidden at: {', '.join(sorted(set(found)))}")


def _diagnostic(
    diagnostics: list[SurpriseDiagnostic],
    code: str,
    path: str,
    message: str,
    *,
    event_id: str | None = None,
) -> None:
    diagnostics.append(SurpriseDiagnostic(severity="error", code=code, event_id=event_id, path=path, message=message))


def _verify_evidence(
    event: FrameworkSurpriseEvent,
    *,
    archive_root: Path,
    log_path: Path,
    diagnostics: list[SurpriseDiagnostic],
) -> None:
    root = archive_root.resolve()
    for reference in event.evidence:
        path = root / reference.artifact_path
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            _diagnostic(
                diagnostics,
                "missing_evidence",
                reference.artifact_path,
                "evidence artifact is missing, unsafe, or outside archive_root",
                event_id=event.event_id,
            )
            continue
        data = path.read_bytes()
        if len(data) != reference.byte_length or _sha256(data) != reference.sha256:
            _diagnostic(
                diagnostics,
                "evidence_hash_mismatch",
                reference.artifact_path,
                "evidence size or SHA-256 differs from the logged reference",
                event_id=event.event_id,
            )
    run_ids = {item.run_id for item in event.evidence}
    if len(run_ids) != 1:
        _diagnostic(
            diagnostics,
            "mixed_run_evidence",
            "events.jsonl",
            "one event must reference exactly one run archive",
            event_id=event.event_id,
        )
        return
    run_id = next(iter(run_ids))
    run_dir = root / "runs" / run_id
    scan = scan_run_observations(run_dir, require_complete=event.status != "evidence_issue")
    for item in scan.diagnostics:
        if item.severity == "error":
            _diagnostic(
                diagnostics,
                f"archive_{item.code}",
                item.path,
                item.message,
                event_id=event.event_id,
            )
    actual_capture_complete = bool(scan.records) and all(
        record.capture_completeness == "complete" for record in scan.records
    )
    if not actual_capture_complete or actual_capture_complete != event.capture_complete:
        _diagnostic(
            diagnostics,
            "partial_capture",
            f"runs/{run_id}",
            "archive contains partial capture or disagrees with the logged capture-completeness assertion",
            event_id=event.event_id,
        )
    if scan.records:
        present = any(
            event.marker_assertion.marker_utf8.encode("utf-8") in record.observation_bytes for record in scan.records
        )
        if present != event.marker_assertion.observed_present:
            _diagnostic(
                diagnostics,
                "marker_assertion_mismatch",
                f"runs/{run_id}",
                "exact observation bytes disagree with the logged marker assertion",
                event_id=event.event_id,
            )
    try:
        manifest = RunManifest.model_validate_json((run_dir / "manifest.json").read_bytes())
        transcript = RunTranscript.model_validate_json((run_dir / "transcript.json").read_bytes())
        expected_browser = _browser_identity(manifest)
        expected_framework = FrameworkIdentity(
            name=manifest.implementation.framework_name,
            version=manifest.implementation.framework_version,
            commit=manifest.implementation.framework_commit,
        )
        expected_environment = EnvironmentIdentity(
            host_platform=manifest.runtime.host_platform,
            python=manifest.runtime.python,
        )
        if (
            manifest.agent_class != event.agent_class
            or manifest.input.model != event.provider_qualified_model
            or expected_framework != event.framework
            or expected_browser != event.browser
            or manifest.implementation.prompt_version != event.prompt_version
            or manifest.implementation.repository_commit != event.source.repository_commit
            or manifest.implementation.source_state != event.source.source_state
            or manifest.archive_schema_version != event.schemas.archive
            or transcript.schema_version != event.schemas.transcript
            or expected_environment != event.environment
            or _provider_responses(transcript) != event.provider_responses
            or _fixture_route(manifest.input.page_url) != event.fixture_route
        ):
            _diagnostic(
                diagnostics,
                "run_identity_mismatch",
                f"runs/{run_id}/manifest.json",
                "logged framework, browser, source, schema, environment, model, or provider identity differs",
                event_id=event.event_id,
            )
    except Exception as exc:
        _diagnostic(
            diagnostics,
            "invalid_run_identity",
            f"runs/{run_id}",
            str(exc),
            event_id=event.event_id,
        )

    by_role = {item.role: item for item in event.evidence}
    if event.event_type == "candidate":
        required_sweep_roles = {
            "sweep_resolved_spec",
            "sweep_conditions",
            "sweep_matrix",
            "sweep_results",
            "sweep_candidate",
        }
        missing_roles = required_sweep_roles - set(by_role)
        if missing_roles:
            _diagnostic(
                diagnostics,
                "missing_sweep_evidence",
                "events.jsonl",
                f"candidate lacks sweep evidence roles: {sorted(missing_roles)}",
                event_id=event.event_id,
            )
        else:
            try:
                resolved = json.loads((root / by_role["sweep_resolved_spec"].artifact_path).read_bytes())
                matrix = _jsonl_values(root / by_role["sweep_matrix"].artifact_path)
                results = [
                    SweepTrialResult.model_validate_json(canonical_json_bytes(item))
                    for item in _jsonl_values(root / by_role["sweep_results"].artifact_path)
                ]
                candidates = [
                    SurpriseCandidate.model_validate_json(canonical_json_bytes(item))
                    for item in _jsonl_values(root / by_role["sweep_candidate"].artifact_path)
                ]
                row = next(item for item in matrix if item["logical_trial_id"] == event.logical_trial_id)
                result = next(
                    item
                    for item in results
                    if item.logical_trial_id == event.logical_trial_id and item.run_id == run_id
                )
                candidate = next(
                    item
                    for item in candidates
                    if item.logical_trial_id == event.logical_trial_id and item.run_id == run_id
                )
                if (
                    resolved["spec"]["sweep_id"] != event.sweep_id
                    or resolved["implementation"]["source_sha256"] != event.source.source_sha256
                    or row["task_definition_sha256"] != event.fixture_sha256
                    or row["task_id"] != event.task.id
                    or row["task_revision"] != event.task.revision
                    or row["condition_id"] != event.condition.id
                    or row["runner_model"] != event.provider_qualified_model
                    or not result.counts_toward_cell
                    or result.capture_complete is not True
                    or candidate.observed_reachable != event.marker_assertion.observed_present
                ):
                    raise ValueError("sweep identity or candidate assertion differs from the event")
            except Exception as exc:
                _diagnostic(
                    diagnostics,
                    "sweep_identity_mismatch",
                    "events.jsonl",
                    str(exc),
                    event_id=event.event_id,
                )

    if event.reproduction_id is not None and event.updates is not None:
        reproduction_path = log_path.parent / "reproductions" / f"{event.updates}.json"
        try:
            reproduction_bytes = reproduction_path.read_bytes()
            if _sha256(reproduction_bytes) != event.reproduction_record_sha256:
                raise ValueError("reproduction record hash differs")
            record = ReproductionRecord.model_validate_json(reproduction_bytes)
            if canonical_json_bytes(record.model_dump(mode="json")) != reproduction_bytes:
                raise ValueError("reproduction record is not canonical JSON")
            if record.reproduction_id != event.reproduction_id or record.run_id != run_id:
                raise ValueError("reproduction record identity differs")
        except Exception as exc:
            _diagnostic(
                diagnostics,
                "invalid_reproduction_record",
                str(reproduction_path),
                str(exc),
                event_id=event.event_id,
            )


def verify_surprise_log(
    log_path: Path,
    *,
    archive_root: Path,
    registry_path: Path | None = None,
) -> SurpriseLogReport:
    """Verify canonical encoding, hash chaining, links, identities, and evidence bytes."""

    registry = load_expectation_registry(registry_path or log_path.parent / "expectations.yaml")
    registry_sha = expectation_registry_sha256(registry)
    expectations = {item.id: item for item in registry.expectations}
    diagnostics: list[SurpriseDiagnostic] = []
    events: list[FrameworkSurpriseEvent] = []
    previous: str | None = None
    seen: dict[str, FrameworkSurpriseEvent] = {}
    try:
        lines = _read_event_lines(log_path)
    except SurpriseLogError as exc:
        lines = []
        _diagnostic(diagnostics, "incomplete_jsonl", str(log_path), str(exc))
    for number, line in lines:
        path = f"{log_path}:{number}"
        try:
            raw = json.loads(line)
            _assert_no_secret_material(raw)
            event = FrameworkSurpriseEvent.model_validate_json(line)
        except Exception as exc:
            _diagnostic(diagnostics, "invalid_event", path, str(exc))
            continue
        if _canonical_event_line(event) != line:
            _diagnostic(
                diagnostics, "noncanonical_json", path, "event line is not canonical JSON", event_id=event.event_id
            )
        if event.event_id in seen:
            _diagnostic(diagnostics, "duplicate_event_id", path, "event ID is duplicated", event_id=event.event_id)
        if event.previous_event_sha256 != previous:
            _diagnostic(
                diagnostics,
                "chain_previous_mismatch",
                path,
                "previous event hash is incorrect",
                event_id=event.event_id,
            )
        computed = event_sha256(event)
        if event.event_sha256 != computed:
            _diagnostic(
                diagnostics, "chain_event_mismatch", path, "event payload hash is incorrect", event_id=event.event_id
            )
        date_in_id = event.event_id.split("-")[1] if EVENT_ID_PATTERN.fullmatch(event.event_id) else ""
        if date_in_id != event.logged_at.astimezone(UTC).strftime("%Y%m%d"):
            _diagnostic(
                diagnostics,
                "event_id_date_mismatch",
                path,
                "event ID date must equal logged_at UTC date",
                event_id=event.event_id,
            )
        if event.logged_at < event.observed_at:
            _diagnostic(diagnostics, "timestamp_order", path, "logged_at precedes observed_at", event_id=event.event_id)
        expectation = expectations.get(event.expectation_id)
        if expectation is None:
            _diagnostic(
                diagnostics,
                "unknown_expectation",
                path,
                "event references an unknown expectation",
                event_id=event.event_id,
            )
        else:
            if event.agent_class != expectation.agent_class or event.condition.id not in expectation.condition_ids:
                _diagnostic(
                    diagnostics,
                    "expectation_scope",
                    path,
                    "expectation does not cover event class/condition",
                    event_id=event.event_id,
                )
        if event.expectation_set != registry.expectation_set or event.expectation_set_sha256 != registry_sha:
            _diagnostic(
                diagnostics,
                "expectation_hash",
                path,
                "event expectation-set identity is stale",
                event_id=event.event_id,
            )
        if event.updates is not None and event.updates not in seen:
            _diagnostic(
                diagnostics,
                "invalid_update_link",
                path,
                "updates must reference an earlier event",
                event_id=event.event_id,
            )
        elif event.updates is not None:
            target = seen[event.updates]
            if (
                target.expectation_id != event.expectation_id
                or target.agent_class != event.agent_class
                or target.task != event.task
                or target.condition != event.condition
                or target.fixture_sha256 != event.fixture_sha256
                or target.fixture_route != event.fixture_route
            ):
                _diagnostic(
                    diagnostics,
                    "update_identity_mismatch",
                    path,
                    "update link crosses expectation, class, task, or condition identity",
                    event_id=event.event_id,
                )
        if event.supersedes is not None and event.supersedes not in seen:
            _diagnostic(
                diagnostics,
                "invalid_supersedes_link",
                path,
                "supersedes must reference an earlier event",
                event_id=event.event_id,
            )
        _verify_evidence(
            event,
            archive_root=archive_root,
            log_path=log_path,
            diagnostics=diagnostics,
        )
        events.append(event)
        seen[event.event_id] = event
        previous = event.event_sha256

    roots = {event.event_id: event for event in events if event.event_type == "candidate"}
    latest = dict(roots)
    for event in events:
        if event.updates in roots:
            latest[event.updates] = event
    unresolved_statuses = {"candidate", "awaiting_followup", "evidence_issue", "fixture_issue"}
    return SurpriseLogReport(
        valid=not diagnostics,
        expectation_set=registry.expectation_set,
        expectation_set_sha256=registry_sha,
        event_count=len(events),
        candidate_count=len(roots),
        unresolved_count=sum(item.status in unresolved_statuses for item in latest.values()),
        confirmed_count=sum(item.status == "confirmed" for item in latest.values()),
        head_sha256=events[-1].event_sha256 if events else None,
        diagnostics=diagnostics,
        verified_at=datetime.now(UTC),
    )
