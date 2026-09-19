# Compact reproducibility artifact — ICLR 2027

This directory contains the complete anonymous reproducibility artifact in a lossless,
deduplicated representation. All original outcomes, failed/retried runs, screenshots,
model requests, responses, and provenance records are retained. Identical screenshots
are stored once, and repeated base64 image payloads in model requests reference those
same image bytes. No screenshots were resized or converted to a lossy format.

## Reproduce the paper

Run these commands from this directory.
Use Python 3.12. Installing dependencies needs internet access; restoration and
statistical reanalysis use local files and require no API keys or provider calls.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-reproduce.txt
.venv/bin/python restore_archive.py --output restored
.venv/bin/python restored/reproduce.py
```

On Windows, replace `.venv/bin/python` with `.venv\Scripts\python.exe`.
Restoration checks the compact package, reconstructs every original archive path,
and verifies each file against the original SHA-256 manifest. It refuses to overwrite
an existing output directory. The restored artifact is approximately 1.16 GB; allow
at least 3 GB of disk space for the full workflow. Compression reduces download and
repository size; full reconstruction still requires the original storage space.

The analysis regenerates the statistical tables and six numerical/fixture figure
PDFs, checks 29 CSV tables against the reference analysis, and audits matched-image
groups. It additionally checks all 880 new raw archives, initial screenshots, transcript-derived candidates, content scores, and the complete instance-study summary. Results appear in `restored/reproduced/`. The conceptual Figure 1 is supplied
as an existing image. The existing `VALIDATION.json` records validation of the full
release; `COMPACT-VALIDATION.json` records validation of this compact distribution.

## Included revision

The active paper contains nine main-text pages and 20 pages total. It preserves the original illustrated pipeline and adds the completed instance-variation study to the narrative about missing evidence and answer boundaries.

There are **5,960 study outcomes plus 48 control outcomes**. The new 880 trials comprise 20 task-instance blocks, paired across four deployments and eleven conditions. Targets, markers, positions, and typography vary; the token format remains fixed. No generic-instruction control was added.

- New results and frozen allocation: `artifacts/instance-variation-20260919-final/`.
- Protocol, tables, paired intervals, and interpretation: `analysis/instance-variation-20260919/`.
- New raw trial archives: losslessly encoded in `raw-audit/`; restoration reconstructs `artifacts/instance-variation-20260919-final/runs/runs/`.
- Current paper: `paper/manuscript.pdf` and `paper/main.tex`.
- Latest authoritative baseline: `paper/reference/coolwebagentsiclr.pdf`; the older `paper/reference-original.pdf` remains for historical run provenance.

Structural marker-text reachability is distinct from **target region visible**. The latter does not establish legibility; the new audit checks geometry and recovered content separately. Historical schema names remain intact. Labeled components had 0/240 errors originally and 1/240 under the new broad substring/superset score (a truncated target, no marker included). Explicit-format prompting supplies boundary guidance and narrows the answer space simultaneously. Results establish robustness across these sampled synthetic instances, not real-site generalization.

The reproduction workflow makes **zero model calls**. Fresh inference requires the full runner dependencies and your own deployments; see `raw-audit/original-readme.md` for the exact command. Preserve archived trial directories.

## Layout

- `src/`, `benchmarks/`, `scripts/`, `tests/`: readable code, configurations, and tests.
- `artifacts/`: readable per-trial result files and study specifications/receipts.
- `analysis/`: numerical tables, coding audits, reports, and completion receipts.
- `paper/`: manuscript source, figures, compiled manuscript, and original reference PDF.
- `historical/`: the source/configuration frozen for the primary study.
- `raw-audit/index.jsonl`: original raw-file paths, byte sizes, hashes, and record/blob references.
- `raw-audit/records/*.jsonl`: deduplicated requests, transcripts, events, and other exact text records.
- `raw-audit/blobs/sha256/`: one copy of each distinct binary payload, addressed by its SHA-256.
- `raw-audit/ORIGINAL-SHA256SUMS`: expected digests of all files in the full artifact.
- `restore_archive.py`: dependency-free reconstruction and integrity checking.
- `SHA256SUMS`: checksums of the compact distribution, also checked by `verify_package.py`.
- `COMPACT-FORMAT.md`: exact encoding and reconstruction details.
- `COMPACT-INVENTORY.json`: file counts and measured representation sizes.

The main code/data remain readable without restoration. To use the existing complete
analysis pipeline or archive verifiers, restore first; those scripts intentionally
expect the original file layout. The packed representation is not itself a replacement
for every original archive path.

## Hosting and new experiments

Upload the extracted compact tree to your anonymous repository. Do not add the expanded
`restored/` directory to that repository. The paper's anonymous repository URL must be
updated once the new artifact is hosted. No upload has been performed by packaging.

For new provider-backed experiments, see `raw-audit/original-readme.md` or the restored
README. Model names are recorded deployment identifiers; repeating hosted inference
requires your own credentials and deployment access and can incur charges. New model
outputs need not match the historical outputs. The supplied archived outcomes support
deterministic reanalysis independently of continued deployment availability.

The full release had already been anonymized. `ANONYMIZATION.json` documents that prior
step. This compact encoding preserves those anonymized bytes exactly, including all
original hashes. The added study has its own `REVISION-ANONYMIZATION.json`; the original anonymization record remains unchanged. See `TEXT-CHANGES.md` for manuscript changes. This archive does not
assert a new license grant.
