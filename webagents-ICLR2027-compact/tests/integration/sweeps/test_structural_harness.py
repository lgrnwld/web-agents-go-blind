from __future__ import annotations

import os
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from webagents.control.config import load_all_task_revisions, load_control_spec
from webagents.sweeps.config import load_sweep_spec
from webagents.sweeps.harness import StructuralFixtureHarness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("WEBAGENT_RUN_SWEEP_INTEGRATION") != "1",
        reason="set WEBAGENT_RUN_SWEEP_INTEGRATION=1 to bind structural fixture servers",
    ),
]

ROOT = Path(__file__).resolve().parents[3]


def test_structural_harness_uses_distinct_origins_and_matched_semantics() -> None:
    spec = load_sweep_spec(ROOT / "benchmarks/sweeps/accessibility-tree-full.yaml")
    control = load_control_spec(ROOT / "benchmarks/control/control.yaml")
    tasks = load_all_task_revisions(ROOT / "benchmarks/control/control.yaml", control)
    active = [tasks[(item.id, item.active_revision)] for item in control.tasks]
    with StructuralFixtureHarness(spec.harness, active, spec.conditions) as harness:
        harness.preflight()
        assert (
            urllib.parse.urlsplit(harness.primary_base_url).netloc
            != urllib.parse.urlsplit(harness.cross_origin_base_url).netloc
        )
        task = active[0]
        cross = next(item for item in spec.conditions if item.id == "iframe-cross-depth-3")
        root = urllib.request.urlopen(harness.condition_url(task, cross), timeout=2).read().decode()
        assert harness.cross_origin_base_url in root
        assert task.revision.level0.reachability_marker not in root
        assert len({harness.semantic_payload_sha256(task) for _ in spec.conditions}) == 1


def test_shadow_and_canvas_sources_preserve_marker_without_an_accessible_canvas_duplicate() -> None:
    spec = load_sweep_spec(ROOT / "benchmarks/sweeps/accessibility-tree-full.yaml")
    control = load_control_spec(ROOT / "benchmarks/control/control.yaml")
    tasks = load_all_task_revisions(ROOT / "benchmarks/control/control.yaml", control)
    copy_roster = next(item for item in control.tasks if item.id == "copy-known-value")
    task = tasks[(copy_roster.id, copy_roster.active_revision)]
    with StructuralFixtureHarness(spec.harness, [task], spec.conditions) as harness:
        harness.preflight()
        closed = next(item for item in spec.conditions if item.id == "shadow-closed")
        canvas = next(item for item in spec.conditions if item.id == "rendered-canvas")
        closed_html = urllib.request.urlopen(harness.condition_url(task, closed), timeout=2).read().decode()
        canvas_html = urllib.request.urlopen(harness.condition_url(task, canvas), timeout=2).read().decode()
        marker = task.revision.level0.reachability_marker
        assert 'attachShadow({mode:"closed"})' in closed_html
        assert marker in closed_html
        assert marker not in canvas_html
        canvas_data_url = f"{harness.condition_url(task, canvas).rstrip('/')}/canvas-data"
        canvas_data = urllib.request.urlopen(canvas_data_url, timeout=2).read().decode()
        assert marker in canvas_data
        assert f'aria-label="{marker}"' not in canvas_html
        assert "fillText" in canvas_html
        assert "data-fixture-presentation" in closed_html
        assert "large-target-v1" in closed_html
        assert 'data-fixture-presentation="large-target-v1"' in canvas_html

        submit_url = f"{harness.condition_url(task, canvas).rstrip('/')}/submit"
        payload = urllib.parse.urlencode({"destination": ""}).encode()
        confirmation = urllib.request.urlopen(
            urllib.request.Request(submit_url, data=payload, method="POST"), timeout=2
        ).read().decode()
        assert "Saved" in confirmation
        assert marker not in confirmation
        assert task.revision.level0.fixture.source_value not in confirmation
