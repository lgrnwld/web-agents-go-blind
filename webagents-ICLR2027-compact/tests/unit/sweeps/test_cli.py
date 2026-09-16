from __future__ import annotations

import os
from pathlib import Path

import pytest

from webagents.sweeps.cli import _latest_control_receipt
from webagents.sweeps.config import SweepConfigurationError


def test_latest_control_receipt_selects_newest_passing_receipt(tmp_path: Path) -> None:
    older = tmp_path / "control-old" / "control-receipt.json"
    newer = tmp_path / "control-new" / "control-receipt.json"
    older.parent.mkdir()
    newer.parent.mkdir()
    older.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    assert _latest_control_receipt(tmp_path) == newer


def test_latest_control_receipt_requires_a_pass(tmp_path: Path) -> None:
    with pytest.raises(SweepConfigurationError, match="no passing control receipt"):
        _latest_control_receipt(tmp_path)
