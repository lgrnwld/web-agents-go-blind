"""Load, validate, and control-bind the full AX sweep specification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from webagents.capture.archive import canonical_json_bytes
from webagents.control.receipt import assert_control_admitted, implementation_identity
from webagents.control.schemas import ControlReceipt, ControlSpec, ResolvedTask, SweepIdentity
from webagents.sweeps.schemas import (
    AccessibilitySweepSpec,
    DomLightSweepSpec,
    ResolvedAccessibilitySweep,
    ResolvedDomLightSweep,
    StructuralConditionInventory,
)


class SweepConfigurationError(ValueError):
    """The sweep specification or its control binding is invalid."""


@dataclass(frozen=True)
class ResolvedSweepBundle:
    resolved: ResolvedAccessibilitySweep
    receipt_path: Path
    project_root: Path


@dataclass(frozen=True)
class ResolvedDomSweepBundle:
    resolved: ResolvedDomLightSweep
    receipt_path: Path
    project_root: Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SweepConfigurationError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SweepConfigurationError(f"{path} must contain one YAML mapping")
    return value


def _resolve_inventory(value: dict[str, Any], *, spec_path: Path) -> dict[str, Any]:
    source = value.get("condition_inventory")
    if source is None:
        return value
    if not isinstance(source, str) or not source:
        raise SweepConfigurationError("condition_inventory must be a nonblank path")
    if "conditions" in value or "axes" in value:
        raise SweepConfigurationError("declare either condition_inventory or inline conditions/axes, not both")
    inventory_root = next(
        (parent for parent in (spec_path.parent, *spec_path.parents) if (parent / "pyproject.toml").is_file()),
        project_root(),
    )
    inventory_path = _declared_path(source, root=inventory_root)
    try:
        inventory = StructuralConditionInventory.model_validate(_yaml_mapping(inventory_path))
    except Exception as exc:
        raise SweepConfigurationError(f"invalid condition inventory {inventory_path}: {exc}") from exc
    return {
        **value,
        "conditions": [item.model_dump(mode="json") for item in inventory.conditions],
        "axes": [item.model_dump(mode="json") for item in inventory.axes],
    }


def load_sweep_spec(path: Path) -> AccessibilitySweepSpec:
    try:
        resolved_path = path.resolve()
        return AccessibilitySweepSpec.model_validate(
            _resolve_inventory(_yaml_mapping(resolved_path), spec_path=resolved_path)
        )
    except Exception as exc:
        raise SweepConfigurationError(f"invalid sweep spec {path}: {exc}") from exc


def load_dom_light_spec(path: Path) -> DomLightSweepSpec:
    try:
        resolved_path = path.resolve()
        return DomLightSweepSpec.model_validate(
            _resolve_inventory(_yaml_mapping(resolved_path), spec_path=resolved_path)
        )
    except Exception as exc:
        raise SweepConfigurationError(f"invalid DOM light sweep spec {path}: {exc}") from exc


def _declared_path(value: str, *, root: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _load_control_bundle(receipt_path: Path) -> tuple[ControlReceipt, ControlSpec, list[ResolvedTask], bytes]:
    try:
        receipt_bytes = receipt_path.read_bytes()
        receipt = ControlReceipt.model_validate_json(receipt_bytes)
        if canonical_json_bytes(receipt.model_dump(mode="json")) != receipt_bytes:
            raise ValueError("control receipt is not canonically encoded")
        resolved_bytes = (receipt_path.parent / "resolved-spec.json").read_bytes()
        resolved_value = json.loads(resolved_bytes)
        control = ControlSpec.model_validate(resolved_value["control"])
        task_revisions = [ResolvedTask.model_validate(item) for item in resolved_value["task_revisions"]]
    except Exception as exc:
        raise SweepConfigurationError(f"cannot load control evidence at {receipt_path}: {exc}") from exc
    return receipt, control, task_revisions, receipt_bytes


def resolve_sweep_spec(
    spec_path: Path,
    *,
    control_receipt_override: Path | None = None,
    root: Path | None = None,
) -> ResolvedSweepBundle:
    actual_root = (root or project_root()).resolve()
    spec = load_sweep_spec(spec_path)
    receipt_path = (
        control_receipt_override.resolve()
        if control_receipt_override is not None
        else _declared_path(spec.control_receipt, root=actual_root)
    )
    receipt, control, all_task_revisions, receipt_bytes = _load_control_bundle(receipt_path)
    current_implementation = implementation_identity(actual_root)
    sweep_identity = SweepIdentity(
        control_spec_sha256=receipt.control_spec_sha256,
        implementation=current_implementation,
        classes=receipt.classes,
        models=receipt.models,
        tasks=receipt.tasks,
        policy=receipt.policy,
    )
    try:
        assert_control_admitted(receipt_path, sweep_identity)
    except Exception as exc:
        raise SweepConfigurationError(f"level-0 control does not admit this sweep: {exc}") from exc

    model_by_runner_id = {model.runner_model: model for model in control.models}
    try:
        models = [model_by_runner_id[runner_model] for runner_model in receipt.models]
    except KeyError as exc:
        raise SweepConfigurationError(f"receipt model is absent from resolved control spec: {exc}") from exc
    if spec.primary_model not in receipt.models:
        raise SweepConfigurationError("primary_model must be one of the exact control-receipt model identifiers")

    active = {(item.id, item.revision) for item in receipt.tasks}
    task_by_identity = {(task.revision.task_id, task.revision.revision): task for task in all_task_revisions}
    missing = sorted(active - set(task_by_identity))
    if missing:
        raise SweepConfigurationError(f"active control task revisions are absent from resolved evidence: {missing}")
    tasks = [task_by_identity[(item.id, item.revision)] for item in receipt.tasks]

    condition_payload = {
        "conditions": [item.model_dump(mode="json") for item in spec.conditions],
        "axes": [item.model_dump(mode="json") for item in spec.axes],
    }
    conditions_sha256 = hashlib.sha256(canonical_json_bytes(condition_payload)).hexdigest()
    resolved = ResolvedAccessibilitySweep(
        spec=spec,
        control_receipt=receipt,
        control_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        control_spec_sha256=receipt.control_spec_sha256,
        implementation=current_implementation,
        models=models,
        tasks=tasks,
        conditions_sha256=conditions_sha256,
        resolved_at=datetime.now(UTC),
    )
    return ResolvedSweepBundle(resolved=resolved, receipt_path=receipt_path, project_root=actual_root)


def resolve_dom_light_spec(
    spec_path: Path,
    *,
    control_receipt_override: Path | None = None,
    root: Path | None = None,
) -> ResolvedDomSweepBundle:
    actual_root = (root or project_root()).resolve()
    spec = load_dom_light_spec(spec_path)
    receipt_path = (
        control_receipt_override.resolve()
        if control_receipt_override is not None
        else _declared_path(spec.control_receipt, root=actual_root)
    )
    receipt, control, all_task_revisions, receipt_bytes = _load_control_bundle(receipt_path)
    current_implementation = implementation_identity(actual_root)
    sweep_identity = SweepIdentity(
        control_spec_sha256=receipt.control_spec_sha256,
        implementation=current_implementation,
        classes=receipt.classes,
        models=receipt.models,
        tasks=receipt.tasks,
        policy=receipt.policy,
    )
    try:
        assert_control_admitted(receipt_path, sweep_identity)
    except Exception as exc:
        raise SweepConfigurationError(f"level-0 control does not admit this DOM sweep: {exc}") from exc

    model_by_runner_id = {model.runner_model: model for model in control.models}
    try:
        models = [model_by_runner_id[runner_model] for runner_model in receipt.models]
    except KeyError as exc:
        raise SweepConfigurationError(f"receipt model is absent from resolved control spec: {exc}") from exc
    primary = next((model for model in models if model.runner_model == spec.primary_model), None)
    if primary is None:
        raise SweepConfigurationError("primary_model must be one exact control-receipt model identifier")
    additional = [model for model in models if model.id != primary.id]

    active = {(item.id, item.revision) for item in receipt.tasks}
    task_by_identity = {(task.revision.task_id, task.revision.revision): task for task in all_task_revisions}
    missing = sorted(active - set(task_by_identity))
    if missing:
        raise SweepConfigurationError(f"active control task revisions are absent from resolved evidence: {missing}")
    tasks = [task_by_identity[(item.id, item.revision)] for item in receipt.tasks]
    condition_payload = {
        "conditions": [item.model_dump(mode="json") for item in spec.conditions],
        "axes": [item.model_dump(mode="json") for item in spec.axes],
    }
    conditions_sha256 = hashlib.sha256(canonical_json_bytes(condition_payload)).hexdigest()
    resolved = ResolvedDomLightSweep(
        spec=spec,
        control_receipt=receipt,
        control_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        control_spec_sha256=receipt.control_spec_sha256,
        implementation=current_implementation,
        primary_model=primary,
        additional_models=additional,
        tasks=tasks,
        conditions_sha256=conditions_sha256,
        resolved_at=datetime.now(UTC),
    )
    return ResolvedDomSweepBundle(resolved=resolved, receipt_path=receipt_path, project_root=actual_root)
