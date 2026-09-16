"""Strict schemas for the dated framework-surprise event stream."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from webagents.schemas import AgentClass

SURPRISE_SCHEMA_VERSION = "1.0"
SHA256_PATTERN = r"^[a-f0-9]{64}$"
EVENT_ID_PATTERN = re.compile(r"^SURPRISE-[0-9]{8}-[0-9]{3,}$")
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FrameworkExpectation(StrictModel):
    id: str
    agent_class: AgentClass
    metric: Literal[
        "reachability",
        "model_directional_consistency",
        "capture_topology",
        "implementation_identity",
    ]
    status: Literal[
        "mechanism_backed",
        "mechanism_backed_but_framework_contingent",
        "behavioral_open_question",
    ]
    expected_outcome: Literal["present", "absent", "consistent", "stable", "open_question"]
    condition_ids: list[str]
    statement: str = Field(min_length=20)
    mechanism: str = Field(min_length=20)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError("expectation ID must be a lowercase slug")
        return value

    @field_validator("condition_ids")
    @classmethod
    def validate_conditions(cls, value: list[str]) -> list[str]:
        if not value or len(value) != len(set(value)):
            raise ValueError("expectation condition_ids must be nonempty and unique")
        if any(not SLUG_PATTERN.fullmatch(item) for item in value):
            raise ValueError("expectation condition IDs must be lowercase slugs")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> FrameworkExpectation:
        if self.metric == "reachability" and self.expected_outcome not in {
            "present",
            "absent",
            "open_question",
        }:
            raise ValueError("reachability expectations require present, absent, or open_question")
        if self.metric == "model_directional_consistency" and self.expected_outcome != "consistent":
            raise ValueError("directional confirmation expectation must be consistent")
        return self


class ExpectationRegistry(StrictModel):
    schema_version: Literal["1.0"] = SURPRISE_SCHEMA_VERSION
    expectation_set: str
    frozen_at: datetime
    declared_sources: list[str]
    expectations: list[FrameworkExpectation]

    @field_validator("expectation_set")
    @classmethod
    def validate_set(cls, value: str) -> str:
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError("expectation_set must be a lowercase slug")
        return value

    @model_validator(mode="after")
    def validate_registry(self) -> ExpectationRegistry:
        if self.frozen_at.tzinfo is None:
            raise ValueError("frozen_at must be timezone-aware")
        ids = [item.id for item in self.expectations]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("expectation IDs must be nonempty and unique")
        if not self.declared_sources or len(self.declared_sources) != len(set(self.declared_sources)):
            raise ValueError("declared_sources must be nonempty and unique")
        return self


class FrameworkIdentity(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    commit: str = Field(min_length=1)


class BrowserIdentity(StrictModel):
    name: Literal["chromium"] | None = None
    revision: str | None = None
    version: str | None = None
    playwright_version: str | None = None


class SourceIdentity(StrictModel):
    repository_commit: str | None = None
    source_state: Literal["versioned", "dirty", "unversioned"]
    source_sha256: str = Field(pattern=SHA256_PATTERN)


class SchemaIdentity(StrictModel):
    archive: Literal["1.0"] = "1.0"
    transcript: Literal["1.0"] = "1.0"


class TaskIdentity(StrictModel):
    id: str
    revision: int = Field(ge=1)


class AxisLevelIdentity(StrictModel):
    axis: str
    level: str


class ConditionIdentity(StrictModel):
    id: str
    axes: list[AxisLevelIdentity]


class EnvironmentIdentity(StrictModel):
    host_platform: str = Field(min_length=1)
    python: str = Field(min_length=1)


class ProviderResponseIdentity(StrictModel):
    step: int = Field(ge=0)
    provider: str
    model: str
    response_id: str | None = None


class TransformIdentity(StrictModel):
    name: str = Field(min_length=1)
    version: str | None = None


EvidenceRole = Literal[
    "manifest",
    "transcript",
    "completion",
    "observation",
    "model_request",
    "metadata",
    "sweep_resolved_spec",
    "sweep_conditions",
    "sweep_matrix",
    "sweep_results",
    "sweep_candidate",
]


class EvidenceReference(StrictModel):
    run_id: str
    artifact_path: str
    role: EvidenceRole
    sha256: str = Field(pattern=SHA256_PATTERN)
    byte_length: int = Field(ge=0)

    @field_validator("artifact_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("evidence artifact_path must be a normalized relative POSIX path")
        if not value or value.startswith("."):
            raise ValueError("evidence artifact_path must not be hidden or blank")
        return value


class MarkerAssertion(StrictModel):
    marker_utf8: str = Field(min_length=1)
    marker_sha256: str = Field(pattern=SHA256_PATTERN)
    expected_present: bool | None
    observed_present: bool

    @model_validator(mode="after")
    def validate_marker_hash(self) -> MarkerAssertion:
        if hashlib.sha256(self.marker_utf8.encode("utf-8")).hexdigest() != self.marker_sha256:
            raise ValueError("marker_sha256 does not match marker_utf8")
        return self


EventStatus = Literal[
    "candidate",
    "confirmed",
    "not_reproduced",
    "fixture_issue",
    "evidence_issue",
    "model_specific",
    "implementation_change",
    "awaiting_followup",
]


class FrameworkSurpriseEvent(StrictModel):
    schema_version: Literal["1.0"] = SURPRISE_SCHEMA_VERSION
    event_id: str
    event_type: Literal["candidate", "adjudication", "correction", "upgrade"]
    observed_at: datetime
    observed_date: date
    logged_at: datetime
    expectation_set: str
    expectation_set_sha256: str = Field(pattern=SHA256_PATTERN)
    expectation_id: str
    summary: str = Field(min_length=10)
    agent_class: AgentClass
    framework: FrameworkIdentity
    browser: BrowserIdentity
    source: SourceIdentity
    schemas: SchemaIdentity
    prompt_version: str = Field(min_length=1)
    provider_qualified_model: str = Field(pattern=r"^(foundry|azure|openrouter)/[^\s]+$")
    provider_responses: list[ProviderResponseIdentity]
    environment: EnvironmentIdentity
    task: TaskIdentity
    condition: ConditionIdentity
    sweep_id: str
    logical_trial_id: str
    fixture_sha256: str = Field(pattern=SHA256_PATTERN)
    fixture_route: str
    expected_behavior: str = Field(min_length=10)
    observed_behavior: str = Field(min_length=10)
    marker_assertion: MarkerAssertion
    capture_complete: bool
    transforms: list[TransformIdentity]
    evidence: list[EvidenceReference]
    reproduction_id: str | None = None
    reproduction_record_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    status: EventStatus
    impact: list[Literal["results", "limitations", "methods", "result_interpretation", "upgrade_decision"]]
    paper_disposition: Literal["pending", "results", "limitations", "results_and_limitations", "not_applicable"]
    reporter: str = Field(min_length=2)
    updates: str | None = None
    supersedes: str | None = None
    previous_event_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    event_sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        if not EVENT_ID_PATTERN.fullmatch(value):
            raise ValueError("event_id must match SURPRISE-YYYYMMDD-NNN")
        return value

    @field_validator("fixture_route")
    @classmethod
    def validate_fixture_route(cls, value: str) -> str:
        if not value.startswith("/") or "://" in value or "@" in value:
            raise ValueError("fixture_route must be a credential-free absolute URL path")
        return value

    @model_validator(mode="after")
    def validate_event(self) -> FrameworkSurpriseEvent:
        if self.observed_at.tzinfo is None or self.logged_at.tzinfo is None:
            raise ValueError("event timestamps must be timezone-aware")
        if self.observed_at.astimezone(UTC).date() != self.observed_date:
            raise ValueError("observed_date must equal the UTC date of observed_at")
        if self.event_type == "candidate":
            if (
                self.status != "candidate"
                or self.updates is not None
                or self.reproduction_id is not None
                or self.reproduction_record_sha256 is not None
            ):
                raise ValueError("candidate events cannot update another event or reference a reproduction")
            if self.logged_at.astimezone(UTC).date() != self.observed_date:
                raise ValueError("candidate must be logged on the same UTC date as its source observation")
        elif self.updates is None:
            raise ValueError("non-candidate events must link to an earlier event through updates")
        if self.event_type == "adjudication" and self.status == "candidate":
            raise ValueError("adjudication cannot retain candidate status")
        if self.supersedes is not None and self.event_type != "correction":
            raise ValueError("only correction events may supersede another event")
        if (self.reproduction_id is None) != (self.reproduction_record_sha256 is None):
            raise ValueError("reproduction ID and record hash must be present together")
        if not self.evidence:
            raise ValueError("every event requires evidence references")
        if len(self.impact) != len(set(self.impact)):
            raise ValueError("impact values must be unique")
        if len(self.transforms) != len({(item.name, item.version) for item in self.transforms}):
            raise ValueError("transform identities must be unique")
        return self


class ReproductionRecord(StrictModel):
    schema_version: Literal["1.0"] = SURPRISE_SCHEMA_VERSION
    reproduction_id: str
    candidate_event_id: str
    run_id: str
    recorded_at: datetime
    identity_matches_candidate: bool
    marker_observed_present: bool
    candidate_behavior_reproduced: bool
    evidence: list[EvidenceReference]


class SurpriseDiagnostic(StrictModel):
    severity: Literal["error", "warning"]
    code: str
    event_id: str | None = None
    path: str
    message: str


class SurpriseLogReport(StrictModel):
    schema_version: Literal["1.0"] = SURPRISE_SCHEMA_VERSION
    valid: bool
    expectation_set: str
    expectation_set_sha256: str = Field(pattern=SHA256_PATTERN)
    event_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    confirmed_count: int = Field(ge=0)
    head_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    diagnostics: list[SurpriseDiagnostic]
    verified_at: datetime
