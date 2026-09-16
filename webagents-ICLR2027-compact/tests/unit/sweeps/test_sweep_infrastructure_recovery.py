from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from webagents.sweeps.schemas import SweepMatrixRow, SweepTrialResult


def _recovery_module() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts/retry_sweep_infrastructure.py"
    spec = importlib.util.spec_from_file_location("retry_sweep_infrastructure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(logical_trial_id: str) -> SweepMatrixRow:
    return SweepMatrixRow(
        logical_trial_id=logical_trial_id,
        task_id="copy-known-value",
        task_revision=5,
        task_definition_sha256="a" * 64,
        model_id="foundry-kimi-k2-6",
        runner_model="foundry/Kimi-K2.6",
        condition_id="iframe-cross-depth-3",
        repeat=3,
        schedule_index=0,
    )


def _failure(logical_trial_id: str, classification: str, attempt: int) -> SweepTrialResult:
    now = datetime.now(UTC)
    return SweepTrialResult.model_validate(
        {
            "logical_trial_id": logical_trial_id,
            "task_id": "copy-known-value",
            "task_revision": 5,
            "model_id": "foundry-kimi-k2-6",
            "condition_id": "iframe-cross-depth-3",
            "repeat": 3,
            "infrastructure_attempt": attempt,
            "classification": classification,
            "checker_detail": "RateLimitReached",
            "started_at": now,
            "finished_at": now,
        }
    )


def test_only_final_infrastructure_failures_are_selected() -> None:
    module = _recovery_module()
    row = _row("a" * 24)
    results = [
        _failure(row.logical_trial_id, "INFRASTRUCTURE_FAILURE", 0),
        _failure(row.logical_trial_id, "INFRASTRUCTURE_FAILURE", 2),
    ]

    final, missing = module._final_results_and_missing([row], results)

    assert final[row.logical_trial_id].infrastructure_attempt == 2
    assert missing == [row]


def test_non_infrastructure_gap_is_rejected() -> None:
    module = _recovery_module()
    row = _row("b" * 24)

    with pytest.raises(module.RecoveryError, match="not retryable infrastructure"):
        module._final_results_and_missing(
            [row],
            [_failure(row.logical_trial_id, "EVIDENCE_FAILURE", 0)],
        )


def test_matrix_rows_without_any_attempt_are_rejected() -> None:
    module = _recovery_module()
    row = _row("c" * 24)

    with pytest.raises(module.RecoveryError, match="omit scheduled logical trials"):
        module._final_results_and_missing([row], [])
