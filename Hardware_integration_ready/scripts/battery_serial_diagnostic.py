#!/usr/bin/env python3
"""Three bounded, identity-checked BATTERY requests to production firmware."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import select
import termios
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
from battery_telemetry import millivolts_from_raw, validate_evidence, TOLERANCE_MV
from stage8_serial_command import _configure_serial


def check_reading(reading: dict, uid: str, code: str) -> dict:
    if (reading.get("event") != "battery" or reading.get("diagnostic_version") != "1.0.0"
            or reading.get("hardware_uid") != uid or reading.get("short_code") != code
            or reading.get("firmware_version") != "3.0.2"):
        raise ValueError("diagnostic identity/version mismatch; stop")
    if type(reading.get("uptime_ms")) is not int or reading["uptime_ms"] < 0:
        raise ValueError("invalid diagnostic uptime; stop")
    raw_mv = millivolts_from_raw(reading.get("raw_adc"))
    if (reading.get("battery_mv") != raw_mv
            or reading.get("usb_connected") is not True
            or reading.get("charge_indication") not in ("CHARGING", "NOT_CHARGING_OR_FULL")
            or raw_mv is None or reading.get("validation_status") != "HARDWARE_VALIDATION_REQUIRED"):
        raise ValueError("invalid, unavailable, or busy battery reading; stop")
    return reading


def format_reading(reading: dict, *, validated: bool = False) -> str:
    stamp = datetime.fromtimestamp(reading["host_utc"], timezone.utc).isoformat(timespec="milliseconds")
    return (f"{stamp} UID={reading['hardware_uid']} code={reading['short_code']} "
            f"ADC={reading['raw_adc']} battery={reading['battery_mv']} mV "
            f"charge={reading['charge_indication']} USB_CONNECTED=true "
            f"validation={'VALIDATED_USB_DC_100MV' if validated else reading['validation_status']} "
            "precision=1mV_encoding accuracy=not_established")


def read_three(port: str, uid: str, code: str, readings: list[dict] | None = None) -> list[dict]:
    readings = [] if readings is None else readings
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        _configure_serial(fd) # Reuse existing 115200 read-only diagnostic setup.
        termios.tcflush(fd, termios.TCIFLUSH)
        buffer = b""
        for _ in range(3):
            if os.write(fd, b"BATTERY\n") != 8:
                raise OSError("incomplete diagnostic command write; stop")
            deadline = time.monotonic() + 5
            received = False
            while time.monotonic() < deadline:
                if not select.select([fd], [], [], min(0.25, max(0, deadline-time.monotonic())))[0]:
                    continue
                data = os.read(fd, 4096)
                if not data:
                    raise OSError("serial disconnected; stop")
                buffer += data
                if len(buffer) > 8192:
                    raise ValueError("oversized serial response; stop")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.startswith(b'{"event":"battery"'):
                        continue
                    try:
                        reading = json.loads(line)
                    except ValueError:
                        readings.append({"host_utc": time.time(), "raw_line": line.decode(errors="replace"),
                                         "validation_status": "MALFORMED"})
                        raise
                    reading["host_utc"] = time.time()
                    readings.append(reading)
                    check_reading(reading, uid, code)
                    if len(readings) > 1 and reading["uptime_ms"] <= readings[-2]["uptime_ms"]:
                        raise ValueError("repeated or restarted diagnostic uptime; stop")
                    print(format_reading(reading), flush=True)
                    received = True
                    break
                if received:
                    break
            if not received:
                raise TimeoutError("no BATTERY diagnostic within 5 s; stop without retry")
            if len(readings) < 3:
                time.sleep(1)
    finally:
        os.close(fd)
    return readings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port")
    parser.add_argument("--transcript", type=Path, help="Validate a retained transcript without serial access")
    parser.add_argument("--uid", required=True)
    parser.add_argument("--short-code", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--board-revision")
    parser.add_argument("--battery-connected", action="store_true")
    parser.add_argument("--meter-model")
    parser.add_argument("--meter-resolution-mv", type=int)
    parser.add_argument("--multimeter-mv", type=int)
    parser.add_argument("--multimeter-at-utc", type=float)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; retain previous evidence")
    if bool(args.port) == bool(args.transcript):
        parser.error("choose exactly one of --port or --transcript")
    records = []
    outcome = "ABORTED_RETAINED"
    try:
        if args.transcript:
            transcript = json.loads(args.transcript.read_text())
            if transcript.get("outcome") != "HARDWARE_VALIDATION_REQUIRED":
                raise ValueError("transcript was not completed; stop")
            records = transcript["readings"]
            record = dict(hardware_uid=args.uid, short_code=args.short_code,
                          firmware_version="3.0.2", core_version="1.1.9",
                          board_model="XIAO nRF52840", board_revision=args.board_revision,
                          battery_connected=args.battery_connected, meter_model=args.meter_model,
                          meter_resolution_mv=args.meter_resolution_mv, multimeter_mv=args.multimeter_mv,
                          multimeter_at_utc=args.multimeter_at_utc, tolerance_mv=TOLERANCE_MV,
                          readings=records)
            validate_evidence(record)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"schema_version": "1.0.0", "devices": [record]}, indent=2) + "\n")
            for reading in records:
                print(format_reading(reading, validated=True), flush=True)
            return 0
        read_three(args.port, args.uid, args.short_code, records)
        outcome = "HARDWARE_VALIDATION_REQUIRED"
        return_code = 0
    except (ValueError, OSError, TimeoutError) as exc:
        outcome = str(exc)
        return_code = 2
        print(outcome, file=sys.stderr)
    finally:
        if not args.output.exists():
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"schema_version": "1.0.0", "readings": records,
                                              "outcome": outcome}, indent=2) + "\n")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
