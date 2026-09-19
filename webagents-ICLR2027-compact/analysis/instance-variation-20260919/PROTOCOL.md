# Instance-variation protocol

This study implements the requested robustness experiment against the user-designated `coolwebagentsiclr.pdf`. No generic-instruction control was included in this revision.

The allocation and runner were frozen before the first model call on 2026-09-19. There are 880 trials: four deployments, two tasks, ten fresh instances per task, and eleven conditions. Conditions are table/card/form shells at 8px, 96px, or separate labeled components (nine), plus inline-canvas baseline and the existing explicit-format instruction (two).

Each task/repeat generates a new target, marker, position, and typography. The instance is paired across deployments and conditions. A target has five uppercase letters, a hyphen, and four digits; its marker has a `REF_` prefix and a random 12-character suffix. All instances are independent of the original ALPHA/BRAVO target values. The two canvas prompt arms use identical screenshots. No generic-instruction arm is included.

The primary content endpoint is delimitation error. Exact candidate content, exact task success, no-answer outcomes, target-token inclusion, and marker inclusion are retained separately. The pre-specified contrasts for this run are 96px versus 8px, labeled components versus 8px, and explicit-format versus baseline. Estimates are exploratory, with cell Wilson intervals and paired bootstrap intervals over complete task-stratified instance blocks. Layouts sharing an instance are resampled together. No multiple-comparison correction or unclustered significance claim is used.

Browser preflight verifies complete target/distractor boxes within the viewport, the intended measured gaps, and pixel-coordinate copy/submit behavior. It does not certify text legibility by OCR or a human. The archived vision metric remains a legacy runner field; this study's explicit `geometry.target_region_visible` is the verified geometry endpoint, and model token recovery is a separate content endpoint.

The study uses the existing version-pinned vision runner, 768x768 screenshots, eight actions, and 90 seconds per trial. All model failures and timeouts count. Only infrastructure failures may be retried without changing the allocation. Task success uses the recorded exact answer or submitted field value, independently of the runner's final status. A copy trial that already submitted the correct value remains a checked success if the runner later times out or fails to finish; terminal status is retained separately. Completed valid trials cannot be rerun or silently dropped. The analyzer refuses incomplete allocations.

The first preflight-only directory (`artifacts/instance-variation-20260919`) contains no model trials. A file-handling issue on resuming that preflight was corrected before model calls; the final frozen study is `artifacts/instance-variation-20260919-final`. The allocation did not change.

## Local reproduction

The frozen study includes `resolved-spec.json`, `matrix.json`, `runner-source.py`, dependency-source snapshots, `geometry.json`, fixture previews, `results.jsonl`, and raw request/response/action archives under `runs/`.

Analyze completed results without provider calls:

```sh
.venv/bin/python -m scripts.analyze_instance_variation \
  --output reproduced/instance-variation-20260919
```

A new paid replication must use a new output/archive directory. Retain the recorded seed for the same allocation or explicitly identify a new seed as a new instance sample. Credentials are loaded from the environment, never included in the paper or artifact package.
