"""Crash-tolerant, append-only archive for exact model-visible observations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
import threading
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from webagents.schemas import (
    RUN_ID_PATTERN,
    ArtifactReference,
    ArtifactRefs,
    CompletionRecord,
    ModelCallMetadata,
    ObservationKind,
    ObservationRecord,
    ObservationReference,
    ObservationScanResult,
    RunEvent,
    RunManifest,
    RunTranscript,
    VerificationDiagnostic,
    observation_kind_for_agent,
)

_STEP_DIRECTORY_PATTERN = re.compile(r"^step-([0-9]{3,})(?:-attempt-([0-9]{3,}))?$")
_TRANSPORT_SECRET_KEYS = {
    "api-key",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}


class ArchiveError(RuntimeError):
    """Raised when evidence cannot be safely or atomically archived."""


def canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON as UTF-8 with sorted keys, compact separators, and no nonfinite numbers.

    This documented encoding is the archive's stable RFC 8785-equivalent contract. It is
    intentionally narrower than arbitrary JSON and does not append a trailing newline.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _diagnostic(
    code: str,
    path: str,
    message: str,
    *,
    severity: Literal["error", "warning"] = "error",
) -> VerificationDiagnostic:
    return VerificationDiagnostic(severity=severity, code=code, path=path, message=message)


def _atomic_write(path: Path, data: bytes, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temp_path, path)
        else:
            # A hard-link claim is atomic and fails if another writer already owns the path.
            os.link(temp_path, path)
            temp_path.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _parse_canonical_json(data: bytes, *, label: str) -> Any:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"{label} must be valid UTF-8 JSON: {exc}") from exc
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ArchiveError(f"{label} contains unsupported JSON values: {exc}") from exc
    if encoded != data:
        raise ArchiveError(f"{label} must use the documented canonical JSON encoding")
    return value


def _find_transport_secret_fields(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if key_text.lower() in _TRANSPORT_SECRET_KEYS:
                found.append(child_path)
            found.extend(_find_transport_secret_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_find_transport_secret_fields(child, f"{path}[{index}]"))
    return found


def _iter_json_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_json_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_strings(child)


def _validate_payload_contract(
    observation_kind: ObservationKind,
    observation_bytes: bytes,
    model_request_bytes: bytes,
) -> None:
    request_value = _parse_canonical_json(model_request_bytes, label="model request")
    secret_fields = _find_transport_secret_fields(request_value)
    if secret_fields:
        raise ArchiveError(f"model request contains transport-secret fields: {', '.join(secret_fields)}")
    request_strings = list(_iter_json_strings(request_value))

    if observation_kind == "screenshot":
        if not observation_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ArchiveError("screenshot observation must be a PNG image")
        prefix = "data:image/png;base64,"
        matches = [value for value in request_strings if value.startswith(prefix)]
        decoded_matches = 0
        for value in matches:
            try:
                decoded = base64.b64decode(value[len(prefix) :], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ArchiveError(f"model request contains an invalid PNG data URL: {exc}") from exc
            decoded_matches += decoded == observation_bytes
        occurrences = decoded_matches
    else:
        try:
            observation_text = observation_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ArchiveError(f"observation must be UTF-8: {exc}") from exc
    if observation_kind in {"ax_tree", "cdp_dom_snapshot"}:
        _parse_canonical_json(observation_bytes, label=f"{observation_kind} observation")
        occurrences = sum(value == observation_text for value in request_strings)
    elif observation_kind == "serialized_dom":
        marker = f"<browser_state>\n{observation_text}\n</browser_state>"
        occurrences = sum(value.count(marker) for value in request_strings)
    if occurrences != 1:
        raise ArchiveError(
            f"model request must embed the exact {observation_kind} observation exactly once; found {occurrences}"
        )


def _step_directory_name(step: int, attempt: int) -> str:
    return f"step-{step:03d}" if attempt == 0 else f"step-{step:03d}-attempt-{attempt:03d}"


def _parse_step_directory(name: str) -> tuple[int, int] | None:
    match = _STEP_DIRECTORY_PATTERN.fullmatch(name)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def _safe_artifact_path(step_dir: Path, filename: str) -> Path:
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise ArchiveError(f"unsafe artifact filename: {filename!r}")
    path = step_dir / filename
    if path.parent.resolve() != step_dir.resolve():
        raise ArchiveError(f"artifact path escaped step directory: {filename!r}")
    return path


def _is_safe_file(run_dir: Path, path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(run_dir.resolve())


class RunArchive:
    """Exclusive writer for a single immutable run directory."""

    def __init__(self, run_dir: Path, manifest: RunManifest) -> None:
        self.run_dir = run_dir
        self.manifest = manifest
        self._lock = threading.RLock()
        self._event_sequence = 0

    @classmethod
    def create(cls, root: Path, manifest: RunManifest) -> RunArchive:
        if not RUN_ID_PATTERN.fullmatch(manifest.run_id):
            raise ArchiveError("unsafe run ID")
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        runs_root = root / "runs"
        runs_root.mkdir(exist_ok=True, mode=0o700)
        if not runs_root.resolve().is_relative_to(root):
            raise ArchiveError("runs directory escaped archive root")
        run_dir = runs_root / manifest.run_id
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ArchiveError(f"run already exists: {manifest.run_id}") from exc
        if run_dir.parent.resolve() != runs_root.resolve():
            raise ArchiveError("run directory escaped archive root")
        archive = cls(run_dir=run_dir, manifest=manifest)
        try:
            _atomic_write(
                run_dir / "manifest.json",
                canonical_json_bytes(manifest.model_dump(mode="json")),
                replace=False,
            )
            _atomic_write(run_dir / "events.jsonl", b"", replace=False)
        except BaseException as exc:
            raise ArchiveError("failed to initialize run archive") from exc
        return archive

    def record_model_call(
        self,
        *,
        step: int,
        attempt: int,
        observation_kind: ObservationKind,
        observation_bytes: bytes,
        model_request_bytes: bytes,
        metadata: ModelCallMetadata,
    ) -> ArtifactRefs:
        with self._lock:
            if (self.run_dir / "completion.json").exists():
                raise ArchiveError("cannot record a model call after run completion")
            if step < 0 or attempt < 0:
                raise ArchiveError("step and attempt must be nonnegative")
            if not isinstance(observation_bytes, bytes) or not isinstance(model_request_bytes, bytes):
                raise ArchiveError("observation and model request artifacts must be bytes")
            expected_kind = observation_kind_for_agent(self.manifest.agent_class)
            if observation_kind != expected_kind:
                raise ArchiveError(
                    f"observation kind {observation_kind!r} does not match agent class {self.manifest.agent_class!r}"
                )
            if (
                metadata.run_id != self.manifest.run_id
                or metadata.agent_class != self.manifest.agent_class
                or metadata.step != step
                or metadata.attempt != attempt
                or metadata.observation_kind != observation_kind
            ):
                raise ArchiveError("model-call metadata does not match writer arguments or run manifest")
            if any(
                (
                    metadata.observation_file,
                    metadata.observation_sha256,
                    metadata.observation_bytes,
                    metadata.model_request_file,
                    metadata.model_request_sha256,
                    metadata.model_request_bytes,
                )
            ):
                raise ArchiveError("artifact identity fields are writer-owned and must be empty before commit")
            if set(metadata.transform) != {"name", "version"} or not metadata.transform.get("name"):
                raise ArchiveError("transform must contain exactly nonblank name and version fields")
            if metadata.capture_completeness == "complete" and metadata.missing_targets:
                raise ArchiveError("complete capture cannot declare missing targets")
            if metadata.capture_completeness == "partial" and not metadata.missing_targets:
                raise ArchiveError("partial capture must identify at least one missing target or frame")
            if len(metadata.redactions) != len(set(metadata.redactions)):
                raise ArchiveError("redactions must not contain duplicates")
            if "authorization_headers" not in metadata.redactions:
                raise ArchiveError("redactions must declare authorization_headers")
            _validate_payload_contract(observation_kind, observation_bytes, model_request_bytes)

            directory_name = _step_directory_name(step, attempt)
            observations_root = self.run_dir / "observations"
            observations_root.mkdir(mode=0o700, exist_ok=True)
            step_dir = observations_root / directory_name
            try:
                step_dir.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise ArchiveError(f"model call already exists: step={step}, attempt={attempt}") from exc

            observation_file = {
                "serialized_dom": "observation.txt",
                "ax_tree": "observation.json",
                "screenshot": "observation.png",
                "cdp_dom_snapshot": "observation.json",
            }[observation_kind]
            observation_path = step_dir / observation_file
            request_path = step_dir / "model-request.json"
            metadata_path = step_dir / "metadata.json"

            try:
                _atomic_write(observation_path, observation_bytes, replace=False)
                _atomic_write(request_path, model_request_bytes, replace=False)
                committed_metadata = metadata.model_copy(
                    update={
                        "observation_file": observation_file,
                        "observation_sha256": _sha256(observation_bytes),
                        "observation_bytes": len(observation_bytes),
                        "model_request_file": request_path.name,
                        "model_request_sha256": _sha256(model_request_bytes),
                        "model_request_bytes": len(model_request_bytes),
                    }
                )
                # metadata.json is deliberately last: it is the commit marker.
                _atomic_write(
                    metadata_path,
                    canonical_json_bytes(committed_metadata.model_dump(mode="json")),
                    replace=False,
                )
            except BaseException as exc:
                raise ArchiveError(f"failed to commit model call step={step}, attempt={attempt}") from exc

            base = Path("observations") / directory_name
            return ArtifactRefs(
                observation=ObservationReference(
                    kind=observation_kind,
                    artifact_path=(base / observation_file).as_posix(),
                    sha256=_sha256(observation_bytes),
                    byte_length=len(observation_bytes),
                ),
                model_request=ArtifactReference(
                    artifact_path=(base / request_path.name).as_posix(),
                    sha256=_sha256(model_request_bytes),
                    byte_length=len(model_request_bytes),
                ),
            )

    def append_event(self, event: RunEvent) -> None:
        with self._lock:
            if (self.run_dir / "completion.json").exists():
                raise ArchiveError("cannot append an event after run completion")
            if event.sequence != self._event_sequence:
                raise ArchiveError(f"expected event sequence {self._event_sequence}, got {event.sequence}")
            line = canonical_json_bytes(event.model_dump(mode="json")) + b"\n"
            with (self.run_dir / "events.jsonl").open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self._event_sequence += 1

    def checkpoint_transcript(self, transcript: RunTranscript) -> None:
        with self._lock:
            if (self.run_dir / "completion.json").exists():
                raise ArchiveError("cannot checkpoint a transcript after run completion")
            if transcript.run_id != self.manifest.run_id or transcript.agent_class != self.manifest.agent_class:
                raise ArchiveError("transcript identity does not match manifest")
            _atomic_write(
                self.run_dir / "transcript.json",
                canonical_json_bytes(transcript.model_dump(mode="json")),
                replace=True,
            )

    def update_manifest(self, manifest: RunManifest) -> None:
        """Atomically enrich runtime identity before the run is completed."""

        with self._lock:
            if (self.run_dir / "completion.json").exists():
                raise ArchiveError("cannot update a completed run manifest")
            if manifest.run_id != self.manifest.run_id or manifest.agent_class != self.manifest.agent_class:
                raise ArchiveError("updated manifest identity does not match")
            if manifest.input != self.manifest.input or manifest.runtime != self.manifest.runtime:
                raise ArchiveError("manifest input and initial runtime identity are immutable")
            self.manifest = manifest
            _atomic_write(
                self.run_dir / "manifest.json",
                canonical_json_bytes(manifest.model_dump(mode="json")),
                replace=True,
            )

    def complete(self, result: CompletionRecord) -> None:
        with self._lock:
            if result.run_id != self.manifest.run_id:
                raise ArchiveError("completion run ID does not match manifest")
            transcript_path = self.run_dir / "transcript.json"
            if not transcript_path.is_file():
                raise ArchiveError("cannot complete a run without transcript.json")
            transcript_bytes = transcript_path.read_bytes()
            if _sha256(transcript_bytes) != result.transcript_sha256:
                raise ArchiveError("completion transcript hash does not match transcript.json")
            try:
                transcript = RunTranscript.model_validate_json(transcript_bytes)
            except Exception as exc:
                raise ArchiveError(f"cannot complete a run with an invalid transcript: {exc}") from exc
            if transcript.finished_at is None:
                raise ArchiveError("cannot complete a run whose transcript has no finished_at")
            if result.status != "capture_error" and result.status != transcript.status:
                raise ArchiveError("completion status does not match transcript status")
            _atomic_write(
                self.run_dir / "completion.json",
                canonical_json_bytes(result.model_dump(mode="json")),
                replace=False,
            )

    @property
    def next_event_sequence(self) -> int:
        return self._event_sequence


def _validate_artifact(
    *,
    run_dir: Path,
    metadata_path: Path,
    filename: str,
    expected_hash: str,
    expected_size: int,
    prefix: str,
    diagnostics: list[VerificationDiagnostic],
) -> bytes | None:
    try:
        artifact = _safe_artifact_path(metadata_path.parent, filename)
    except ArchiveError as exc:
        diagnostics.append(
            _diagnostic(
                f"unsafe_{prefix}_path",
                metadata_path.relative_to(run_dir).as_posix(),
                str(exc),
            )
        )
        return None
    relative = artifact.relative_to(run_dir).as_posix()
    if not artifact.is_file():
        diagnostics.append(_diagnostic(f"missing_{prefix}", relative, "file is missing"))
        return None
    if artifact.is_symlink() or not artifact.resolve().is_relative_to(metadata_path.parent.resolve()):
        diagnostics.append(_diagnostic(f"unsafe_{prefix}_path", relative, "artifact is a symlink or escaped its step"))
        return None
    data = artifact.read_bytes()
    if len(data) != expected_size:
        diagnostics.append(
            _diagnostic(
                f"{prefix}_size_mismatch",
                relative,
                f"expected {expected_size} bytes, found {len(data)}",
            )
        )
    if _sha256(data) != expected_hash:
        diagnostics.append(_diagnostic(f"{prefix}_hash_mismatch", relative, "SHA-256 mismatch"))
    return data


def _read_metadata(
    run_dir: Path,
    step_dir: Path,
    diagnostics: list[VerificationDiagnostic],
) -> tuple[ModelCallMetadata, bytes, bytes] | None:
    metadata_path = step_dir / "metadata.json"
    relative = metadata_path.relative_to(run_dir).as_posix()
    if not metadata_path.is_file():
        diagnostics.append(
            _diagnostic(
                "uncommitted_model_call",
                step_dir.relative_to(run_dir).as_posix(),
                "model-call directory has no metadata.json commit marker",
            )
        )
        return None
    if metadata_path.is_symlink() or not metadata_path.resolve().is_relative_to(step_dir.resolve()):
        diagnostics.append(_diagnostic("unsafe_metadata_path", relative, "metadata is a symlink or escaped its step"))
        return None
    try:
        metadata_bytes = metadata_path.read_bytes()
        metadata = ModelCallMetadata.model_validate_json(metadata_bytes)
    except Exception as exc:
        diagnostics.append(_diagnostic("invalid_metadata", relative, str(exc)))
        return None
    try:
        _parse_canonical_json(metadata_bytes, label="metadata")
    except ArchiveError as exc:
        diagnostics.append(_diagnostic("noncanonical_metadata", relative, str(exc)))
    directory_identity = _parse_step_directory(step_dir.name)
    if directory_identity != (metadata.step, metadata.attempt):
        diagnostics.append(
            _diagnostic(
                "call_directory_mismatch",
                relative,
                f"directory identifies {directory_identity}, metadata identifies {(metadata.step, metadata.attempt)}",
            )
        )
    expected_observation_name = {
        "serialized_dom": "observation.txt",
        "ax_tree": "observation.json",
        "screenshot": "observation.png",
        "cdp_dom_snapshot": "observation.json",
    }[metadata.observation_kind]
    if metadata.observation_file != expected_observation_name or metadata.model_request_file != "model-request.json":
        diagnostics.append(
            _diagnostic(
                "unexpected_artifact_name",
                relative,
                "metadata does not name the schema-defined artifact files",
            )
        )
    observation = _validate_artifact(
        run_dir=run_dir,
        metadata_path=metadata_path,
        filename=metadata.observation_file,
        expected_hash=metadata.observation_sha256,
        expected_size=metadata.observation_bytes,
        prefix="observation",
        diagnostics=diagnostics,
    )
    request = _validate_artifact(
        run_dir=run_dir,
        metadata_path=metadata_path,
        filename=metadata.model_request_file,
        expected_hash=metadata.model_request_sha256,
        expected_size=metadata.model_request_bytes,
        prefix="model_request",
        diagnostics=diagnostics,
    )
    if observation is None or request is None:
        return None
    try:
        _validate_payload_contract(metadata.observation_kind, observation, request)
    except ArchiveError as exc:
        diagnostics.append(_diagnostic("payload_contract_violation", relative, str(exc)))
    return metadata, observation, request


def _step_directories(run_dir: Path, diagnostics: list[VerificationDiagnostic]) -> list[Path]:
    observations_root = run_dir / "observations"
    if not observations_root.exists():
        return []
    if not observations_root.is_dir():
        diagnostics.append(_diagnostic("invalid_observations_root", "observations", "not a directory"))
        return []
    if observations_root.is_symlink() or not observations_root.resolve().is_relative_to(run_dir):
        diagnostics.append(
            _diagnostic("unsafe_observations_root", "observations", "directory is a symlink or escaped the run")
        )
        return []
    parsed: list[tuple[tuple[int, int], Path]] = []
    for path in observations_root.iterdir():
        identity = _parse_step_directory(path.name)
        if not path.is_dir() or identity is None:
            diagnostics.append(
                _diagnostic(
                    "unexpected_observation_entry",
                    path.relative_to(run_dir).as_posix(),
                    "expected a step-NNN[-attempt-NNN] directory",
                )
            )
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(observations_root.resolve()):
            diagnostics.append(
                _diagnostic(
                    "unsafe_step_directory",
                    path.relative_to(run_dir).as_posix(),
                    "step is a symlink or escaped observations",
                )
            )
            continue
        parsed.append((identity, path))
    return [path for _, path in sorted(parsed)]


def verify_run(run_dir: Path, *, require_complete: bool = True) -> list[VerificationDiagnostic]:
    """Verify schemas, commit markers, byte contracts, hashes, events, and call cardinality."""

    diagnostics: list[VerificationDiagnostic] = []
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        return [_diagnostic("missing_run_directory", ".", f"run directory does not exist: {run_dir}")]

    manifest: RunManifest | None = None
    transcript: RunTranscript | None = None
    completion: CompletionRecord | None = None
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        diagnostics.append(_diagnostic("missing_file", "manifest.json", "missing manifest.json"))
    elif not _is_safe_file(run_dir, manifest_path):
        diagnostics.append(_diagnostic("unsafe_file_path", "manifest.json", "file is a symlink or escaped the run"))
    else:
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = RunManifest.model_validate_json(manifest_bytes)
            _parse_canonical_json(manifest_bytes, label="manifest")
        except Exception as exc:
            diagnostics.append(_diagnostic("invalid_manifest", "manifest.json", str(exc)))

    transcript_path = run_dir / "transcript.json"
    if not transcript_path.is_file():
        diagnostics.append(_diagnostic("missing_file", "transcript.json", "missing transcript.json"))
    elif not _is_safe_file(run_dir, transcript_path):
        diagnostics.append(_diagnostic("unsafe_file_path", "transcript.json", "file is a symlink or escaped the run"))
    else:
        try:
            transcript_bytes = transcript_path.read_bytes()
            transcript = RunTranscript.model_validate_json(transcript_bytes)
            _parse_canonical_json(transcript_bytes, label="transcript")
        except Exception as exc:
            diagnostics.append(_diagnostic("invalid_transcript", "transcript.json", str(exc)))

    completion_path = run_dir / "completion.json"
    if completion_path.is_file() and _is_safe_file(run_dir, completion_path):
        try:
            completion_bytes = completion_path.read_bytes()
            completion = CompletionRecord.model_validate_json(completion_bytes)
            _parse_canonical_json(completion_bytes, label="completion")
        except Exception as exc:
            diagnostics.append(_diagnostic("invalid_completion", "completion.json", str(exc)))
    elif not completion_path.exists():
        diagnostics.append(
            _diagnostic(
                "incomplete_run",
                "completion.json",
                "completion.json is absent; the run may have crashed or been abandoned",
                severity="error" if require_complete else "warning",
            )
        )
    else:
        diagnostics.append(_diagnostic("unsafe_file_path", "completion.json", "file is a symlink or escaped the run"))

    if manifest is not None:
        if manifest.run_id != run_dir.name:
            diagnostics.append(
                _diagnostic("run_directory_mismatch", "manifest.json", "manifest run_id does not match directory name")
            )
    if manifest is not None and transcript is not None:
        if transcript.run_id != manifest.run_id or transcript.agent_class != manifest.agent_class:
            diagnostics.append(
                _diagnostic("transcript_identity_mismatch", "transcript.json", "identity does not match manifest")
            )
        if transcript.input != manifest.input:
            diagnostics.append(
                _diagnostic("transcript_input_mismatch", "transcript.json", "input does not match manifest")
            )
    if completion is not None:
        if manifest is not None and completion.run_id != manifest.run_id:
            diagnostics.append(_diagnostic("completion_identity_mismatch", "completion.json", "run_id mismatch"))
        transcript_path = run_dir / "transcript.json"
        if transcript_path.is_file() and _sha256(transcript_path.read_bytes()) != completion.transcript_sha256:
            diagnostics.append(
                _diagnostic("completion_hash_mismatch", "completion.json", "transcript SHA-256 does not match")
            )
        if transcript is not None and completion.status != "capture_error" and completion.status != transcript.status:
            diagnostics.append(
                _diagnostic("completion_status_mismatch", "completion.json", "status does not match transcript")
            )

    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        diagnostics.append(_diagnostic("missing_file", "events.jsonl", "missing events.jsonl"))
    elif not _is_safe_file(run_dir, events_path):
        diagnostics.append(_diagnostic("unsafe_file_path", "events.jsonl", "file is a symlink or escaped the run"))
    else:
        for expected_sequence, line in enumerate(events_path.read_bytes().splitlines()):
            event_path = f"events.jsonl:{expected_sequence + 1}"
            try:
                event = RunEvent.model_validate_json(line)
                _parse_canonical_json(line, label="event")
            except Exception as exc:
                diagnostics.append(_diagnostic("invalid_event", event_path, str(exc)))
                continue
            if event.sequence != expected_sequence:
                diagnostics.append(
                    _diagnostic(
                        "noncontiguous_event_sequence",
                        event_path,
                        f"expected sequence {expected_sequence}, found {event.sequence}",
                    )
                )

    committed: list[tuple[ModelCallMetadata, bytes, bytes]] = []
    identities: set[tuple[int, int]] = set()
    for step_dir in _step_directories(run_dir, diagnostics):
        loaded = _read_metadata(run_dir, step_dir, diagnostics)
        if loaded is None:
            continue
        metadata, observation, request = loaded
        identity = (metadata.step, metadata.attempt)
        if identity in identities:
            diagnostics.append(
                _diagnostic(
                    "duplicate_call_identity",
                    (step_dir / "metadata.json").relative_to(run_dir).as_posix(),
                    f"duplicate metadata identity {identity}",
                )
            )
        identities.add(identity)
        if manifest is not None:
            expected_kind = observation_kind_for_agent(manifest.agent_class)
            if (
                metadata.run_id != manifest.run_id
                or metadata.agent_class != manifest.agent_class
                or metadata.observation_kind != expected_kind
            ):
                diagnostics.append(
                    _diagnostic(
                        "metadata_identity_mismatch",
                        (step_dir / "metadata.json").relative_to(run_dir).as_posix(),
                        "metadata does not match the run manifest",
                    )
                )
        if metadata.capture_completeness == "complete" and metadata.missing_targets:
            diagnostics.append(
                _diagnostic(
                    "completeness_mismatch",
                    (step_dir / "metadata.json").relative_to(run_dir).as_posix(),
                    "complete capture declares missing targets",
                )
            )
        if metadata.capture_completeness == "partial" and not metadata.missing_targets:
            diagnostics.append(
                _diagnostic(
                    "partial_capture_without_missing_targets",
                    (step_dir / "metadata.json").relative_to(run_dir).as_posix(),
                    "partial capture does not identify a missing target or frame",
                )
            )
        if set(metadata.transform) != {"name", "version"} or not metadata.transform.get("name"):
            diagnostics.append(
                _diagnostic(
                    "invalid_transform",
                    (step_dir / "metadata.json").relative_to(run_dir).as_posix(),
                    "transform must contain exactly nonblank name and version fields",
                )
            )
        if "authorization_headers" not in metadata.redactions or len(metadata.redactions) != len(
            set(metadata.redactions)
        ):
            diagnostics.append(
                _diagnostic(
                    "invalid_redactions",
                    (step_dir / "metadata.json").relative_to(run_dir).as_posix(),
                    "redactions must uniquely declare authorization_headers",
                )
            )
        committed.append((metadata, observation, request))

    attempts_by_step: defaultdict[int, set[int]] = defaultdict(set)
    for metadata, _, _ in committed:
        attempts_by_step[metadata.step].add(metadata.attempt)
    if attempts_by_step:
        steps = sorted(attempts_by_step)
        expected_steps = list(range(steps[-1] + 1))
        if steps != expected_steps:
            diagnostics.append(
                _diagnostic("noncontiguous_steps", "observations", f"expected steps {expected_steps}, found {steps}")
            )
        for step, attempts in attempts_by_step.items():
            found = sorted(attempts)
            expected_attempts = list(range(found[-1] + 1))
            if found != expected_attempts:
                diagnostics.append(
                    _diagnostic(
                        "noncontiguous_attempts",
                        "observations",
                        f"step {step} expected attempts {expected_attempts}, found {found}",
                    )
                )

    committed_by_identity = {
        (
            metadata.step,
            metadata.attempt,
        ): metadata
        for metadata, _, _ in committed
    }
    if transcript is not None:
        if completion is not None and transcript.finished_at is None:
            diagnostics.append(
                _diagnostic("completed_transcript_unfinished", "transcript.json", "finished_at is absent")
            )
        for index, transcript_step in enumerate(transcript.steps):
            identity = (transcript_step.step, transcript_step.attempt)
            if identity not in identities:
                diagnostics.append(
                    _diagnostic(
                        "transcript_references_uncommitted_call",
                        f"transcript.json:steps[{index}]",
                        f"no committed model call for {identity}",
                    )
                )
                continue
            metadata = committed_by_identity[identity]
            directory = _step_directory_name(*identity)
            expected_observation_path = f"observations/{directory}/{metadata.observation_file}"
            expected_request_path = f"observations/{directory}/{metadata.model_request_file}"
            if (
                transcript_step.observation.kind != metadata.observation_kind
                or transcript_step.observation.artifact_path != expected_observation_path
                or transcript_step.observation.sha256 != metadata.observation_sha256
                or transcript_step.observation.byte_length != metadata.observation_bytes
                or transcript_step.model_request.artifact_path != expected_request_path
                or transcript_step.model_request.sha256 != metadata.model_request_sha256
                or transcript_step.model_request.byte_length != metadata.model_request_bytes
            ):
                diagnostics.append(
                    _diagnostic(
                        "transcript_artifact_mismatch",
                        f"transcript.json:steps[{index}]",
                        "artifact references do not match committed metadata",
                    )
                )

    for temp_path in sorted(run_dir.rglob("*.tmp")):
        diagnostics.append(
            _diagnostic(
                "uncommitted_temp_file",
                temp_path.relative_to(run_dir).as_posix(),
                "temporary file remains from an interrupted write",
            )
        )
    return diagnostics


def scan_model_observations(archive_root: Path, *, require_complete: bool = False) -> ObservationScanResult:
    """Return valid committed observations and explicit diagnostics for excluded evidence."""

    records: list[ObservationRecord] = []
    diagnostics: list[VerificationDiagnostic] = []
    runs_root = archive_root.resolve() / "runs"
    if not runs_root.is_dir():
        diagnostics.append(_diagnostic("missing_runs_root", "runs", f"directory does not exist: {runs_root}"))
        return ObservationScanResult(records=records, diagnostics=diagnostics)
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        manifest_path = run_dir / "manifest.json"
        try:
            if not _is_safe_file(run_dir, manifest_path):
                raise ArchiveError("manifest is missing, a symlink, or outside the run")
            manifest_bytes = manifest_path.read_bytes()
            manifest = RunManifest.model_validate_json(manifest_bytes)
            _parse_canonical_json(manifest_bytes, label="manifest")
        except Exception as exc:
            diagnostics.append(
                _diagnostic(
                    "invalid_manifest_excluded",
                    f"{run_dir.name}/manifest.json",
                    f"run excluded because its manifest is unavailable or invalid: {exc}",
                )
            )
            continue
        if manifest.run_id != run_dir.name:
            diagnostics.append(
                _diagnostic(
                    "run_directory_mismatch",
                    f"{run_dir.name}/manifest.json",
                    "run excluded because manifest run_id does not match its directory",
                )
            )
            continue
        if require_complete:
            completion_path = run_dir / "completion.json"
            try:
                if not _is_safe_file(run_dir, completion_path):
                    raise ArchiveError("completion is missing, a symlink, or outside the run")
                completion_bytes = completion_path.read_bytes()
                completion = CompletionRecord.model_validate_json(completion_bytes)
                _parse_canonical_json(completion_bytes, label="completion")
                transcript_path = run_dir / "transcript.json"
                if not _is_safe_file(run_dir, transcript_path):
                    raise ArchiveError("transcript is missing, a symlink, or outside the run")
                transcript_bytes = transcript_path.read_bytes()
                transcript = RunTranscript.model_validate_json(transcript_bytes)
                _parse_canonical_json(transcript_bytes, label="transcript")
                if completion.run_id != manifest.run_id or _sha256(transcript_bytes) != completion.transcript_sha256:
                    raise ArchiveError("completion identity or transcript hash mismatch")
                if transcript.run_id != manifest.run_id or transcript.finished_at is None:
                    raise ArchiveError("completed transcript identity or finished_at is invalid")
                if completion.status != "capture_error" and completion.status != transcript.status:
                    raise ArchiveError("completion status does not match transcript status")
            except Exception as exc:
                diagnostics.append(
                    _diagnostic(
                        "incomplete_run_excluded",
                        f"{run_dir.name}/completion.json",
                        f"run excluded because completion evidence is absent or invalid: {exc}",
                    )
                )
                continue
        run_diagnostics: list[VerificationDiagnostic] = []
        for step_dir in _step_directories(run_dir, run_diagnostics):
            call_diagnostics: list[VerificationDiagnostic] = []
            loaded = _read_metadata(run_dir, step_dir, call_diagnostics)
            if loaded is None or any(item.severity == "error" for item in call_diagnostics):
                diagnostics.extend(
                    item.model_copy(update={"path": f"{run_dir.name}/{item.path}"}) for item in call_diagnostics
                )
                continue
            metadata, observation, _ = loaded
            expected_kind = observation_kind_for_agent(manifest.agent_class)
            if (
                metadata.run_id != manifest.run_id
                or metadata.agent_class != manifest.agent_class
                or metadata.observation_kind != expected_kind
            ):
                diagnostics.append(
                    _diagnostic(
                        "metadata_identity_mismatch",
                        f"{run_dir.name}/observations/{step_dir.name}/metadata.json",
                        "call excluded because metadata does not match the manifest",
                    )
                )
                continue
            records.append(
                ObservationRecord(
                    run_id=metadata.run_id,
                    agent_class=metadata.agent_class,
                    step=metadata.step,
                    attempt=metadata.attempt,
                    observation_kind=metadata.observation_kind,
                    observation_bytes=observation,
                    sha256=metadata.observation_sha256,
                    capture_completeness=metadata.capture_completeness,
                    missing_targets=metadata.missing_targets,
                    transform=metadata.transform,
                    reachability=metadata.reachability,
                )
            )
        diagnostics.extend(item.model_copy(update={"path": f"{run_dir.name}/{item.path}"}) for item in run_diagnostics)
    records.sort(key=lambda record: (record.run_id, record.step, record.attempt))
    return ObservationScanResult(records=records, diagnostics=diagnostics)


def iter_model_observations(
    archive_root: Path,
    *,
    require_complete: bool = False,
    diagnostics: list[VerificationDiagnostic] | None = None,
) -> Iterator[ObservationRecord]:
    """Yield valid evidence; optionally collect diagnostics for excluded calls and runs."""

    result = scan_model_observations(archive_root, require_complete=require_complete)
    if diagnostics is not None:
        diagnostics.extend(result.diagnostics)
    yield from result.records


def scan_run_observations(run_dir: Path, *, require_complete: bool = True) -> ObservationScanResult:
    """Return verified model-visible observations for one run without rescanning the archive root."""

    run_dir = run_dir.resolve()
    diagnostics = verify_run(run_dir, require_complete=require_complete)
    if any(item.severity == "error" for item in diagnostics):
        return ObservationScanResult(records=[], diagnostics=diagnostics)
    try:
        manifest_bytes = (run_dir / "manifest.json").read_bytes()
        manifest = RunManifest.model_validate_json(manifest_bytes)
        _parse_canonical_json(manifest_bytes, label="manifest")
    except Exception as exc:
        diagnostics.append(_diagnostic("invalid_manifest_excluded", "manifest.json", str(exc)))
        return ObservationScanResult(records=[], diagnostics=diagnostics)

    records: list[ObservationRecord] = []
    for step_dir in _step_directories(run_dir, diagnostics):
        call_diagnostics: list[VerificationDiagnostic] = []
        loaded = _read_metadata(run_dir, step_dir, call_diagnostics)
        diagnostics.extend(call_diagnostics)
        if loaded is None or any(item.severity == "error" for item in call_diagnostics):
            continue
        metadata, observation, _ = loaded
        expected_kind = observation_kind_for_agent(manifest.agent_class)
        if (
            metadata.run_id != manifest.run_id
            or metadata.agent_class != manifest.agent_class
            or metadata.observation_kind != expected_kind
        ):
            diagnostics.append(
                _diagnostic(
                    "metadata_identity_mismatch",
                    f"observations/{step_dir.name}/metadata.json",
                    "call excluded because metadata does not match the manifest",
                )
            )
            continue
        records.append(
            ObservationRecord(
                run_id=metadata.run_id,
                agent_class=metadata.agent_class,
                step=metadata.step,
                attempt=metadata.attempt,
                observation_kind=metadata.observation_kind,
                observation_bytes=observation,
                sha256=metadata.observation_sha256,
                capture_completeness=metadata.capture_completeness,
                missing_targets=metadata.missing_targets,
                transform=metadata.transform,
                reachability=metadata.reachability,
            )
        )
    records.sort(key=lambda record: (record.step, record.attempt))
    return ObservationScanResult(records=records, diagnostics=diagnostics)
