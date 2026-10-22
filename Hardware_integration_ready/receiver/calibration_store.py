"""calibration_store.py — atomic manual-cuff calibration capture, expiry, and
invalidation (integration spec §7).

Policy defaults (configurable on Jetson, per spec):
  - warning_after_s   = 14400  (4h)
  - hard_expiry_s     = 21600  (6h)
Never default to 86400s — that was explicitly rejected by the integration
spec in favor of the 4h/6h clinical-safety window.

E2E hardening pass correction: a calibration is now only ever captured with
demographics validated as raw (age in years or None, canonical sex "F"/"M"/
None — see http_service.py, which runs these through the sibling's frozen
`veinnet_runtime.demographics_transform` rather than accepting a
caller-supplied age_z), and only after the baseline window has passed both
hardware SQI and the sibling's frozen preprocessing — capture() itself
refuses a record that looks like it skipped that (empty morphology, or
preprocessing_version=="UNSET"), so a "successful" calibration can never be
partially real.
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


DEFAULT_WARNING_AFTER_S = 14400
DEFAULT_HARD_EXPIRY_S = 21600

UNSET_PREPROCESSING_VERSION = "UNSET"


class CalibrationStatus(str, Enum):
    VALID = "VALID"
    WARNING = "WARNING"           # past warning_after_s, still usable
    EXPIRED = "EXPIRED"           # past hard_expiry_s — must RECALIBRATE
    MISSING = "MISSING"           # no calibration recorded for this identity yet
    INVALIDATED = "INVALIDATED"   # explicitly invalidated (reposition, device change, etc.)


class CalibrationCaptureError(ValueError):
    """Raised when a calibration capture attempt does not meet the minimum
    bar for a 'successful' calibration (finite cuff values, a real
    preprocessing_version, non-empty processed baseline PPG/morphology).
    Never silently accepted as degraded-but-stored."""


@dataclass
class CalibrationRecord:
    calibration_id: str
    patient_id: str
    device_id: str
    sensor_session_id: int
    sensor_config_id: int
    baseline_cuff_sbp: float
    baseline_cuff_dbp: float
    baseline_ppg_red: list[int]          # raw, as acquired
    baseline_ppg_ir: list[int]           # raw, as acquired
    baseline_ppg_processed: list[float]  # sibling-preprocessed, flattened [3,1000]
    baseline_morphology: list[float]     # sibling-computed 13-feature vector
    age_years: float | None
    sex: str | None                      # exactly "F", "M", or None
    firmware_version: str
    preprocessing_version: str
    captured_at_utc: float               # unix epoch seconds
    invalidated: bool = False
    # Prototype-continuous mode only: {"spo2_pct": reference oximeter value, "r": ratio-of-ratios of the baseline window}
    spo2_reference: dict | None = None
    invalidation_reason: str | None = None

    def age_s(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.captured_at_utc

    def to_json_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_json_dict(cls, d: dict) -> "CalibrationRecord":
        return cls(**d)


class CalibrationStore:
    """One active calibration per (patient_id, device_id) identity. Storage
    is a single JSON file written atomically (write-temp + rename) so a
    crash mid-write can never leave a torn/partial calibration record —
    spec §7 "one synchronized transaction"."""

    def __init__(
        self,
        storage_path: Path,
        warning_after_s: int = DEFAULT_WARNING_AFTER_S,
        hard_expiry_s: int = DEFAULT_HARD_EXPIRY_S,
    ) -> None:
        if hard_expiry_s <= warning_after_s:
            raise ValueError("hard_expiry_s must be greater than warning_after_s")
        self.storage_path = storage_path
        self.warning_after_s = warning_after_s
        self.hard_expiry_s = hard_expiry_s
        self._records: dict[str, CalibrationRecord] = {}
        self._history: list[dict] = []
        self._load()

    def _identity_key(self, patient_id: str, device_id: str) -> str:
        return f"{patient_id}::{device_id}"

    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        raw = json.loads(self.storage_path.read_text())
        if raw.get("format_version") == "2.0.0":
            self._history = raw["history"]
            raw = raw["active"]
        self._records = {k: CalibrationRecord.from_json_dict(v) for k, v in raw.items()}

    def _persist(self) -> None:
        # Calibration records contain cuff BP and raw/processed baseline PPG
        # — treat the file as sensitive: owner-only permissions, and never
        # log its contents (callers must not print `payload` or any
        # CalibrationRecord).
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.storage_path.parent, stat.S_IRWXU)  # 0700
        except OSError:
            pass  # best-effort on filesystems that don't support chmod (e.g. some CI mounts)

        tmp = self.storage_path.with_suffix(self.storage_path.suffix + ".tmp")
        payload = {"format_version": "2.0.0", "active": {k: v.to_json_dict() for k, v in self._records.items()}, "history": self._history}
        tmp.write_text(json.dumps(payload, indent=2))
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
        tmp.replace(self.storage_path)  # atomic on POSIX
        try:
            os.chmod(self.storage_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

    def capture(
        self,
        *,
        patient_id: str,
        device_id: str,
        sensor_session_id: int,
        sensor_config_id: int,
        baseline_cuff_sbp: float,
        baseline_cuff_dbp: float,
        baseline_ppg_red: list[int],
        baseline_ppg_ir: list[int],
        baseline_ppg_processed: list[float],
        baseline_morphology: list[float],
        age_years: float | None,
        sex: str | None,
        firmware_version: str,
        preprocessing_version: str,
        now: float | None = None,
    ) -> CalibrationRecord:
        if not _is_finite(baseline_cuff_sbp) or not _is_finite(baseline_cuff_dbp):
            raise CalibrationCaptureError("baseline_cuff_sbp/baseline_cuff_dbp must be finite numbers")
        if baseline_cuff_sbp <= 0 or baseline_cuff_dbp <= 0 or baseline_cuff_dbp >= baseline_cuff_sbp:
            raise CalibrationCaptureError(
                f"implausible cuff values: sbp={baseline_cuff_sbp}, dbp={baseline_cuff_dbp}"
            )
        if not baseline_morphology:
            raise CalibrationCaptureError(
                "baseline_morphology must not be empty — capture the sibling's processed "
                "morphology, never a placeholder for a 'successful' calibration"
            )
        if not baseline_ppg_processed:
            raise CalibrationCaptureError("baseline_ppg_processed must not be empty")
        if not preprocessing_version or preprocessing_version == UNSET_PREPROCESSING_VERSION:
            raise CalibrationCaptureError(
                f"preprocessing_version must be a real version, not {preprocessing_version!r} "
                "— a calibration reported successful must record which preprocessing produced it"
            )
        if sex is not None and sex not in ("F", "M"):
            raise CalibrationCaptureError(f"sex must be exactly 'F', 'M', or None; got {sex!r}")

        record = CalibrationRecord(
            calibration_id=str(uuid.uuid4()),
            patient_id=patient_id,
            device_id=device_id,
            sensor_session_id=sensor_session_id,
            sensor_config_id=sensor_config_id,
            baseline_cuff_sbp=baseline_cuff_sbp,
            baseline_cuff_dbp=baseline_cuff_dbp,
            baseline_ppg_red=baseline_ppg_red,
            baseline_ppg_ir=baseline_ppg_ir,
            baseline_ppg_processed=baseline_ppg_processed,
            baseline_morphology=baseline_morphology,
            age_years=age_years,
            sex=sex,
            firmware_version=firmware_version,
            preprocessing_version=preprocessing_version,
            captured_at_utc=now if now is not None else time.time(),
        )
        previous = self.get(patient_id, device_id)
        if previous:
            self._history.append({"type": "REPLACED", "recorded_at_utc": time.time(), "record": previous.to_json_dict()})
        self._records[self._identity_key(patient_id, device_id)] = record
        try:
            self._persist()
        except Exception:
            if previous:
                self._records[self._identity_key(patient_id, device_id)] = previous
                self._history.pop()
            else:
                self._records.pop(self._identity_key(patient_id, device_id), None)
            raise
        return record

    def get(self, patient_id: str, device_id: str) -> CalibrationRecord | None:
        return self._records.get(self._identity_key(patient_id, device_id))

    def set_spo2_reference(self, patient_id: str, device_id: str, spo2_pct: float, r: float) -> None:
        """Prototype-continuous mode: anchor session SpO2 to a reference oximeter value
        taken with the calibration baseline. Does not touch BP calibration values."""
        record = self.get(patient_id, device_id)
        if record is None:
            return
        record.spo2_reference = {"spo2_pct": float(spo2_pct), "r": float(r)}
        self._persist()

    def invalidate(self, patient_id: str, device_id: str, reason: str) -> None:
        record = self.get(patient_id, device_id)
        if record is None:
            return
        self._history.append({"type": "INVALIDATED", "recorded_at_utc": time.time(), "record": record.to_json_dict()})
        record.invalidated = True
        record.invalidation_reason = reason
        self._persist()

    def status(
        self,
        patient_id: str,
        device_id: str,
        *,
        current_sensor_session_id: int | None = None,
        current_sensor_config_id: int | None = None,
        now: float | None = None,
    ) -> CalibrationStatus:
        record = self.get(patient_id, device_id)
        if record is None:
            return CalibrationStatus.MISSING
        if record.invalidated:
            return CalibrationStatus.INVALIDATED
        if (
            current_sensor_session_id is not None
            and current_sensor_session_id != record.sensor_session_id
        ):
            # Session mismatch: spec §7 invalidates calibration on sensor
            # repositioning / contact loss+reacquisition (both bump
            # sensor_session_id). Reported, not silently patched — callers
            # that detect this should also persist an explicit invalidate().
            return CalibrationStatus.INVALIDATED
        if (
            current_sensor_config_id is not None
            and current_sensor_config_id != record.sensor_config_id
        ):
            # Config mismatch: spec §7 "material optical-profile change"
            # (e.g. fingertip <-> dorsal) invalidates calibration.
            return CalibrationStatus.INVALIDATED

        age = record.age_s(now)
        if age >= self.hard_expiry_s:
            return CalibrationStatus.EXPIRED
        if age >= self.warning_after_s:
            return CalibrationStatus.WARNING
        return CalibrationStatus.VALID

    def usable(
        self,
        patient_id: str,
        device_id: str,
        *,
        current_sensor_session_id: int | None = None,
        current_sensor_config_id: int | None = None,
        now: float | None = None,
    ) -> bool:
        """True only for VALID or WARNING — EXPIRED/MISSING/INVALIDATED must
        never allow a new BP prediction (spec §7: after expiry, return
        RECALIBRATE, never a stale prediction)."""
        s = self.status(
            patient_id,
            device_id,
            current_sensor_session_id=current_sensor_session_id,
            current_sensor_config_id=current_sensor_config_id,
            now=now,
        )
        return s in (CalibrationStatus.VALID, CalibrationStatus.WARNING)


def _is_finite(x) -> bool:
    try:
        return x == x and abs(x) != float("inf")
    except TypeError:
        return False
