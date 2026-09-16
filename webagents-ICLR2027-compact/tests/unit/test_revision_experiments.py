from types import SimpleNamespace

from scripts.run_revision_experiments import content_score, matrix


def test_positions_are_paired_and_depth_zero_not_duplicated():
    rows = matrix("iframe", ["foundry/gpt-5.4", "foundry/gpt-5-mini"], 10, 20260915)
    assert len(rows) == 280
    assert len({row["id"] for row in rows}) == 280
    assert len([r for r in rows if r["depth"] == 0]) == 40
    for task in ("read-known-value", "copy-known-value"):
        for repeat in range(1, 11):
            positions = {
                tuple(sorted(r["position"].items())) for r in rows if r["task_id"] == task and r["repeat"] == repeat
            }
            assert len(positions) == 1
    assert rows == matrix("iframe", ["foundry/gpt-5.4", "foundry/gpt-5-mini"], 10, 20260915)


def test_generalization_crosses_distance_and_dom_boundaries():
    rows = matrix("generalization", ["foundry/gpt-5.4"], 10, 1)
    assert len(rows) == 360
    assert {(r["layout"], r["gap"], r["boundary"]) for r in rows} == {
        (layout, gap, boundary)
        for layout in ("table", "cards", "form")
        for gap in (8, 40, 96)
        for boundary in ("shared", "separate")
    }


def transcript(text):
    return SimpleNamespace(
        steps=[SimpleNamespace(actions=[SimpleNamespace(name="type", parameters={"text": text})])],
        final=SimpleNamespace(answer=""),
    )


def test_correct_content_with_failed_submission_is_not_delimitation():
    result = content_score(transcript("BRAVO-4826"), {"task_id": "copy-known-value"}, None)
    assert result["content_exact"]
    assert not result["exact_match"]
    assert not result["delimitation_error"]


def test_marker_contamination_is_scored_from_attempted_content():
    result = content_score(transcript("BRAVO-4826 | CONTROL_COPY_VALUE_R5"), {"task_id": "copy-known-value"}, None)
    assert result["delimitation_error"]
    assert not result["content_exact"]
    assert not result["exact_match"]


def test_paired_effect_preserves_pairing_and_rejects_missing_arms():
    import pytest

    from scripts.analyze_revision_experiments import paired_effect

    rows = [
        {"repeat": 1, "arm": "baseline", "exact_match": False},
        {"repeat": 1, "arm": "format", "exact_match": True},
        {"repeat": 2, "arm": "baseline", "exact_match": True},
        {"repeat": 2, "arm": "format", "exact_match": True},
    ]
    result = paired_effect(rows, "arm", "baseline", "format", ("repeat",))
    assert result["difference"] == 0.5
    assert result["improved"] == 1 and result["worsened"] == 0
    assert result["pairs"] == 2
    with pytest.raises(AssertionError):
        paired_effect(rows[:-1], "arm", "baseline", "format", ("repeat",))
