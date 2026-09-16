# Web-agent observation boundaries — ICLR 2027 reproducibility artifact

This anonymous research snapshot accompanies the ICLR 2027 manuscript in `paper/`. Extract this ZIP into a repository and give the repository to your anonymization service. The ZIP has repository files at its root. No upload or publication has been performed. Update the manuscript's anonymous repository URL after the new repository is available.

## Reproduce the reported analysis without model calls

Requirements: Python 3.12, approximately 2 GB free disk space including generated outputs. Installing dependencies requires internet access; analysis then uses only the archived local observations. No API keys or browser installation are required for this path.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-reproduce.txt
.venv/bin/python verify_package.py
.venv/bin/python reproduce.py
```

On Windows, use `.venv\Scripts\python.exe`. Run commands from this directory. The driver writes `reproduced/`, regenerates statistical CSV tables and publication figures, and compares the numerical tables with the archived reference analysis. It fails on a mismatch. Move the generated directory aside before rerunning. `VALIDATION.json` records the export test; `reproduced/VERIFICATION.json` records your own run. Portable bundled fonts can change figure typography, and PDF timestamps can differ; statistical values are checked, not PDF byte identity.

## Included evidence

| Study | Valid trials | Location |
|---|---:|---|
| Primary four-architecture grid | 1,760 | `artifacts/reconciled/` and `artifacts/sweeps-r5/` |
| Original separation follow-up | 240 | `artifacts/followups/canvas-delimitation-v8/` |
| Original vision model extension | 440 | `artifacts/followups/vision-model-extension-v1/` |
| New separation extension | 240 | `artifacts/revision-20260915/separation-extension-final/` |
| Table/card/form generalization | 1,440 | `artifacts/revision-20260915/generalization-v2/` |
| Explicit-format intervention | 160 | `artifacts/revision-20260915/intervention-v2/` |
| Calibrated randomized iframe rerun | 560 | `artifacts/revision-20260915/iframe-v2/` |
| Visible natural-boundary controls | 240 | `artifacts/revision-20260915/boundary-cues/` |
| Primary control gate | 48 | `artifacts/control/` |

There are 5,080 study outcomes plus 48 control outcomes. Raw run archives also include retry attempts and inherited checkpoints; these are not additional independent observations. `PACKAGE-INVENTORY.json` gives the archive count. Every allocated revision cell has ten valid repeats.

- `src/`, `benchmarks/`, `scripts/`, `tests/`: runner implementation, fixtures, configurations, analysis, and tests.
- `artifacts/runs/` and `artifacts/revision-20260915/runs/runs/`: per-run observations, screenshots, model responses, transcripts, and checker evidence.
- `analysis/`: archived reference tables, coding audit, reports, completion receipt, and image-pair audits.
- `historical/core-grid-source/`: source/configuration frozen for the primary run. The current source includes later calibration changes and should not be substituted for the historical source when interpreting old runs.
- `paper/`: manuscript source, figures, compiled PDF, and the original anonymous reference PDF.
- `ANONYMIZATION.json`: changed-file digests and the scope of release-only sanitization. Outcome values and screenshots are preserved. Hash references were rebound after identifying paths and provider resource names were removed; original digests remain available in this audit record.
- `SHA256SUMS`: integrity checks for the distributed files.

## Collect new experimental observations (optional; incurs API charges)

Use Python 3.12 and the pinned `uv.lock` for runner dependencies:

```sh
uv sync --frozen --extra dev
uv run playwright install chromium
```

Configure your own hosted deployments using `.env.example` and `src/webagents/providers/config.py`. Supply environment variables to the process; the real `.env` and credentials are deliberately absent. The recorded model names are deployment identifiers, not guarantees that the same snapshots are available in another account. Fresh stochastic outcomes, provider latency, and infrastructure retries may differ from the archived run.

The four new layout/calibration arms are available through `scripts.run_revision_experiments`. Example, preserving the full allocation:

```sh
uv run python -m scripts.run_revision_experiments \
  --study generalization \
  --models foundry/gpt-5.6-terra foundry/Kimi-K2.6 foundry/gpt-5.4 foundry/gpt-5-mini \
  --repeats 10 --seed 20260915 --concurrency 4 \
  --output-dir fresh/generalization --archive-root fresh/archives \
  --paper paper/reference-original.pdf
```

Use `--study intervention`, `--study iframe`, or `--study boundary-cues` with separate output directories for the other arms. `--preflight-only` renders and checks fixtures without provider calls; use a separate preflight output directory. The separation extension uses `scripts.run_canvas_delimitation_followup`; consult `--help` and its distributed resolved specification for the recorded model roster, seed, eight-step budget, and 90-second timeout. Historical specifications and receipts preserve the original model/task allocations and run windows.

## Interpretation and limitations

The hidden DOM-edge contrast is a vision negative control: screenshots are held identical. Visible natural-boundary controls jointly change labels, layout, and spacing, so they do not isolate a causal DOM-distance effect. Delimitation of the last typed candidate is reported separately from actual task submission. The iframe rerun tests depth after coordinate calibration and position randomization; absence of a consistent gradient does not prove equivalence. Historical and calibrated iframe field sizes differ. See the manuscript and `analysis/revision-20260915/report.md` for details.

## Manuscript and anonymous release

`paper/main.tex` uses the official unmodified ICLR 2027 anonymous style. Compile with `latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex` from `paper/`. A separate compact Overleaf ZIP is supplied for editing. Read `TEXT-CHANGES.md` for the comparison with the original supplied paper.

The export excludes `.git`, private planning notes, real environment files, caches, and local build logs. It removes local account paths and concrete provider resource identifiers and checks for configured credential values. Included third-party template files retain their copyright notices. Review any future additions before sharing; anonymizing a repository does not automatically update the paper's link. No new license grant is asserted by this packaging step.
