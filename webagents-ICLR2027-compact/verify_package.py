#!/usr/bin/env python3
"""Check all distributed files against SHA256SUMS without third-party dependencies."""
import hashlib
from pathlib import Path

root = Path(__file__).resolve().parent
count = 0
for line in (root / 'SHA256SUMS').read_text().splitlines():
    expected, relative = line.split('  ', 1)
    path = root / relative
    h = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    if h.hexdigest() != expected:
        raise SystemExit(f'Hash mismatch: {relative}')
    count += 1
print(f'Verified {count} distributed files.')
