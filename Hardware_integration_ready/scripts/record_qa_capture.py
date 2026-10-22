#!/usr/bin/env python3
"""Persist one deterministic Stage 7 capture from raw-notification JSONL.

The input is intentionally transport-neutral so a BLE callback can record
notifications immediately and the exact stream can later be replayed. Each
line must contain raw_base64, host_monotonic_ns, and host_utc.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BUNDLE_ROOT / "receiver"))

from qa_capture_recorder import CaptureIntegrityError, QaCaptureRecorder


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a PHI-free Stage 7 QA capture bundle")
    parser.add_argument("--packets-jsonl", required=True, type=Path)
    parser.add_argument("--hardware-uid", required=True)
    parser.add_argument("--short-code", required=True)
    parser.add_argument("--sensor-config-id", required=True, type=int, choices=(1, 2))
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()

    try:
        recorder = QaCaptureRecorder(
            hardware_uid=args.hardware_uid,
            short_code=args.short_code,
            sensor_config_id=args.sensor_config_id,
        )
        with args.packets_jsonl.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                try:
                    raw = base64.b64decode(row["raw_base64"], validate=True)
                    recorder.ingest(
                        raw,
                        host_monotonic_ns=row["host_monotonic_ns"],
                        host_utc=row["host_utc"],
                    )
                except Exception as exc:
                    raise CaptureIntegrityError(f"invalid input line {line_number}: {exc}") from exc
        artifact = recorder.persist(args.output_root)
    except (CaptureIntegrityError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"capture rejected: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({
        "capture_id": artifact.capture_id,
        "manifest_sha256": artifact.manifest_sha256,
        "path": str(artifact.path),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
