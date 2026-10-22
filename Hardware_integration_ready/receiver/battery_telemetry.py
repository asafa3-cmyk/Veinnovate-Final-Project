"""ADC-derived battery telemetry; physical evidence is scoped to UID/firmware."""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

POLICY_VERSION = "POC_BATTERY_DISPLAY_3600_3900_V1"
TOLERANCE_MV = 100
MAX_STATUS_AGE_S = 5.0
FIRMWARE_VERSION = "3.0.2"


def percentage_from_mv(mv: int | None) -> int | None:
    if mv is None or isinstance(mv, bool) or not isinstance(mv, int) or not 2500 <= mv <= 4500:
        return None
    # Integer round-half-up; one canonical operational display scale.
    return max(0, min(100, (100 * (mv - 3600) + 150) // 300))


def millivolts_from_raw(raw: int) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 < raw < 4095:
        return None
    mv = (raw * 3600 * 1510 + 4096 * 510 // 2) // (4096 * 510)
    return mv if 2500 <= mv <= 4500 else None


def validate_evidence(record: dict) -> None:
    """Accept only three identified USB readings compared with the same DC meter."""
    uid = record.get("hardware_uid", "")
    if not re.fullmatch(r"[0-9A-F]{16}", uid) or uid in ("0" * 16, "F" * 16):
        raise ValueError("invalid battery evidence UID")
    code = record.get("short_code", "")
    if not re.fullmatch(r"VN-[0-9]{3,5}", code) or int(code[3:]) == 0:
        raise ValueError("invalid battery evidence code")
    if (record.get("firmware_version") != FIRMWARE_VERSION
            or record.get("core_version") != "1.1.9"
            or record.get("board_model") != "XIAO nRF52840"
            or not record.get("board_revision") or not record.get("meter_model")
            or record.get("battery_connected") is not True
            or record.get("tolerance_mv") != TOLERANCE_MV):
        raise ValueError("battery evidence prerequisites missing")
    meter = record.get("multimeter_mv")
    resolution = record.get("meter_resolution_mv")
    if (type(meter) is not int or not 2500 <= meter <= 4500
            or type(resolution) is not int or not 0 < resolution <= 10):
        raise ValueError("invalid DC multimeter reference")
    meter_at = record.get("multimeter_at_utc")
    if type(meter_at) not in (int, float) or not math.isfinite(meter_at):
        raise ValueError("multimeter timestamp required")
    readings = record.get("readings", [])
    if len(readings) != 3:
        raise ValueError("exactly three battery readings required")
    previous = -1
    previous_uptime = -1
    for reading in readings:
        mv = millivolts_from_raw(reading.get("raw_adc"))
        stamp = reading.get("host_utc")
        uptime = reading.get("uptime_ms")
        if (reading.get("event") != "battery" or reading.get("diagnostic_version") != "1.0.0"
                or reading.get("hardware_uid") != uid or reading.get("short_code") != code
                or reading.get("firmware_version") != FIRMWARE_VERSION
                or reading.get("charge_indication") not in ("CHARGING", "NOT_CHARGING_OR_FULL")
                or reading.get("usb_connected") is not True
                or reading.get("validation_status") != "HARDWARE_VALIDATION_REQUIRED"
                or mv is None or reading.get("battery_mv") != mv
                or abs(mv - meter) > TOLERANCE_MV
                or type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= previous
                or abs(stamp - meter_at) > 60
                or type(uptime) is not int or uptime <= previous_uptime):
            raise ValueError("battery reading fails physical evidence check")
        previous = stamp
        previous_uptime = uptime


def load_validations() -> dict[str, dict]:
    path = os.environ.get("VEINNET_BATTERY_VALIDATION_FILE")
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    if data.get("schema_version") != "1.0.0" or not isinstance(data.get("devices"), list):
        raise ValueError("unsupported battery validation registry")
    records = {}
    for record in data["devices"]:
        validate_evidence(record)
        uid = record["hardware_uid"]
        if uid in records:
            raise ValueError("duplicate battery validation UID")
        records[uid] = record
    return records


def battery_snapshot(orchestrator, validations: dict[str, dict], *, now: float) -> dict:
    result = {"value_mv": None, "pct": None, "status": "UNAVAILABLE",
              "measured_at_utc": None, "charge_indication": "UNKNOWN",
              "display_policy_version": POLICY_VERSION}
    status = getattr(orchestrator, "last_status", None)
    if orchestrator.connection_status != "CONNECTED":
        result["status"] = "UNAVAILABLE"
        return result
    if status is None:
        result["status"] = "HARDWARE_VALIDATION_REQUIRED"
        return result
    received = getattr(orchestrator, "last_status_at_utc", None)
    monotonic = getattr(orchestrator, "last_status_monotonic", None)
    if received is None or monotonic is None or not 0 <= time.monotonic() - monotonic <= MAX_STATUS_AGE_S:
        result["status"] = "STALE"
        return result
    expected = getattr(orchestrator, "expected_hardware_uid", None)
    if expected is None or status.hardware_uid != (expected if isinstance(expected, int) else int(expected, 16)):
        return result
    uid = f"{status.hardware_uid:016X}"
    record = validations.get(uid)
    if record is None or record["firmware_version"] != ".".join(map(str, status.firmware_version)):
        result["status"] = "HARDWARE_VALIDATION_REQUIRED"
        return result
    if percentage_from_mv(status.battery_mv) is None:
        return result
    result.update(value_mv=status.battery_mv, pct=percentage_from_mv(status.battery_mv),
                  status="AVAILABLE", measured_at_utc=received,
                  charge_indication={0: "NOT_CHARGING_OR_FULL", 1: "CHARGING"}.get(status.charge_state, "UNKNOWN"))
    return result
