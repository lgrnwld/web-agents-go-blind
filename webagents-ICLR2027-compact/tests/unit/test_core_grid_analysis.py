from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _analysis_module() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts/analyze_core_grid.py"
    spec = importlib.util.spec_from_file_location("analyze_core_grid", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wilson_interval_for_ten_of_ten() -> None:
    module = _analysis_module()
    rate, lower, upper = module.wilson(10, 10)

    assert rate == 1.0
    assert lower == pytest.approx(0.7224672001)
    assert upper == pytest.approx(1.0)


def test_cliff_classifier_distinguishes_cliff_slope_and_flat() -> None:
    module = _analysis_module()
    assert module.curve_statistics([1.0, 1.0, 0.1, 0.0])["cliff_classifier"] == "cliff"
    assert module.curve_statistics([1.0, 0.7, 0.4, 0.1])["cliff_classifier"] == "slope"
    assert module.curve_statistics([0.9, 0.9, 0.85, 0.8])["cliff_classifier"] == "no_material_net_drop"


def test_two_way_partition_recovers_architecture_only_signal() -> None:
    module = _analysis_module()
    architectures = (
        "dom_extraction",
        "accessibility_tree",
        "vision",
        "cdp_frame_traversal",
    )
    models = ("model-a", "model-b")
    values = {
        ("component", architecture, model): float(index)
        for index, architecture in enumerate(architectures)
        for model in models
    }

    shares = module.two_way_shares(values, ["component"], models)

    assert shares["architecture"] == pytest.approx(1.0)
    assert shares["model"] == pytest.approx(0.0)
    assert shares["interaction"] == pytest.approx(0.0)
