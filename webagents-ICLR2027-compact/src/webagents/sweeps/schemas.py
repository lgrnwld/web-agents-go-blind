"""Strict, versioned schemas for structural-containment sweeps."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from webagents.control.schemas import ControlReceipt, ImplementationIdentity, ModelSpec, ResolvedTask

SWEEP_SCHEMA_VERSION = "1.0"
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256_PATTERN = r"^[a-f0-9]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StructuralCondition(StrictModel):
    id: str
    kind: Literal["flat", "iframe", "shadow", "canvas"]
    iframe_origin: Literal["same", "cross"] | None = None
    iframe_depth: int = Field(default=0, ge=0, le=3)
    shadow_modes: list[Literal["open", "closed"]] = Field(default_factory=list)
    expected_reachability: Literal["present", "absent", "open_question"] = "open_question"
    dom_expected_reachability: Literal["present", "absent", "open_question"] = "open_question"

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("condition id must be a lowercase slug")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> StructuralCondition:
        if self.kind == "flat":
            if self.iframe_origin is not None or self.iframe_depth != 0 or self.shadow_modes:
                raise ValueError("flat condition cannot declare iframe or shadow parameters")
        elif self.kind == "iframe":
            if self.iframe_origin is None or self.iframe_depth < 1 or self.shadow_modes:
                raise ValueError("iframe condition requires origin and depth 1..3 only")
        elif self.kind == "shadow":
            if self.iframe_origin is not None or self.iframe_depth != 0 or not self.shadow_modes:
                raise ValueError("shadow condition requires one or more root modes only")
            if len(self.shadow_modes) > 3:
                raise ValueError("shadow nesting is limited to three explicitly declared roots")
        elif self.iframe_origin is not None or self.iframe_depth != 0 or self.shadow_modes:
            raise ValueError("canvas condition cannot declare iframe or shadow parameters")
        return self


class AxisLevel(StrictModel):
    id: str
    condition_id: str

    @field_validator("id", "condition_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("axis level and condition ids must be lowercase slugs")
        return value


class SweepAxis(StrictModel):
    id: str
    levels: list[AxisLevel]

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("axis id must be a lowercase slug")
        return value

    @model_validator(mode="after")
    def validate_levels(self) -> SweepAxis:
        if len(self.levels) < 2:
            raise ValueError("every axis requires at least two ordered levels")
        ids = [level.id for level in self.levels]
        if len(ids) != len(set(ids)):
            raise ValueError("axis level ids must be unique within an axis")
        return self


def _validate_condition_inventory(
    conditions: list[StructuralCondition],
    axes: list[SweepAxis],
) -> None:
    condition_ids = [condition.id for condition in conditions]
    if not condition_ids or len(condition_ids) != len(set(condition_ids)):
        raise ValueError("condition ids must be nonempty and unique")
    if condition_ids.count("level-0") != 1:
        raise ValueError("the deduplicated inventory must contain exactly one level-0 condition")
    axis_ids = [axis.id for axis in axes]
    if not axis_ids or len(axis_ids) != len(set(axis_ids)):
        raise ValueError("axis ids must be nonempty and unique")
    referenced = [level.condition_id for axis in axes for level in axis.levels]
    unknown = sorted(set(referenced) - set(condition_ids))
    unused = sorted(set(condition_ids) - set(referenced))
    if unknown:
        raise ValueError(f"axes reference unknown conditions: {unknown}")
    if unused:
        raise ValueError(f"conditions are not referenced by any axis: {unused}")
    if any(axis.levels[0].condition_id != "level-0" for axis in axes):
        raise ValueError("every one-factor-at-a-time axis must begin at shared level-0")


class StructuralConditionInventory(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    inventory_id: str
    conditions: list[StructuralCondition]
    axes: list[SweepAxis]

    @field_validator("inventory_id")
    @classmethod
    def validate_inventory_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("inventory_id must be a lowercase slug")
        return value

    @model_validator(mode="after")
    def validate_inventory(self) -> StructuralConditionInventory:
        _validate_condition_inventory(self.conditions, self.axes)
        return self


class SweepAllocation(StrictModel):
    valid_trials_per_cell: int = Field(default=10, ge=1)
    max_infrastructure_retries: int = Field(default=2, ge=0)


class SweepRuntime(StrictModel):
    max_steps: int = Field(default=30, ge=1)
    timeout_seconds: float = Field(default=180.0, gt=0)


class SweepAnalysis(StrictModel):
    confidence_interval: Literal["wilson-95"] = "wilson-95"
    blindness_rubric: Literal["phrase-v1"] = "phrase-v1"
    randomization_seed: int


class SweepHarnessSpec(StrictModel):
    kind: Literal["builtin"] = "builtin"
    host: Literal["127.0.0.1", "localhost"] = "127.0.0.1"
    primary_port: int = Field(default=0, ge=0, le=65535)
    cross_origin_port: int = Field(default=0, ge=0, le=65535)

    @model_validator(mode="after")
    def validate_ports(self) -> SweepHarnessSpec:
        if self.primary_port and self.primary_port == self.cross_origin_port:
            raise ValueError("primary and cross-origin ports must differ")
        return self


class AccessibilitySweepSpec(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    sweep_id: str
    agent_class: Literal["accessibility_tree", "vision", "cdp_frame_traversal"] = "accessibility_tree"
    runner: Literal["ax", "vision", "cdp"] = "ax"
    observation_kind: Literal["ax_tree", "screenshot", "cdp_dom_snapshot"] = "ax_tree"
    control_receipt: str
    models: Literal["from_control_receipt"] = "from_control_receipt"
    tasks: Literal["from_control_receipt"] = "from_control_receipt"
    primary_model: str
    condition_inventory: str | None = None
    reuse_level0_controls: bool = True
    observation_policy: Literal["unrestricted", "same_origin_only"] = "unrestricted"
    allocation: SweepAllocation = Field(default_factory=SweepAllocation)
    runtime: SweepRuntime = Field(default_factory=SweepRuntime)
    analysis: SweepAnalysis
    harness: SweepHarnessSpec = Field(default_factory=SweepHarnessSpec)
    conditions: list[StructuralCondition]
    axes: list[SweepAxis]

    @field_validator("sweep_id")
    @classmethod
    def validate_sweep_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("sweep_id must be a lowercase slug")
        return value

    @model_validator(mode="after")
    def validate_inventory(self) -> AccessibilitySweepSpec:
        if self.allocation.valid_trials_per_cell != 10:
            raise ValueError("Plan 5 requires exactly 10 valid trials per cell")
        expected = {
            "accessibility_tree": ("ax", "ax_tree"),
            "vision": ("vision", "screenshot"),
            "cdp_frame_traversal": ("cdp", "cdp_dom_snapshot"),
        }[self.agent_class]
        if (self.runner, self.observation_kind) != expected:
            raise ValueError(f"{self.agent_class} requires runner/observation {expected}")
        _validate_condition_inventory(self.conditions, self.axes)
        return self


class DomLightAllocation(StrictModel):
    primary_valid_trials_per_cell: int = Field(default=10, ge=1)
    additional_valid_trials_per_cell: int = Field(default=1, ge=1)
    max_infrastructure_retries: int = Field(default=2, ge=0)
    divergence_followup_trials: Literal[0] = 0

    @model_validator(mode="after")
    def validate_additional_allocation(self) -> DomLightAllocation:
        if self.additional_valid_trials_per_cell not in {1, 10}:
            raise ValueError("additional allocation must be exactly one or ten trials per cell")
        return self


class DomLightSweepSpec(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    sweep_id: str
    agent_class: Literal["dom_extraction"] = "dom_extraction"
    control_receipt: str
    primary_model: str
    primary_model_rationale: str = Field(min_length=20)
    confirmation_rule: Literal["primary-majority-v1"] = "primary-majority-v1"
    additional_models: Literal["from_control_receipt_except_primary"] = "from_control_receipt_except_primary"
    tasks: Literal["from_control_receipt"] = "from_control_receipt"
    condition_inventory: str | None = None
    inherit_results_from: str | None = None
    reuse_level0_controls: bool = True
    observation_policy: Literal["unrestricted", "same_origin_only"] = "unrestricted"
    allocation: DomLightAllocation = Field(default_factory=DomLightAllocation)
    runtime: SweepRuntime = Field(default_factory=SweepRuntime)
    analysis: SweepAnalysis
    harness: SweepHarnessSpec = Field(default_factory=SweepHarnessSpec)
    conditions: list[StructuralCondition]
    axes: list[SweepAxis]

    @field_validator("sweep_id")
    @classmethod
    def validate_sweep_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("sweep_id must be a lowercase slug")
        return value

    @model_validator(mode="after")
    def validate_design(self) -> DomLightSweepSpec:
        if self.allocation.primary_valid_trials_per_cell != 10:
            raise ValueError("Plan 6 requires exactly 10 primary-model trials per cell")
        _validate_condition_inventory(self.conditions, self.axes)
        return self


class ResolvedAccessibilitySweep(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    spec: AccessibilitySweepSpec
    control_receipt: ControlReceipt
    control_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    control_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    implementation: ImplementationIdentity
    models: list[ModelSpec]
    tasks: list[ResolvedTask]
    conditions_sha256: str = Field(pattern=_SHA256_PATTERN)
    resolved_at: datetime


class ResolvedDomLightSweep(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    spec: DomLightSweepSpec
    control_receipt: ControlReceipt
    control_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    control_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    implementation: ImplementationIdentity
    primary_model: ModelSpec
    additional_models: list[ModelSpec]
    tasks: list[ResolvedTask]
    conditions_sha256: str = Field(pattern=_SHA256_PATTERN)
    resolved_at: datetime


class SweepMatrixRow(StrictModel):
    logical_trial_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    agent_class: Literal["accessibility_tree", "vision", "cdp_frame_traversal"] = "accessibility_tree"
    runner: Literal["ax", "vision", "cdp"] = "ax"
    task_id: str
    task_revision: int = Field(ge=1)
    task_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    model_id: str
    runner_model: str
    observation_policy: Literal["unrestricted", "same_origin_only"] = "unrestricted"
    condition_id: str
    repeat: int = Field(ge=1)
    schedule_index: int = Field(ge=0)


class DomSweepMatrixRow(StrictModel):
    logical_trial_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    agent_class: Literal["dom_extraction"] = "dom_extraction"
    runner: Literal["dom"] = "dom"
    allocation_role: Literal["primary", "confirmation"]
    task_id: str
    task_revision: int = Field(ge=1)
    task_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    model_id: str
    runner_model: str
    observation_policy: Literal["unrestricted", "same_origin_only"] = "unrestricted"
    condition_id: str
    repeat: int = Field(ge=1)
    schedule_index: int = Field(ge=0)


SweepClassification = Literal[
    "VALID_OUTCOME",
    "INFRASTRUCTURE_FAILURE",
    "EVIDENCE_FAILURE",
    "FIXTURE_FAILURE",
    "CONFIGURATION_FAILURE",
]


class SweepTrialResult(StrictModel):
    logical_trial_id: str
    source: Literal["sweep", "control_reuse", "sweep_inheritance"] = "sweep"
    allocation_role: Literal["full", "primary", "confirmation"] = "full"
    task_id: str
    task_revision: int
    model_id: str
    condition_id: str
    repeat: int
    infrastructure_attempt: int = Field(ge=0)
    run_id: str | None = None
    classification: SweepClassification
    counts_toward_cell: bool = False
    runner_status: str | None = None
    runner_success_claimed: bool | None = None
    archive_valid: bool = False
    archive_diagnostics: list[str] = Field(default_factory=list)
    reachable: bool | None = None
    task_success: bool | None = None
    self_reported_blindness: bool | None = None
    capture_complete: bool | None = None
    missing_targets: list[str] = Field(default_factory=list)
    checker_detail: str | None = None
    manifest_sha256: str | None = None
    transcript_sha256: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    actions: int = Field(default=0, ge=0)
    provider_cost_usd: float | None = Field(default=None, ge=0)
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def validate_counting(self) -> SweepTrialResult:
        if self.counts_toward_cell:
            if self.classification != "VALID_OUTCOME":
                raise ValueError("only valid outcomes count toward a cell")
            if self.reachable is None or self.task_success is None or self.self_reported_blindness is None:
                raise ValueError("valid outcomes require all primary outcomes")
            if not self.archive_valid or self.capture_complete is not True or self.run_id is None:
                raise ValueError("valid outcomes require a complete verified archive")
        return self


class WilsonInterval(StrictModel):
    successes: int = Field(ge=0)
    trials: int = Field(ge=0)
    rate: float | None = Field(default=None, ge=0, le=1)
    lower: float | None = Field(default=None, ge=0, le=1)
    upper: float | None = Field(default=None, ge=0, le=1)


class CellAggregate(StrictModel):
    task_id: str
    task_revision: int
    model_id: str
    condition_id: str
    valid_trials: int = Field(ge=0)
    required_trials: int = Field(ge=1)
    complete: bool
    reachability: WilsonInterval
    task_success: WilsonInterval
    self_reported_blindness: WilsonInterval
    joint_outcomes: dict[str, int]
    mean_input_tokens: float | None = Field(default=None, ge=0)
    mean_output_tokens: float | None = Field(default=None, ge=0)
    mean_actions: float | None = Field(default=None, ge=0)
    mean_elapsed_seconds: float | None = Field(default=None, ge=0)
    mean_provider_cost_usd: float | None = Field(default=None, ge=0)
    reused_control_trials: int = Field(default=0, ge=0)


class AxisSummary(StrictModel):
    task_id: str
    task_revision: int
    model_id: str
    axis_id: str
    ordered_levels: list[str]
    condition_ids: list[str]
    reachability_rates: list[float | None]
    success_rates: list[float | None]
    reachability_auc: float | None = Field(default=None, ge=0, le=1)
    success_auc: float | None = Field(default=None, ge=0, le=1)
    mean_reachability_drop: float | None = None
    maximum_adjacent_reachability_drop: float | None = None


class SweepReport(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    sweep_id: str
    verdict: Literal["complete", "incomplete", "blocked"]
    resolved_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    conditions_sha256: str = Field(pattern=_SHA256_PATTERN)
    matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    results_sha256: str = Field(pattern=_SHA256_PATTERN)
    exclusions_sha256: str = Field(pattern=_SHA256_PATTERN)
    reused_controls_sha256: str = Field(pattern=_SHA256_PATTERN)
    required_cells: int = Field(ge=0)
    complete_cells: int = Field(ge=0)
    valid_trial_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    blocking_classifications: list[SweepClassification] = Field(default_factory=list)
    aggregates: list[CellAggregate]
    live_only_aggregates: list[CellAggregate]
    axis_summaries: list[AxisSummary]
    architecture_variance_status: str
    surprise_candidate_count: int = Field(default=0, ge=0)
    started_at: datetime
    finished_at: datetime


class SweepReceipt(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    sweep_id: str
    verdict: Literal["complete"] = "complete"
    resolved_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    conditions_sha256: str = Field(pattern=_SHA256_PATTERN)
    matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    results_sha256: str = Field(pattern=_SHA256_PATTERN)
    exclusions_sha256: str = Field(pattern=_SHA256_PATTERN)
    reused_controls_sha256: str = Field(pattern=_SHA256_PATTERN)
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    model_roster: list[str]
    tasks: list[str]
    condition_ids: list[str]
    valid_trials_per_cell: Literal[10] = 10
    completed_at: datetime


class SurpriseCandidate(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    observed_at: datetime
    sweep_id: str
    logical_trial_id: str
    run_id: str
    task_id: str
    model_id: str
    condition_id: str
    expectation: Literal["present", "absent", "open_question"]
    observed_reachable: bool
    summary: str
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    trigger: Literal["expectation_mismatch", "model_divergence"] = "expectation_mismatch"
    primary_model_id: str | None = None


class ModelConfirmation(StrictModel):
    task_id: str
    task_revision: int
    model_id: str
    condition_id: str
    run_id: str
    reachable: bool
    task_success: bool
    self_reported_blindness: bool
    primary_reachability_rate: float = Field(ge=0, le=1)
    primary_success_rate: float = Field(ge=0, le=1)
    primary_modal_reachability: bool | None
    primary_modal_success: bool | None
    reachability_agrees: bool | None
    success_agrees: bool | None
    divergent: bool


class DomLightReport(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    sweep_id: str
    verdict: Literal["complete", "incomplete", "blocked"]
    primary_model_id: str
    additional_model_ids: list[str]
    resolved_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    conditions_sha256: str = Field(pattern=_SHA256_PATTERN)
    matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    results_sha256: str = Field(pattern=_SHA256_PATTERN)
    exclusions_sha256: str = Field(pattern=_SHA256_PATTERN)
    reused_controls_sha256: str = Field(pattern=_SHA256_PATTERN)
    primary_required_cells: int = Field(ge=0)
    primary_complete_cells: int = Field(ge=0)
    confirmation_required_cells: int = Field(ge=0)
    confirmation_complete_cells: int = Field(ge=0)
    valid_trial_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    blocking_classifications: list[SweepClassification] = Field(default_factory=list)
    primary_aggregates: list[CellAggregate]
    live_only_primary_aggregates: list[CellAggregate]
    primary_axis_summaries: list[AxisSummary]
    additional_aggregates: list[CellAggregate] = Field(default_factory=list)
    additional_axis_summaries: list[AxisSummary] = Field(default_factory=list)
    model_confirmations: list[ModelConfirmation]
    divergent_confirmation_count: int = Field(default=0, ge=0)
    surprise_candidate_count: int = Field(default=0, ge=0)
    inherited_trial_count: int = Field(default=0, ge=0)
    interpretation_limit: str
    started_at: datetime
    finished_at: datetime


class DomLightReceipt(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    sweep_id: str
    verdict: Literal["complete"] = "complete"
    resolved_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    conditions_sha256: str = Field(pattern=_SHA256_PATTERN)
    matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    results_sha256: str = Field(pattern=_SHA256_PATTERN)
    exclusions_sha256: str = Field(pattern=_SHA256_PATTERN)
    reused_controls_sha256: str = Field(pattern=_SHA256_PATTERN)
    primary_aggregates_sha256: str = Field(pattern=_SHA256_PATTERN)
    model_confirmations_sha256: str = Field(pattern=_SHA256_PATTERN)
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    primary_model: str
    additional_models: list[str]
    tasks: list[str]
    condition_ids: list[str]
    primary_valid_trials_per_cell: Literal[10] = 10
    additional_valid_trials_per_cell: int = Field(default=1, ge=1)
    inherited_results_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    inherited_trial_count: int = Field(default=0, ge=0)
    completed_at: datetime

    @field_validator("additional_valid_trials_per_cell")
    @classmethod
    def validate_additional_allocation(cls, value: int) -> int:
        if value not in {1, 10}:
            raise ValueError("additional allocation must be exactly one or ten trials per cell")
        return value


class ReachabilityCorrection(StrictModel):
    logical_trial_id: str
    run_id: str
    task_id: str
    model_id: str
    condition_id: str
    previous_reachable: bool
    corrected_reachable: bool


class RescoreReceipt(StrictModel):
    schema_version: Literal["1.0"] = SWEEP_SCHEMA_VERSION
    verdict: Literal["complete"] = "complete"
    source_sweep_id: str
    corrected_analysis_id: str
    agent_class: Literal["dom_extraction", "accessibility_tree"]
    scoring_policy: Literal["initial-task-page-marker-v2"]
    source_resolved_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_matrix_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_results_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    corrected_results_sha256: str = Field(pattern=_SHA256_PATTERN)
    corrected_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    correction_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    valid_trial_count: int = Field(ge=0)
    changed_trial_count: int = Field(ge=0)
    created_at: datetime
