"""dashboard_api.py — the FROZEN result schema consumed by
VeinoGate_Dashboard_integration_ready, producer-side.

The wire schema is not defined here — it is the literal byte-for-byte copy
of the dashboard's own schemas at schemas/jetson-result.schema.json and
schemas/jetson-ws-envelope.schema.json (copied from
VeinoGate_Dashboard_integration_ready/schemas/, verified identical by
scripts/check-schema-drift.mjs in that sibling bundle). This module's
VitalsResult dataclass and build_result() exist only to construct payloads
that are guaranteed to validate against that copied schema —
tests/test_schema_validation.py checks every example this module produces.

Canonical identity: schema_version "1.0.0", message_type "vitals_result".
Do NOT introduce a competing schema version or a "state"/flat-BP alias —
the nested bp/hr/spo2/calibration/sqi/provenance structure below is the
only shape this bundle emits.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum


RESULT_SCHEMA_VERSION = "1.1.0"
MESSAGE_TYPE_VITALS_RESULT = "vitals_result"
MESSAGE_TYPE_CONNECTION_STATE = "connection_state"
MESSAGE_TYPE_ALARM_EVENT = "alarm_event"

BP_UNIT = "mmHg"


class ResultState(str, Enum):
    VALID = "VALID"
    ABSTAIN = "ABSTAIN"
    RECALIBRATE = "RECALIBRATE"
    INFERENCE_ERROR = "INFERENCE_ERROR"
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"


class ConnectionStatus(str, Enum):
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DISCONNECTED = "DISCONNECTED"


class CalibrationDisplayStatus(str, Enum):
    UNCALIBRATED = "UNCALIBRATED"
    CALIBRATING = "CALIBRATING"
    CALIBRATED = "CALIBRATED"
    RECALIBRATION_DUE = "RECALIBRATION_DUE"
    EXPIRED = "EXPIRED"
    SENSOR_REPOSITIONED = "SENSOR_REPOSITIONED"
    CALIBRATION_FAILED = "CALIBRATION_FAILED"


class Spo2Status(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"
    VALID = "VALID"
    INVALID = "INVALID"


# The dashboard schema's sqi.reason_codes is a closed enum — never emit a
# free-text reason. Every code Hardware's own window/BLE-integrity layer can
# produce (window_assembler.py) is mapped 1:1 below; anything from the
# sibling's frozen preprocessing (veinnet_runtime) already returns one of
# these exact strings and passes through unchanged.
SQI_REASON_CODES = frozenset({
    "NO_PACKETS", "SAMPLE_RATE_CHANGED", "MALFORMED_PACKET",
    "DUPLICATE_OR_REORDERED_SAMPLE", "PACKET_LOSS", "FIFO_OVERFLOW", "SATURATION_FLAG",
    "CLIPPING", "CONTACT_CHANGED", "EXCESSIVE_MOTION", "WINDOW_LENGTH",
    "NONFINITE", "POOR_CONTACT", "RED_SQI_CORRELATION", "RED_SQI_PULSATILITY",
    "RESAMPLE_LENGTH", "FLATLINE", "UNKNOWN",
})


def to_iso_utc(unix_seconds: float) -> str:
    """Formats a unix timestamp as an ISO-8601 UTC string matching
    JavaScript's `Date.prototype.toISOString()` (milliseconds, trailing
    'Z') — what the dashboard's `Date.parse()` (messageGuard.ts) and Ajv's
    `format: date-time` both expect."""
    dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def normalize_sqi_reason_code(code: str) -> str:
    """Maps any free-text/legacy reason string to a schema-valid code,
    falling back to 'UNKNOWN' rather than ever emitting an out-of-enum
    value (which would fail dashboard-side Ajv validation outright)."""
    return code if code in SQI_REASON_CODES else "UNKNOWN"


@dataclass
class ConnectionInfo:
    status: ConnectionStatus
    data_age_seconds: float | None = None


@dataclass
class BpInfo:
    sbp_mmhg: float | None
    dbp_mmhg: float | None
    valid: bool
    unit: str = BP_UNIT


@dataclass
class HrInfo:
    bpm: float | None
    valid: bool
    status: str = "UNAVAILABLE"
    reason: str | None = "NO_WINDOW"
    algorithm_name: str | None = None
    algorithm_version: str | None = None
    experimental: bool = True
    window_start_utc: str | None = None
    window_end_utc: str | None = None


@dataclass
class Spo2Info:
    pct: float | None = None
    status: Spo2Status = Spo2Status.UNAVAILABLE
    algorithm_name: str | None = None
    algorithm_version: str | None = None

    def __post_init__(self) -> None:
        if self.status == Spo2Status.VALID:
            if self.pct is None or self.algorithm_name is None or self.algorithm_version is None:
                raise ValueError("Spo2Info status VALID requires pct, algorithm_name, and algorithm_version")
        else:
            if self.pct is not None:
                raise ValueError(f"Spo2Info status {self.status} requires pct=None")


@dataclass
class CalibrationInfo:
    status: CalibrationDisplayStatus
    calibration_id: str | None
    calibrated_at_utc: str | None  # ISO-8601 or None
    warning_threshold_seconds: float
    expiry_threshold_seconds: float
    age_seconds: float | None
    reason: str | None


@dataclass
class SqiMetrics:
    red_ir_correlation: float | None = None
    red_pulsatility: float | None = None
    packet_loss_fraction: float | None = None
    duplicate_or_reordered_count: int | None = None
    overflow_count: int | None = None
    sample_count: int | None = None


@dataclass
class SqiInfo:
    accepted: bool
    reason_codes: list[str] = field(default_factory=list)
    metrics: SqiMetrics = field(default_factory=SqiMetrics)


@dataclass
class ProvenanceInfo:
    model_name: str | None
    model_version: str | None
    model_sha256: str | None
    onnx_opset: int | None
    preprocessing_version: str | None
    morphology_version: str | None
    sqi_version: str | None
    calibration_protocol_version: str | None
    ble_protocol_version: str | None
    sensor_config_version: str | None


@dataclass
class VitalsResult:
    schema_version: str
    message_type: str
    result_state: ResultState
    reading_id: str
    session_id: str
    patient_id: str
    device_id: str
    acquisition_start_utc: str
    acquisition_end_utc: str
    emitted_at_utc: str
    connection: ConnectionInfo
    bp: BpInfo
    hr: HrInfo
    spo2: Spo2Info
    calibration: CalibrationInfo
    sqi: SqiInfo
    provenance: ProvenanceInfo

    def to_dict(self) -> dict:
        d = asdict(self)
        d["result_state"] = self.result_state.value
        d["connection"]["status"] = self.connection.status.value
        d["calibration"]["status"] = self.calibration.status.value
        d["spo2"]["status"] = self.spo2.status.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def map_calibration_status(status: str) -> CalibrationDisplayStatus:
    """Maps calibration_store.CalibrationStatus values to the dashboard's
    richer CalibrationDisplayStatus enum. Both are string enums so this
    accepts either the enum member or its .value string."""
    value = status.value if hasattr(status, "value") else status
    return {
        "MISSING": CalibrationDisplayStatus.UNCALIBRATED,
        "VALID": CalibrationDisplayStatus.CALIBRATED,
        "WARNING": CalibrationDisplayStatus.RECALIBRATION_DUE,
        "EXPIRED": CalibrationDisplayStatus.EXPIRED,
        "INVALIDATED": CalibrationDisplayStatus.SENSOR_REPOSITIONED,
    }[value]


def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def build_result(
    *,
    reading_id: str,
    session_id: str,
    patient_id: str,
    device_id: str,
    acquisition_start_utc: float,
    acquisition_end_utc: float,
    result_state: ResultState,
    sbp_mmhg: float | None,
    dbp_mmhg: float | None,
    connection: ConnectionInfo,
    calibration: CalibrationInfo,
    sqi: SqiInfo,
    provenance: ProvenanceInfo,
    hr: HrInfo | None = None,
    spo2: Spo2Info | None = None,
    emitted_at_utc: float | None = None,
) -> VitalsResult:
    valid = result_state == ResultState.VALID
    if valid:
        if not _is_finite_number(sbp_mmhg) or not _is_finite_number(dbp_mmhg):
            raise ValueError("VALID results require finite sbp_mmhg and dbp_mmhg")
    else:
        if sbp_mmhg is not None or dbp_mmhg is not None:
            raise ValueError(f"{result_state} results must carry null sbp_mmhg/dbp_mmhg")

    if result_state == ResultState.ABSTAIN and not sqi.reason_codes:
        raise ValueError("ABSTAIN results require at least one sqi.reason_codes entry")
    for code in sqi.reason_codes:
        if code not in SQI_REASON_CODES:
            raise ValueError(f"sqi reason code {code!r} is not in the frozen dashboard enum")

    if result_state == ResultState.RECALIBRATE and not calibration.reason:
        raise ValueError("RECALIBRATE results require calibration.reason")

    return VitalsResult(
        schema_version=RESULT_SCHEMA_VERSION,
        message_type=MESSAGE_TYPE_VITALS_RESULT,
        result_state=result_state,
        reading_id=reading_id,
        session_id=session_id,
        patient_id=patient_id,
        device_id=device_id,
        acquisition_start_utc=to_iso_utc(acquisition_start_utc),
        acquisition_end_utc=to_iso_utc(acquisition_end_utc),
        emitted_at_utc=to_iso_utc(emitted_at_utc if emitted_at_utc is not None else time.time()),
        connection=connection,
        bp=BpInfo(sbp_mmhg=sbp_mmhg, dbp_mmhg=dbp_mmhg, valid=valid),
        hr=hr if hr is not None else HrInfo(bpm=None, valid=False),
        spo2=spo2 if spo2 is not None else Spo2Info(),
        calibration=calibration,
        sqi=sqi,
        provenance=provenance,
    )
