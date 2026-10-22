"""Fail-closed SHA-256 verification for frozen artifacts."""
from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file_hash(path: str | Path, expected_sha256: str) -> None:
    """Raises RuntimeError if the file at `path` does not hash to `expected_sha256`.

    Fail-closed: any mismatch, including a missing file, raises rather than
    returning a boolean the caller might ignore.
    """
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"Hash verification failed: file not found: {path}")
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise RuntimeError(
            f"Hash verification failed for {path}: expected {expected_sha256}, got {actual}"
        )
