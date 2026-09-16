"""Strict, versioned schemas shared by agent runners and archive consumers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

TRANSCRIPT_SCHEMA_VERSION = "1.0"
ARCHIVE_SCHEMA_VERSION = "1.0"
RUN_ID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$|^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MODEL_ID_PATTERN = re.compile(r"^(foundry|openrouter)/([^\s/]+(?:/[^\s/]+)*)$")

type AgentClass = Literal[
    "dom_extraction",
    "accessibility_tree",
    "vision",
    "cdp_frame_traversal",
]
type ObservationKind = Literal[
    "serialized_dom",
    "ax_tree",
    "screenshot",
    "cdp_dom_snapshot",
]


def observation_kind_for_agent(agent_class: AgentClass) -> ObservationKind:
    if agent_class == "dom_extraction":
        return "serialized_dom"
    if agent_class == "accessibility_tree":
        return "ax_tree"
    if agent_class == "vision":
        return "screenshot"
    return "cdp_dom_snapshot"


class StrictModel(BaseModel):
    """Base model that refuses forward-incompatible, unknown fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RunRequest(StrictModel):
    page_url: str
    task_prompt: str
    model: str
    run_id: str | None = None
    max_steps: int = Field(default=30, gt=0)

    @field_validator("page_url")
    @classmethod
    def validate_page_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("page_url must be an absolute http:// or https:// URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError("page_url must not contain credentials")
        return value

    @field_validator("task_prompt")
    @classmethod
    def validate_task_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task_prompt must not be blank")
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not MODEL_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "model must use foundry/<deployment> or openrouter/<model>"
            )
        return value

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str | None) -> str | None:
        if value is not None and not RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("run_id contains unsafe characters or is longer than 128 characters")
        return value


class FrameworkIdentity(StrictModel):
    name: Literal["browser-use", "playwright"] = "browser-use"
    version: str
    commit: str


class RunInput(StrictModel):
    page_url: str
    task_prompt: str
    model: str
    observation_policy: Literal["unrestricted", "same_origin_only"] = "unrestricted"


class ArtifactReference(StrictModel):
    artifact_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_length: int = Field(ge=0)


class ObservationReference(ArtifactReference):
    kind: ObservationKind


class ToolCall(StrictModel):
    id: str | None = None
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(StrictModel):
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class ActionRecord(StrictModel):
    name: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ActionResultRecord(StrictModel):
    extracted_content: str | None = None
    error: str | None = None
    is_done: bool = False
    success: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class RunStep(StrictModel):
    step: int = Field(ge=0)
    attempt: int = Field(default=0, ge=0)
    timestamp: datetime
    url: str
    observation: ObservationReference
    model_request: ArtifactReference
    model_response: ModelResponse | None = None
    actions: list[ActionRecord] = Field(default_factory=list)
    action_results: list[ActionResultRecord] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    error: str | None = None


class FinalResult(StrictModel):
    answer: str | None = None
    success_claimed: bool = False
    error: str | None = None


class RunTranscript(StrictModel):
    schema_version: Literal["1.0"] = TRANSCRIPT_SCHEMA_VERSION
    run_id: str
    agent_class: AgentClass
    framework: FrameworkIdentity
    input: RunInput
    started_at: datetime
    finished_at: datetime | None = None
    status: Literal["success", "failure", "timeout", "infrastructure_error"]
    steps: list[RunStep] = Field(default_factory=list)
    final: FinalResult = Field(default_factory=FinalResult)


class RuntimeIdentity(StrictModel):
    started_at: datetime
    host_platform: str
    python: str


class ImplementationIdentity(StrictModel):
    repository_commit: str | None = None
    source_state: Literal["versioned", "dirty", "unversioned"]
    framework_name: str
    framework_version: str
    framework_commit: str
    playwright_version: str | None = None
    browser_version: str | None = None
    prompt_version: str


class CaptureIdentity(StrictModel):
    writer_version: Literal["1.0"] = "1.0"
    observation_encoding: Literal["utf-8", "binary"] = "utf-8"
    canonical_json: Literal["UTF-8, sorted keys, compact separators, no nonfinite numbers"] = (
        "UTF-8, sorted keys, compact separators, no nonfinite numbers"
    )


class RunManifest(StrictModel):
    archive_schema_version: Literal["1.0"] = ARCHIVE_SCHEMA_VERSION
    run_id: str
    agent_class: AgentClass
    input: RunInput
    runtime: RuntimeIdentity
    implementation: ImplementationIdentity
    capture: CaptureIdentity = Field(default_factory=CaptureIdentity)


class ReachabilityEvidence(StrictModel):
    """Privileged scorer evidence that is never included in the model request."""

    method: Literal["playwright-visible-target-v1"]
    visible: bool
    target_count: int = Field(ge=0)


class ModelCallMetadata(StrictModel):
    run_id: str
    step: int = Field(ge=0)
    attempt: int = Field(ge=0)
    agent_class: AgentClass
    observation_kind: ObservationKind
    observation_file: str = ""
    observation_sha256: str = ""
    observation_bytes: int = 0
    model_request_file: str = ""
    model_request_sha256: str = ""
    model_request_bytes: int = 0
    captured_at: datetime
    model_call_started_at: datetime
    transform: dict[str, Any] = Field(default_factory=lambda: {"name": "none", "version": None})
    capture_completeness: Literal["complete", "partial"] = "complete"
    missing_targets: list[str] = Field(default_factory=list)
    redactions: list[str] = Field(default_factory=lambda: ["authorization_headers"])
    reachability: ReachabilityEvidence | None = None


class ArtifactRefs(StrictModel):
    observation: ObservationReference
    model_request: ArtifactReference


class RunEvent(StrictModel):
    sequence: int = Field(ge=0)
    timestamp: datetime
    kind: str
    step: int | None = Field(default=None, ge=0)
    attempt: int | None = Field(default=None, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)


class CompletionRecord(StrictModel):
    archive_schema_version: Literal["1.0"] = ARCHIVE_SCHEMA_VERSION
    run_id: str
    status: Literal["success", "failure", "timeout", "infrastructure_error", "capture_error"]
    finished_at: datetime
    transcript_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ObservationRecord(StrictModel):
    run_id: str
    agent_class: AgentClass
    step: int
    attempt: int
    observation_kind: ObservationKind
    observation_bytes: bytes
    sha256: str
    capture_completeness: Literal["complete", "partial"]
    missing_targets: list[str] = Field(default_factory=list)
    transform: dict[str, Any]
    reachability: ReachabilityEvidence | None = None


class VerificationDiagnostic(StrictModel):
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str


class ObservationScanResult(StrictModel):
    records: list[ObservationRecord] = Field(default_factory=list)
    diagnostics: list[VerificationDiagnostic] = Field(default_factory=list)
