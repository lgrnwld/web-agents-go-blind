# Compact reproducibility artifact — ICLR 2027

This ZIP contains the complete anonymous reproducibility artifact in a lossless,
deduplicated representation. All original outcomes, failed/retried runs, screenshots,
model requests, responses, and provenance records are retained. Identical screenshots
are stored once, and repeated base64 image payloads in model requests reference those
same image bytes. No screenshots were resized or converted to a lossy format.

## Reproduce the paper

Extract the ZIP into a new directory, then run these commands from that directory.
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
an existing output directory. The restored artifact is approximately 1.03 GB; allow
at least 2 GB of disk space for the full workflow. Compression reduces download and
repository size; full reconstruction still requires the original storage space.

The analysis regenerates the statistical tables and six numerical/fixture figure
PDFs, checks 26 CSV tables against the reference analysis, and audits matched-image
groups. Results appear in `restored/reproduced/`. The conceptual Figure 1 is supplied
as an existing image. The existing `VALIDATION.json` records validation of the full
release; `COMPACT-VALIDATION.json` records validation of this compact distribution.

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
original hashes. See `TEXT-CHANGES.md` for manuscript changes. This archive does not
assert a new license grant.
