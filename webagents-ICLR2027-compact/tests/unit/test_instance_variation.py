from types import SimpleNamespace

from scripts.run_instance_variation import content_score, matrix, render


def test_fresh_instances_are_paired_without_answer_leakage():
    rows = matrix(["model-a", "model-b"], 10, 20260919)
    assert len(rows) == 440
    assert len({r["instance_id"] for r in rows}) == 20
    assert len({r["value"] for r in rows}) == 20
    assert len({r["marker"] for r in rows}) == 20
    for task in {r["task_id"] for r in rows}:
        for repeat in range(1, 11):
            pair = [r for r in rows if r["task_id"] == task and r["repeat"] == repeat]
            assert (
                len(
                    {
                        (
                            r["value"],
                            r["marker"],
                            r["font_size"],
                            r["font_family"],
                            r["position"]["x"],
                            r["position"]["y"],
                        )
                        for r in pair
                    }
                )
                == 1
            )
    assert rows == matrix(["model-a", "model-b"], 10, 20260919)
    for row in rows:
        page = render(row, "/case/opaque/0")
        assert row["value"] in page and row["marker"] in page
        assert "ALPHA-7391" not in page and "BRAVO-4826" not in page


def transcript(answer="", typed=None):
    return SimpleNamespace(
        final=SimpleNamespace(answer=answer),
        steps=[]
        if typed is None
        else [SimpleNamespace(actions=[SimpleNamespace(name="type", parameters={"text": typed})])],
    )


def test_instance_specific_read_and_copy_scoring():
    row = {"task_id": "read-known-value", "value": "ABCDE-1234", "marker": "REF_EXAMPLE"}
    assert content_score(transcript(answer=row["value"]), row, None)["exact_match"]
    bad = content_score(transcript(answer=row["value"] + " | " + row["marker"]), row, None)
    assert bad["delimitation_error"] and bad["target_token_in_candidate"] and not bad["exact_match"]
    row["task_id"] = "copy-known-value"
    failed_submit = content_score(transcript(typed=row["value"]), row, None)
    assert failed_submit["content_exact"] and not failed_submit["exact_match"]
    assert not failed_submit["delimitation_error"]
    assert content_score(transcript(typed=row["value"]), row, row["value"])["exact_match"]
    no_answer = content_score(transcript(), row, None)
    assert no_answer["no_answer"] and not no_answer["delimitation_error"]


def test_analysis_rejects_missing_repeated_and_infrastructure_outcomes(tmp_path):
    import hashlib
    import json

    import pytest

    from scripts.analyze_instance_variation import load_complete

    source = b"frozen runner"
    row = {"id": "a", "layout": "table", "boundary": "shared", "gap": 8}
    (tmp_path / "runner-source.py").write_bytes(source)
    (tmp_path / "resolved-spec.json").write_text(
        json.dumps(
            {
                "planned_trials": 1,
                "source_sha256": hashlib.sha256(source).hexdigest(),
            }
        )
    )
    (tmp_path / "matrix.json").write_text(json.dumps([row]))
    results = tmp_path / "results.jsonl"
    results.write_text("")
    with pytest.raises(AssertionError, match="Incomplete allocation"):
        load_complete(tmp_path)
    result = {**row, "valid": False, "geometry": {"target_region_visible": True}}
    results.write_text(json.dumps(result) + "\n")
    with pytest.raises(AssertionError, match="Infrastructure failures remain"):
        load_complete(tmp_path)
    result["valid"] = True
    results.write_text(json.dumps(result) + "\n")
    _, rows, _ = load_complete(tmp_path)
    assert rows[0]["arm"] == "gap-8"
    results.write_text((json.dumps(result) + "\n") * 2)
    with pytest.raises(AssertionError, match="valid trial was rerun"):
        load_complete(tmp_path)
