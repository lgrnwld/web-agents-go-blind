#!/usr/bin/env python3
"""Recompute paper statistics from archived observations; no provider calls."""
from __future__ import annotations
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'reproduced'


def call(module, *args):
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + str(ROOT)
    for key in list(environment):
        if any(word in key.upper() for word in ('API_KEY', 'AZURE_OPENAI', 'FOUNDRY_', 'OPENROUTER_')):
            environment.pop(key)
    command = [sys.executable, '-m', module, *map(str, args)]
    print('RUN', ' '.join(command[1:]), flush=True)
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True)
    name = module.rsplit('.', 1)[-1]
    (OUT / f'{name}.log').write_text(result.stdout + result.stderr)
    if result.returncode:
        print(result.stdout[-3000:] + result.stderr[-3000:])
        raise SystemExit(f'{module} failed; see reproduced/{name}.log')


def main():
    if OUT.exists():
        raise SystemExit('Move or remove the prior reproduced/ directory before a new run; archived inputs are never overwritten.')
    OUT.mkdir()
    call('scripts.analyze_core_grid', '--output-dir', 'reproduced/core-grid-v1')
    call('scripts.analyze_reviewer_followups', '--output', 'reproduced/reviewer-response-v1')
    call('scripts.analyze_canvas_delimitation_followup',
         'artifacts/followups/canvas-delimitation-v8/results.jsonl',
         '--output-dir', 'reproduced/canvas-delimitation-followup')
    call('scripts.analyze_vision_model_extension',
         '--extension-results', 'artifacts/followups/vision-model-extension-v1/results.jsonl',
         '--output-dir', 'reproduced/vision-model-extension')
    call('scripts.score_blind_coding_agreement', '--output', 'reproduced/blind-coding-agreement.json')
    call('scripts.analyze_revision_experiments', '--output', 'reproduced/revision-20260915',
         '--figures', 'reproduced/figures')
    call('scripts.analyze_instance_variation', '--output', 'reproduced/instance-variation-20260919')
    call('scripts.audit_instance_variation')
    # Build the remaining numerical publication figures from newly computed tables.
    sys.path.insert(0, str(ROOT))
    from scripts import build_publication_figures as figures
    figures.ANALYSIS = OUT / 'core-grid-v1'
    figures.VISION_EXTENSION = OUT / 'vision-model-extension/combined-cells.csv'
    figures.OUTPUT = OUT / 'figures'
    figures.main()
    # Supplementary fixture illustration is a deterministic crop of archived PNGs.
    call('scripts.build_revision_fixture_figure')
    pairs = [
        ('analysis/instance-variation-20260919', 'reproduced/instance-variation-20260919'),
        ('analysis/core-grid-v1', 'reproduced/core-grid-v1'),
        ('analysis/revision-20260915', 'reproduced/revision-20260915'),
        ('analysis/reviewer-response-v1/canvas-delimitation-followup', 'reproduced/canvas-delimitation-followup'),
        ('analysis/reviewer-response-v1/vision-model-extension', 'reproduced/vision-model-extension'),
    ]
    checks = []
    for olddir, newdir in pairs:
        for new in sorted((ROOT / newdir).glob('*.csv')):
            expected = ROOT / olddir / new.name
            if not expected.exists():
                continue
            def rows(path):
                with path.open(newline='') as f:
                    return list(csv.DictReader(f))
            assert rows(new) == rows(expected), f'Statistical table differs: {new.name}'
            checks.append(str(new.relative_to(ROOT)))
    assert len(checks) == 29, checks
    summary = json.loads((OUT / 'revision-20260915/summary.json').read_text())
    assert sum(s['trials'] for s in summary['sources'].values()) == 2640
    audit = json.loads((OUT / 'revision-20260915/image-pair-audit.json').read_text())
    assert all(a['different_image_groups'] == 0 for a in audit)
    agreement = json.loads((OUT / 'blind-coding-agreement.json').read_text())
    assert agreement['agreements'] == agreement['trials'] == 40
    instance = json.loads((OUT / 'instance-variation-20260919/summary.json').read_text())
    expected_instance = json.loads((ROOT / 'analysis/instance-variation-20260919/summary.json').read_text())
    assert instance == expected_instance, 'Instance summary differs from the frozen reference'
    instance_audit = json.loads((OUT / 'instance-variation-20260919/archive-audit.json').read_text())
    assert instance['valid'] == 880 and instance['unique_instances'] == 20
    assert instance_audit['status'] == 'passed'
    verification = {'status': 'passed', 'matching_csv_tables': len(checks),
                    'tables': checks, 'revision_valid_trials': 2640, 'primary_valid_trials': 1760,
                    'paired_image_audits': audit, 'instance_variation_valid_trials': 880,
                    'total_study_outcomes': 5960, 'control_outcomes': 48,
                    'instance_archive_audit': instance_audit, 'provider_calls': 0}
    (OUT / 'VERIFICATION.json').write_text(json.dumps(verification, indent=2) + '\n')
    print(json.dumps(verification, indent=2))


if __name__ == '__main__':
    main()
