#!/usr/bin/env python3
"""Build one PHI-free production firmware artifact for a reserved UID/VN pair."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROTOCOL_VERSION = 2
FQBN = "Seeeduino:nrf52:xiaonRF52840"
CORE_ID = "Seeeduino:nrf52"
PINNED_CORE_VERSION = "1.1.9"
PINNED_MAX3010X_VERSION = "1.1.2"
UID_PATTERN = re.compile(r"^[0-9A-F]{16}$")
SHORT_CODE_PATTERN = re.compile(r"^VN-([0-9]{3,5})$")


class ProvisioningBuildError(ValueError):
    pass


def validate_uid(uid: str) -> str:
    if not isinstance(uid, str) or UID_PATTERN.fullmatch(uid) is None:
        raise ProvisioningBuildError("UID must be exactly 16 uppercase hexadecimal characters")
    if uid in {"0000000000000000", "FFFFFFFFFFFFFFFF"}:
        raise ProvisioningBuildError("sentinel UID is forbidden")
    return uid


def parse_short_code(short_code: str) -> int:
    match = SHORT_CODE_PATTERN.fullmatch(short_code) if isinstance(short_code, str) else None
    if match is None:
        raise ProvisioningBuildError("short code must be canonical VN-NNN")
    number = int(match.group(1))
    if number == 0 or number > 0xFFFF or short_code != f"VN-{number:03d}":
        raise ProvisioningBuildError("short-code suffix must be canonical and in uint16 range 1..65535")
    return number


def compile_command(*, sketch_dir: Path, output_dir: Path, code_number: int) -> list[str]:
    return [
        "arduino-cli",
        "compile",
        "--clean",
        "--fqbn",
        FQBN,
        "--output-dir",
        str(output_dir),
        "--build-path",
        str(output_dir / "_build"),
        "--build-property",
        f"compiler.cpp.extra_flags=-DVEINNET_SHORT_CODE_NUMBER={code_number}",
        str(sketch_dir),
    ]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_json(command: list[str]) -> dict:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _verify_toolchain() -> tuple[str, str, str]:
    cli = _run_json(["arduino-cli", "version", "--format", "json"])["VersionString"]
    cores = _run_json(["arduino-cli", "core", "list", "--format", "json"])["platforms"]
    installed = next((p.get("installed_version") for p in cores if p.get("id") == CORE_ID), None)
    if installed != PINNED_CORE_VERSION:
        raise ProvisioningBuildError(
            f"required {CORE_ID}@{PINNED_CORE_VERSION}, found {installed or 'not installed'}"
        )
    libraries = _run_json(["arduino-cli", "lib", "list", "--format", "json"])["installed_libraries"]
    max3010x = next(
        (
            item["library"].get("version")
            for item in libraries
            if item.get("library", {}).get("name") == "SparkFun MAX3010x Pulse and Proximity Sensor Library"
        ),
        None,
    )
    if max3010x != PINNED_MAX3010X_VERSION:
        raise ProvisioningBuildError(
            f"required SparkFun MAX3010x@{PINNED_MAX3010X_VERSION}, found {max3010x or 'not installed'}"
        )
    return cli, installed, max3010x


def build(*, uid: str, short_code: str, output_dir: Path, bundle_root: Path) -> Path:
    uid = validate_uid(uid)
    code_number = parse_short_code(short_code)
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ProvisioningBuildError("output directory already exists; use a unique new directory")
    if not output_dir.parent.is_dir():
        raise ProvisioningBuildError("output directory parent does not exist")

    sketch_dir = bundle_root / "firmware" / "veinnet_sensor_v1"
    sketch = sketch_dir / "veinnet_sensor_v1.ino"
    protocol = sketch_dir / "common" / "protocol.h"
    if not sketch.is_file() or not protocol.is_file():
        raise ProvisioningBuildError("approved production firmware sources are missing")
    source_text = sketch.read_text(encoding="utf-8")
    if "#define VEINNET_SHORT_CODE_NUMBER 0" not in source_text:
        raise ProvisioningBuildError("generic firmware default is not explicitly unprovisioned")
    if "NRF_FICR->DEVICEID[0]" not in source_text or "NRF_FICR->DEVICEID[1]" not in source_text:
        raise ProvisioningBuildError("firmware does not derive immutable UID from FICR")

    cli_version, core_version, max3010x_version = _verify_toolchain()
    command = compile_command(sketch_dir=sketch_dir, output_dir=output_dir, code_number=code_number)
    try:
        subprocess.run(command, check=True)
        stem = f"veinnet_sensor_v1_{uid}_{short_code}"
        expected = {
            "hex": output_dir / "veinnet_sensor_v1.ino.hex",
            "dfu_zip": output_dir / "veinnet_sensor_v1.ino.zip",
        }
        missing = [str(path.name) for path in expected.values() if not path.is_file()]
        if missing:
            raise ProvisioningBuildError(f"compile did not produce required artifacts: {', '.join(missing)}")
        artifacts = {}
        for kind, target in expected.items():
            artifacts[kind] = {"file": target.name, "sha256": sha256(target), "bytes": target.stat().st_size}
        for kind, extension in (("elf", "elf"), ("map", "map")):
            target = output_dir / f"veinnet_sensor_v1.ino.{extension}"
            if target.is_file():
                artifacts[kind] = {
                    "file": target.name,
                    "sha256": sha256(target),
                    "bytes": target.stat().st_size,
                }

        source_files = [sketch, sketch_dir / "MAX30105.cpp", sketch_dir / "MAX30105.h",
                        *sorted((sketch_dir / "common").glob("*.h"))]
        record = {
            "record_version": "1.0.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "hardware_uid": uid,
            "short_code": short_code,
            "short_code_number": code_number,
            "protocol_version": PROTOCOL_VERSION,
            "fqbn": FQBN,
            "toolchain": {
                "arduino_cli": cli_version,
                "seeed_nrf52_core": core_version,
                "sparkfun_max3010x": max3010x_version,
            },
            "compile_configuration": {
                "clean": True,
                "build_path": "_build",
                "build_property": f"compiler.cpp.extra_flags=-DVEINNET_SHORT_CODE_NUMBER={code_number}",
                "uid_compile_definition": None,
            },
            "source_hashes": {
                str(path.relative_to(bundle_root)): sha256(path) for path in source_files
            },
            "artifacts": artifacts,
            "physical_verification": "PENDING",
        }
        record_path = output_dir / f"{stem}.build-record.json"
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return record_path
    except Exception:
        # A failed attempt must not leave an apparently complete record.
        if output_dir.exists():
            failed = output_dir / "BUILD_FAILED"
            failed.write_text("Provisioning build incomplete; do not flash or finalize inventory.\n", encoding="utf-8")
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", required=True)
    parser.add_argument("--short-code", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    bundle_root = Path(__file__).resolve().parent.parent
    try:
        record = build(uid=args.uid, short_code=args.short_code, output_dir=args.output_dir, bundle_root=bundle_root)
    except (ProvisioningBuildError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
