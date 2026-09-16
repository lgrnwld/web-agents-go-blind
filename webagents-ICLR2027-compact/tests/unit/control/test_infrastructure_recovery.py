from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from webagents.control.receipt import implementation_identity
from webagents.control.schemas import TrialResult


def _recovery_module() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts/retry_control_infrastructure.py"
    spec = importlib.util.spec_from_file_location("retry_control_infrastructure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(*, status: str, classification: str) -> TrialResult:
    now = datetime.now(UTC)
    return TrialResult.model_validate(
        {
            "logical_trial_id": "logical-1",
            "class_id": "cdp_frame_traversal",
            "task_id": "copy-known-value",
            "task_revision": 5,
            "model_id": "foundry-kimi-k2-6",
            "repeat": 2,
            "infrastructure_attempt": 0,
            "classification": classification,
            "runner_status": status,
            "started_at": now,
            "finished_at": now,
        }
    )


def test_wall_timeout_is_normalized_to_infrastructure_without_changing_trial_identity() -> None:
    normalize = _recovery_module()._as_infrastructure_timeout
    source = _result(status="timeout", classification="TASK_FAILURE")
    corrected = normalize(source)

    assert corrected.logical_trial_id == source.logical_trial_id
    assert corrected.infrastructure_attempt == source.infrastructure_attempt
    assert corrected.classification == "INFRASTRUCTURE_FAILURE"
    assert "frozen infrastructure retry policy" in (corrected.checker_detail or "")


def test_non_timeout_result_is_not_reclassified() -> None:
    normalize = _recovery_module()._as_infrastructure_timeout
    source = _result(status="failure", classification="TASK_FAILURE")
    assert normalize(source) is source


def test_repository_scripts_do_not_change_hashed_runner_identity(tmp_path: Path) -> None:
    (tmp_path / "src/webagents").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (tmp_path / "uv.lock").write_text("fixture-lock\n")
    (tmp_path / "src/webagents/runner.py").write_text("IDENTITY = 1\n")
    before = implementation_identity(tmp_path)

    (tmp_path / "scripts/recovery.py").write_text("RECOVERY = 1\n")
    after = implementation_identity(tmp_path)

    assert after == before
