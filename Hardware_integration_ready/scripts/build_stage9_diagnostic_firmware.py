#!/usr/bin/env python3
"""Build the isolated, provisioned VN-001 Stage 9 DIAGNOSTIC-ONLY firmware
artifact -- identical to build_stage9_firmware.py except for one additional
compile-time override: -DSTAGE9_CAPTURE_SAMPLES=10000.

Why: a continuous multi-window diagnostic stream (pre-roll + several 30s
windows on one uninterrupted BLE stream, no STOP/START in between) needs
more than the production 3000-sample-per-capture limit before
stage9StartCapture()'s completion path fires (g_stage9SamplesSent ==
STAGE9_CAPTURE_SAMPLES -> stage9SafeOff(), which -- since the SHDN
correction -- genuinely shuts the sensor down). Without this override, such
a stream silently stalls exactly at 3000 samples with no error (observed
on-device and traced to this constant, not a BLE/hardware fault; see
firmware/veinnet_sensor_v1/common/stage9_experiment_profiles.h).

This is a SEPARATE, diagnostic-only build. build_stage9_firmware.py (the
production Stage 9 build, 3000-sample default) is unmodified by this file
and remains the one used for every real Stage 9B/dorsal-sweep/exposure
batch. Do not point real experiment plans at artifacts built by this
script.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from build_provisioned_firmware import (
    FQBN,
    ProvisioningBuildError,
    _verify_toolchain,
    sha256,
)

UID = "A90AFDAF672D66F1"
SHORT_CODE = "VN-001"
DIAGNOSTIC_CAPTURE_SAMPLES = 10000
BUILD_FLAGS = (
    "-DVEINNET_SHORT_CODE_NUMBER=1 -DVEINNET_STAGE9_EXPERIMENT=1 "
    f"-DSTAGE9_CAPTURE_SAMPLES={DIAGNOSTIC_CAPTURE_SAMPLES}"
)


def build(output_dir: Path, bundle_root: Path) -> Path:
    output_dir = output_dir.resolve()
    if output_dir.exists() or not output_dir.parent.is_dir():
        raise ProvisioningBuildError("output directory must be a unique path under an existing parent")
    sketch_dir = bundle_root / "firmware" / "veinnet_sensor_v1"
    plan = bundle_root / "config" / "stage9_dorsal_sweep.json"
    sources = [sketch_dir / "veinnet_sensor_v1.ino", sketch_dir / "MAX30105.cpp",
               sketch_dir / "MAX30105.h", *sorted((sketch_dir / "common").glob("*.h")), plan]
    if any(not path.is_file() for path in sources):
        raise ProvisioningBuildError("Stage 9 source or frozen plan is missing")
    cli, core, max3010x = _verify_toolchain()
    command = [
        "arduino-cli", "compile", "--clean", "--fqbn", FQBN,
        "--output-dir", str(output_dir), "--build-path", str(output_dir / "_build"),
        "--build-property", f"compiler.cpp.extra_flags={BUILD_FLAGS}", str(sketch_dir),
    ]
    output_dir.mkdir(mode=0o700)
    try:
        subprocess.run(command, check=True)
        package = output_dir / "veinnet_sensor_v1.ino.zip"
        image = output_dir / "veinnet_sensor_v1.ino.hex"
        if not package.is_file() or not image.is_file():
            raise ProvisioningBuildError("compile did not emit HEX and DFU package")
        record = {
            "record_version": "1.0.0",
            "artifact_role": "STAGE9_DIAGNOSTIC_ONLY_NOT_PRODUCTION",
            "diagnostic_capture_samples": DIAGNOSTIC_CAPTURE_SAMPLES,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "hardware_uid": UID,
            "short_code": SHORT_CODE,
            "fqbn": FQBN,
            "compile_flags": BUILD_FLAGS,
            "toolchain": {"arduino_cli": cli, "seeed_nrf52_core": core, "sparkfun_max3010x": max3010x},
            "source_hashes": {str(path.relative_to(bundle_root)): sha256(path) for path in sources},
            "artifacts": {
                "hex": {"file": image.name, "bytes": image.stat().st_size, "sha256": sha256(image)},
                "dfu_zip": {"file": package.name, "bytes": package.stat().st_size, "sha256": sha256(package)},
            },
            "physical_verification": "PENDING",
        }
        record_path = output_dir / "stage9_vn001_diagnostic.build-record.json"
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return record_path
    except Exception:
        (output_dir / "BUILD_FAILED").write_text("Stage 9 diagnostic build incomplete; do not flash.\n", encoding="utf-8")
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        record = build(args.output_dir, Path(__file__).resolve().parent.parent)
    except (ProvisioningBuildError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
