"""Audit the 880 archived instance trials without browsers or provider calls."""
from pathlib import Path
import argparse
import hashlib
import json
from scripts.analyze_instance_variation import load_complete
from webagents.capture.archive import verify_run


def audit(root, output):
    spec, rows, _ = load_complete(root)
    geometry = json.loads((root / 'geometry.json').read_text())
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    matrix = json.loads((root / 'matrix.json').read_text())
    assert hashlib.sha256(canonical(matrix)).hexdigest() == spec['matrix_sha256']
    assert len(rows) == 880 and len({r['instance_id'] for r in rows}) == 20
    assert len({r['run_id'] for r in rows}) == 880
    for row in rows:
        archive = root / 'runs/runs' / row['run_id']
        diagnostics = verify_run(archive, require_complete=False)
        assert not diagnostics, (row['id'], [d.code for d in diagnostics])
        transcript = json.loads((archive / 'transcript.json').read_text())
        assert transcript['run_id'] == row['run_id']
        assert transcript['status'] == row['runner_status']
        image = archive / 'observations/step-000/observation.png'
        assert hashlib.sha256(image.read_bytes()).hexdigest() == row['geometry']['screenshot_sha256']
        assert row['geometry'] == geometry[row['id']]
        assert row['geometry']['target_region_visible']
        assert not row['geometry']['text_legibility_verified']
        for name in ('target_box', 'distractor_box'):
            box = row['geometry'][name]
            assert box['x'] >= 0 and box['y'] >= 0
            assert box['width'] > 0 and box['height'] > 0
            assert box['x'] + box['width'] <= spec['viewport']['width']
            assert box['y'] + box['height'] <= spec['viewport']['height']
        typed = [a['parameters'].get('text', '') for s in transcript['steps'] for a in s['actions'] if a['name'] == 'type']
        candidate = ((transcript.get('final') or {}).get('answer') or '') if row['task_id'] == 'read-known-value' else (typed[-1] if typed else '')
        assert candidate == row['candidate_value']
        c, value = candidate.strip(), row['value']
        category = 'correct_content' if c == value else 'delimitation' if c and (value in c or c in value) else 'no_answer' if not c else 'substitution_or_other'
        assert row['content_category'] == category
        expected = dict(delimitation_error=category == 'delimitation', content_exact=c == value,
                        target_token_in_candidate=value in c, marker_in_candidate=row['marker'] in c, no_answer=not c)
        emitted = row['emitted_value']
        if row['task_id'] == 'read-known-value': assert emitted == candidate
        expected.update(exact_match=emitted.strip() == value, substring_match=value in emitted,
                        target_without_marker=value in emitted and row['marker'].casefold() not in emitted.casefold())
        assert all(row[k] == v for k,v in expected.items()), row['id']
    report = dict(status='passed', archives_checked=len(rows), matching_initial_screenshots=len(rows),
                  candidates_and_scores_checked=len(rows), unique_instances=20, provider_calls=0,
                  submission_scope='Exact task scores are recomputed from the saved checker-emitted values; candidate strings are independently recovered from transcripts.',
                  visibility_scope='Geometry is checked separately from legibility; no OCR or human legibility verification is asserted.')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('artifacts/instance-variation-20260919-final'))
    parser.add_argument('--output', type=Path, default=Path('reproduced/instance-variation-20260919/archive-audit.json'))
    args = parser.parse_args()
    audit(args.root, args.output)
