"""Strict schemas for the level-0 validation gate."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from webagents.schemas import MODEL_ID_PATTERN, AgentClass

CONTROL_SCHEMA_VERSION = "1.0"
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentClassSpec(StrictModel):
    id: AgentClass
    runner: Literal["dom", "ax", "vision", "cdp"]

    @model_validator(mode="after")
    def validate_pair(self) -> AgentClassSpec:
        expected = {
            "dom_extraction": "dom",
            "accessibility_tree": "ax",
            "vision": "vision",
            "cdp_frame_traversal": "cdp",
        }[self.id]
        if self.runner != expected:
            raise ValueError(f"{self.id} must use runner {expected}")
        return self


class ModelSpec(StrictModel):
    id: str
    runner_model: str
    expected_provider: Literal["foundry", "openrouter"]
    expected_model: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("model id must be a lowercase slug")
        return value

    @model_validator(mode="after")
    def validate_model(self) -> ModelSpec:
        match = MODEL_ID_PATTERN.fullmatch(self.runner_model)
        if match is None:
            raise ValueError("runner_model must be provider-qualified")
        if match.group(1) != self.expected_provider:
            raise ValueError("expected_provider does not match runner_model")
        if match.group(2) != self.expected_model:
            raise ValueError("expected_model does not match runner_model")
        if self.expected_model.endswith("/latest") or self.expected_model == "latest":
            raise ValueError("floating model aliases are not permitted")
        return self


class CheckerSpec(StrictModel):
    name: Literal["json_endpoint_equals", "final_answer_equals"]
    version: Literal["1.0"] = "1.0"
    endpoint_path: str | None = None
    json_field: str | None = None
    expected: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_checker(self) -> CheckerSpec:
        if self.name == "json_endpoint_equals":
            if self.endpoint_path is None or self.json_field is None:
                raise ValueError("json_endpoint_equals requires endpoint_path and json_field")
            _validate_path(self.endpoint_path)
        elif self.endpoint_path is not None or self.json_field is not None:
            raise ValueError("final_answer_equals does not accept endpoint_path or json_field")
        return self


class FixtureSpec(StrictModel):
    kind: Literal["copy_value_form", "copy_value_form_large_target", "read_value"]
    source_value: str = Field(min_length=1)
    source_label: str = Field(default="Source", min_length=1)
    destination_label: str = Field(default="Destination", min_length=1)
    submit_label: str = Field(default="Submit", min_length=1)


class LevelZeroSpec(StrictModel):
    page_path: str
    reset_path: str
    prompt: str
    reachability_marker: str
    checker: CheckerSpec
    fixture: FixtureSpec

    @model_validator(mode="after")
    def validate_level0(self) -> LevelZeroSpec:
        _validate_path(self.page_path)
        _validate_path(self.reset_path)
        if not self.prompt.strip():
            raise ValueError("prompt must not be blank")
        if not self.reachability_marker or self.reachability_marker in self.prompt:
            raise ValueError("reachability marker must be nonblank and absent from the prompt")
        if self.checker.expected != self.fixture.source_value:
            raise ValueError("the checker must preserve the fixture source value as the expected answer/state")
        return self


class TaskRevisionSpec(StrictModel):
    schema_version: Literal["1.0"] = CONTROL_SCHEMA_VERSION
    task_id: str
    revision: int = Field(ge=1)
    semantic_goal: str = Field(min_length=1)
    level0: LevelZeroSpec

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("task_id must be a lowercase slug")
        return value


class TaskRosterEntry(StrictModel):
    id: str
    active_revision: int = Field(ge=1)
    simplification_ladder: list[int] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("task id must be a lowercase slug")
        return value

    @model_validator(mode="after")
    def validate_ladder(self) -> TaskRosterEntry:
        revisions = [self.active_revision, *self.simplification_ladder]
        if revisions != sorted(set(revisions)):
            raise ValueError("task revisions must be unique and strictly increasing")
        return self


class ControlPolicy(StrictModel):
    repeats: int = Field(default=3, ge=1)
    required_successes: int = Field(default=3, ge=1)
    max_infrastructure_retries: int = Field(default=2, ge=0)
    max_steps: int = Field(default=30, ge=1)
    timeout_seconds: float = Field(default=180.0, gt=0)
    schedule_seed: int

    @model_validator(mode="after")
    def validate_threshold(self) -> ControlPolicy:
        if self.required_successes != self.repeats:
            raise ValueError("level-0 admission is strict: required_successes must equal repeats")
        return self


class HarnessSpec(StrictModel):
    kind: Literal["builtin", "external"] = "builtin"
    host: str = "127.0.0.1"
    port: int = Field(default=0, ge=0, le=65535)
    base_url: str | None = None
    health_path: str = "/health"

    @model_validator(mode="after")
    def validate_harness(self) -> HarnessSpec:
        _validate_path(self.health_path)
        if self.kind == "external" and self.base_url is None:
            raise ValueError("external harness requires base_url")
        if self.kind == "builtin" and self.base_url is not None:
            raise ValueError("builtin harness chooses its own base_url")
        if self.kind == "builtin" and self.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("builtin harness must bind to loopback only")
        if self.base_url is not None:
            parts = urlsplit(self.base_url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                raise ValueError("external base_url must be an absolute HTTP(S) URL without credentials")
        return self


class ControlSpec(StrictModel):
    schema_version: Literal["1.0"] = CONTROL_SCHEMA_VERSION
    classes: list[AgentClassSpec]
    models: list[ModelSpec]
    tasks: list[TaskRosterEntry]
    policy: ControlPolicy
    harness: HarnessSpec = Field(default_factory=HarnessSpec)

    @model_validator(mode="after")
    def validate_rosters(self) -> ControlSpec:
        roster = frozenset((item.id, item.runner) for item in self.classes)
        two_class = {
            ("dom_extraction", "dom"),
            ("accessibility_tree", "ax"),
        }
        four_class = two_class | {("vision", "vision"), ("cdp_frame_traversal", "cdp")}
        if roster not in {frozenset(two_class), frozenset(four_class)}:
            raise ValueError("class roster must contain the matched two-class or four-class architecture roster")
        if not self.models or not self.tasks:
            raise ValueError("model and task rosters must not be empty")
        for label, values in (
            ("class", [item.id for item in self.classes]),
            ("model", [item.id for item in self.models]),
            ("task", [item.id for item in self.tasks]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} roster ID")
        return self


class ResolvedTask(StrictModel):
    roster: TaskRosterEntry
    revision: TaskRevisionSpec
    definition_path: str
    definition_sha256: str


class MatrixRow(StrictModel):
    logical_trial_id: str
    class_id: AgentClass
    runner: Literal["dom", "ax", "vision", "cdp"]
    task_id: str
    task_revision: int
    task_definition_sha256: str
    model_id: str
    runner_model: str
    repeat: int = Field(ge=1)
    schedule_index: int = Field(ge=0)


FailureClassification = Literal[
    "PASS",
    "TASK_FAILURE",
    "INFRASTRUCTURE_FAILURE",
    "EVIDENCE_FAILURE",
    "FIXTURE_FAILURE",
    "CONFIGURATION_FAILURE",
]


class TrialResult(StrictModel):
    logical_trial_id: str
    class_id: AgentClass
    task_id: str
    task_revision: int
    model_id: str
    repeat: int
    infrastructure_attempt: int = Field(ge=0)
    run_id: str | None = None
    classification: FailureClassification
    runner_status: str | None = None
    runner_success_claimed: bool | None = None
    archive_valid: bool = False
    archive_diagnostics: list[str] = Field(default_factory=list)
    reachable: bool | None = None
    capture_complete: bool | None = None
    checker_passed: bool | None = None
    checker_detail: str | None = None
    manifest_sha256: str | None = None
    transcript_sha256: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    actions: int = Field(default=0, ge=0)
    started_at: datetime
    finished_at: datetime


class CellResult(StrictModel):
    class_id: str
    task_id: str
    task_revision: int
    model_id: str
    successes: int = Field(ge=0)
    required_successes: int = Field(ge=1)
    trials: int = Field(ge=0)
    admitted: bool
    classifications: dict[str, int]


class TaskResult(StrictModel):
    task_id: str
    task_revision: int
    admitted: bool
    next_revision: int | None = None
    cells: list[CellResult]


class ImplementationIdentity(StrictModel):
    source_sha256: str
    transcript_schema_version: str
    archive_schema_version: str
    dom_framework: str
    dom_prompt_version: str
    ax_framework: str
    ax_prompt_version: str
    vision_framework: str = "not-recorded"
    vision_prompt_version: str = "not-recorded"
    cdp_framework: str = "not-recorded"
    cdp_prompt_version: str = "not-recorded"


class TaskIdentity(StrictModel):
    id: str
    revision: int = Field(ge=1)


class ControlValidationReport(StrictModel):
    schema_version: Literal["1.0"] = CONTROL_SCHEMA_VERSION
    validation_id: str
    verdict: Literal["pass", "task_failure", "blocked"]
    control_spec_sha256: str
    matrix_sha256: str
    results_sha256: str
    implementation: ImplementationIdentity
    active_tasks: list[TaskIdentity]
    task_results: list[TaskResult]
    blocking_classifications: list[FailureClassification] = Field(default_factory=list)
    trial_count: int = Field(ge=0)
    passed_at: datetime | None = None


class ControlReceipt(StrictModel):
    schema_version: Literal["1.0"] = CONTROL_SCHEMA_VERSION
    validation_id: str
    verdict: Literal["pass"] = "pass"
    control_spec_sha256: str
    matrix_sha256: str
    results_sha256: str
    implementation: ImplementationIdentity
    classes: list[str]
    models: list[str]
    tasks: list[TaskIdentity]
    policy: dict[str, int]
    passed_at: datetime


class SweepIdentity(StrictModel):
    control_spec_sha256: str
    implementation: ImplementationIdentity
    classes: list[str]
    models: list[str]
    tasks: list[TaskIdentity]
    policy: dict[str, int]


def _validate_path(value: str) -> str:
    if not _PATH_PATTERN.fullmatch(value) or ".." in Path(value).parts:
        raise ValueError("harness paths must be absolute URL paths without traversal")
    return value
