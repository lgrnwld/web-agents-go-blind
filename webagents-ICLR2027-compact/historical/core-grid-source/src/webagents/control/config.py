"""Load and resolve versioned control and task specifications."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from webagents.capture.archive import canonical_json_bytes
from webagents.control.schemas import ControlSpec, ResolvedTask, TaskRevisionSpec, TaskRosterEntry


class ControlConfigurationError(ValueError):
    """A control specification is missing, unsafe, or internally inconsistent."""


class ControlSpecError(ControlConfigurationError):
    """A versioned YAML control or task specification is invalid."""


def _yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ControlSpecError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlSpecError(f"{path} must contain one YAML mapping")
    return value


def load_control_spec(path: Path) -> ControlSpec:
    path = path.resolve()
    try:
        return ControlSpec.model_validate(_yaml_mapping(path))
    except Exception as exc:
        raise ControlSpecError(f"invalid control spec {path}: {exc}") from exc


def task_revision_path(spec_path: Path, task_id: str, revision: int) -> Path:
    benchmarks_root = spec_path.resolve().parent.parent
    path = benchmarks_root / "tasks" / task_id / f"r{revision}.yaml"
    if not path.resolve().is_relative_to(benchmarks_root):
        raise ControlSpecError("task definition escaped benchmarks root")
    return path


def load_task_revision(spec_path: Path, roster: TaskRosterEntry, revision: int) -> ResolvedTask:
    path = task_revision_path(spec_path, roster.id, revision)
    try:
        task = TaskRevisionSpec.model_validate(_yaml_mapping(path))
    except Exception as exc:
        raise ControlSpecError(f"invalid task definition {path}: {exc}") from exc
    if task.task_id != roster.id or task.revision != revision:
        raise ControlSpecError(f"task identity in {path} does not match roster entry {roster.id} r{revision}")
    canonical = canonical_json_bytes(task.model_dump(mode="json"))
    return ResolvedTask(
        roster=roster,
        revision=task,
        definition_path=str(path),
        definition_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def load_all_task_revisions(spec_path: Path, spec: ControlSpec) -> dict[tuple[str, int], ResolvedTask]:
    resolved: dict[tuple[str, int], ResolvedTask] = {}
    for roster in spec.tasks:
        for revision in (roster.active_revision, *roster.simplification_ladder):
            task = load_task_revision(spec_path, roster, revision)
            resolved[(roster.id, revision)] = task
    paths: list[str] = []
    for task in resolved.values():
        level0 = task.revision.level0
        paths.extend([level0.page_path, level0.reset_path])
        if level0.fixture.kind in {"copy_value_form", "copy_value_form_large_target"}:
            paths.append(f"{level0.page_path.rstrip('/')}/submit")
        if level0.checker.endpoint_path is not None:
            paths.append(level0.checker.endpoint_path)
    if len(paths) != len(set(paths)):
        raise ControlSpecError("task revisions must use unique page, reset, and checker paths")
    return resolved
