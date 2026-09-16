"""Shared repository and run identity helpers."""

from __future__ import annotations

import secrets
import subprocess
import time
from pathlib import Path
from typing import Literal

_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def generate_run_id() -> str:
    """Generate a sortable, collision-resistant 26-character ULID."""

    value = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
    chars = ["0"] * 26
    for index in range(25, -1, -1):
        chars[index] = _CROCKFORD32[value & 31]
        value >>= 5
    return "".join(chars)


def repository_identity() -> tuple[str | None, Literal["versioned", "dirty", "unversioned"]]:
    root = project_root()
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
        return commit, "dirty" if dirty else "versioned"
    except (OSError, subprocess.SubprocessError):
        return None, "unversioned"
