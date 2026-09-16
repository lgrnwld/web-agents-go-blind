#!/usr/bin/env python3
"""Restore and verify the complete anonymous artifact from its compact encoding."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or not path.parts:
        raise ValueError(f"Unsafe archive path: {relative}")
    destination = root.joinpath(*path.parts)
    if not destination.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Archive path escapes output: {relative}")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("restored"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    audit = root / "raw-audit"
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Output already exists. Choose a new --output directory to preserve existing files.")

    # Check compact inputs before restoring any files. The checksums detect
    # corruption; they are not an authenticated signature of the research.
    for line in (root / "SHA256SUMS").read_text().splitlines():
        expected, relative = line.split("  ", 1)
        if digest(safe_path(root, relative).read_bytes()) != expected:
            raise ValueError(f"Compact package checksum mismatch: {relative}")

    records = {}
    for source in sorted((audit / "records").glob("*.jsonl")):
        for line in source.open(encoding="utf-8"):
            row = json.loads(line)
            if row["id"] in records:
                raise ValueError(f"Duplicate record: {row['id']}")
            records[row["id"]] = row["parts"]

    index = {}
    for line in (audit / "index.jsonl").open(encoding="utf-8"):
        row = json.loads(line)
        if row["path"] in index:
            raise ValueError(f"Duplicate path: {row['path']}")
        index[row["path"]] = row

    blobs = {}

    def blob(key: str) -> bytes:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid blob digest")
        if key not in blobs:
            data = (audit / "blobs" / "sha256" / key).read_bytes()
            if digest(data) != key:
                raise ValueError(f"Blob checksum mismatch: {key}")
            blobs[key] = data
        return blobs[key]

    overrides = {
        "README.md": "raw-audit/original-readme.md",
        ".gitignore": "raw-audit/original-gitignore",
    }
    count = 0
    total_bytes = 0
    output.mkdir(parents=True)
    manifest = (audit / "ORIGINAL-SHA256SUMS").read_bytes()
    for line in manifest.decode().splitlines():
        expected, relative = line.split("  ", 1)
        destination = safe_path(output, relative)
        if relative in index:
            row = index[relative]
            if row["sha256"] != expected:
                raise ValueError(f"Index/manifest digest mismatch: {relative}")
            if "blob" in row:
                data = blob(row["blob"])
            else:
                parts = records[row["record"]]
                data = b"".join(
                    part.encode("utf-8") if isinstance(part, str) else base64.b64encode(blob(part["base64_sha256"]))
                    for part in parts
                )
            if len(data) != row["size"]:
                raise ValueError(f"Restored size mismatch: {relative}")
        else:
            data = safe_path(root, overrides.get(relative, relative)).read_bytes()
        if digest(data) != expected:
            raise ValueError(f"Restored checksum mismatch: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(data)
        total_bytes += len(data)
        count += 1
        if count % 10000 == 0:
            print(f"Restored and verified {count:,} files", flush=True)
    (output / "SHA256SUMS").write_bytes(manifest)
    report = {
        "status": "passed",
        "files_verified_against_original_manifest": count,
        "restored_bytes_excluding_checksum_manifest": total_bytes,
        "raw_archive_paths_restored": len(index),
        "unique_text_records": len(records),
        "unique_binary_blobs_used": len(blobs),
        "exact_bytes_preserved": True,
    }
    print(json.dumps(report, indent=2), flush=True)
    print("Run the restored reproduce.py with the analysis dependencies installed.")


if __name__ == "__main__":
    main()
