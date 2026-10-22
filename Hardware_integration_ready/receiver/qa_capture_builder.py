"""qa_capture_builder.py — builds and validates Stage 7 QA capture records
(docs/QA_CAPTURE_FORMAT_SPEC.md). Reference implementation for
schemas/qa-capture-record.schema.json.

This module is additive and QA-only: it does not change, call, or import
anything that participates in the clinical accept/reject path beyond
reading `sqi_gate`'s threshold constants (to avoid a second, drifting copy
of the same numbers — see spec §5).
"""

from __future__ import annotations

from dataclasses import dataclass

from capture_quality_metrics import channel_metrics, is_flatline, is_finite_sample, pearson_correlation
from sqi_gate import (
    MAX_CLIPPED_FRACTION,
    MAX_PACKET_LOSS_FRACTION,
    MIN_CONTACT_DC,
    MIN_RED_IR_CORRELATION,
    MIN_RED_PULSATILITY,
)
from window_assembler import AssembledWindow, WINDOW_SAMPLE_COUNT
from protocol_decoder import SensorConfigId

SCHEMA_VERSION = "1.1.0"
RECORD_TYPE = "qa_capture_record"

# New Stage 7 threshold; not part of the clinical sqi_gate rule set.
MIN_PSNR_DB = 3.0

_CONFIG_NAME_BY_ID = {
    int(SensorConfigId.UNVERIFIED): "UNVERIFIED",
    int(SensorConfigId.FINGERTIP): "FINGERTIP",
    int(SensorConfigId.DORSAL): "DORSAL",
}


@dataclass(frozen=True)
class RegisterSettings:
    sample_rate_sps: int
    sample_avg: int
    effective_sample_rate_hz: int
    pulse_width_us: int
    led_mode: int
    led_brightness_hex: str
    adc_range_na: int

    def to_dict(self) -> dict:
        return {
            "sample_rate_sps": self.sample_rate_sps,
            "sample_avg": self.sample_avg,
            "effective_sample_rate_hz": self.effective_sample_rate_hz,
            "pulse_width_us": self.pulse_width_us,
            "led_mode": self.led_mode,
            "led_brightness_hex": self.led_brightness_hex,
            "adc_range_na": self.adc_range_na,
        }

    @staticmethod
    def from_hardware_profile(profile: dict) -> "RegisterSettings":
        """Builds RegisterSettings from one entry of
        config/hardware_profiles.json's "profiles" map — the single source
        of truth for these register values (spec §2)."""
        return RegisterSettings(
            sample_rate_sps=profile["sample_rate_sps"],
            sample_avg=profile["sample_avg"],
            effective_sample_rate_hz=profile["effective_paired_sample_rate_hz"],
            pulse_width_us=profile["pulse_width_us"],
            led_mode=profile["led_mode"],
            led_brightness_hex=profile["led_brightness_hex"],
            adc_range_na=profile["adc_range_na"],
        )


def build_capture_record(
    window: AssembledWindow,
    *,
    capture_id: str,
    hardware_uid: str,
    short_code: str,
    register_settings: RegisterSettings,
    device_monotonic_timestamp_start_us: int,
    device_monotonic_timestamp_end_us: int,
    capture_start_utc: str,
    capture_end_utc: str,
) -> dict:
    """Builds one QA capture record dict (schema-shaped) from an already
    assembled window plus the acquisition/register metadata that
    window_assembler.py does not itself carry."""
    integ = window.integrity
    rate_hz = register_settings.effective_sample_rate_hz

    red_metrics = channel_metrics(window.red, rate_hz)
    ir_metrics = channel_metrics(window.ir, rate_hz)
    correlation = pearson_correlation([float(v) for v in window.red], [float(v) for v in window.ir])

    record = {
        "schema_version": SCHEMA_VERSION,
        "timestamp_semantics": "PACKET_CONSTRUCTION_NOT_ACQUISITION",
        "physical_sampling_verified": False,
        "record_type": RECORD_TYPE,
        "capture_id": capture_id,
        "device": {"hardware_uid": hardware_uid, "short_code": short_code},
        "session": {
            "sensor_session_id": window.sensor_session_id,
            "sensor_config_id": window.sensor_config_id,
            "sensor_config_name": _CONFIG_NAME_BY_ID.get(window.sensor_config_id, "UNVERIFIED"),
        },
        "register_settings": register_settings.to_dict(),
        "timing": {
            "device_monotonic_timestamp_start_us": device_monotonic_timestamp_start_us,
            "device_monotonic_timestamp_end_us": device_monotonic_timestamp_end_us,
            "capture_start_utc": capture_start_utc,
            "capture_end_utc": capture_end_utc,
            "window_duration_s": 30,
        },
        "samples": {
            "first_sample_index": window.first_sample_index,
            "last_sample_index": window.last_sample_index,
            "count": len(window.red),
            "red": list(window.red),
            "ir": list(window.ir),
        },
        "integrity": {
            "expected_samples": integ.expected_samples,
            "received_samples": integ.received_samples,
            "lost_samples": integ.lost_samples,
            "packet_loss_fraction": integ.loss_fraction,
            "duplicate_samples": integ.duplicate_samples,
            "out_of_order_packets": integ.out_of_order_packets,
            "fifo_overflow_count": integ.fifo_overflow_count,
            "software_overflow_count": integ.software_overflow_count,
            "acquisition_uncertainty": integ.acquisition_uncertainty,
            "loss_evidence_available": integ.loss_evidence_available,
            "session_changed": integ.session_changed,
            "config_changed": integ.config_changed,
        },
        "quality_metrics": {
            "red": red_metrics.to_dict(),
            "ir": ir_metrics.to_dict(),
            "red_ir_correlation": correlation,
        },
    }

    valid, violated = validate_capture_record(record)
    record["validation"] = {"valid": valid, "violated_rules": violated}
    return record


def validate_capture_record(record: dict) -> tuple[bool, list[str]]:
    """Applies every rule in spec §5 to a record dict (schema-shaped, minus
    the `validation` block itself — this function computes it). Does not
    itself run JSON Schema validation; callers combine this with
    jsonschema.validate() against schemas/qa-capture-record.schema.json to
    also catch SCHEMA_INVALID."""
    violated: list[str] = []

    samples = record["samples"]
    integ = record["integrity"]
    qm = record["quality_metrics"]

    red = samples["red"]
    ir = samples["ir"]

    if samples["count"] != len(red) or samples["count"] != len(ir) or len(red) != len(ir):
        violated.append("SAMPLE_COUNT_MISMATCH")

    if integ["received_samples"] != WINDOW_SAMPLE_COUNT:
        violated.append("INCOMPLETE_WINDOW")
    if integ["packet_loss_fraction"] > MAX_PACKET_LOSS_FRACTION:
        violated.append("PACKET_LOSS")
    if integ["duplicate_samples"] > 0:
        violated.append("DUPLICATE_SAMPLES")
    if integ["out_of_order_packets"] > 0:
        violated.append("OUT_OF_ORDER")
    if not integ.get("loss_evidence_available", False) or integ.get("acquisition_uncertainty", 1):
        violated.append("ACQUISITION_CONTINUITY_UNCERTAIN")
    if integ.get("software_overflow_count", 0):
        violated.append("SOFTWARE_ACQUISITION_OVERFLOW")
    if integ["fifo_overflow_count"] > 0:
        violated.append("FIFO_OVERFLOW")
    if integ["session_changed"] or integ["config_changed"]:
        violated.append("SESSION_OR_CONFIG_CHANGED")

    if qm["red"]["clipped_fraction"] > MAX_CLIPPED_FRACTION or qm["ir"]["clipped_fraction"] > MAX_CLIPPED_FRACTION:
        violated.append("CLIPPING")
    if qm["ir"]["dc"] < MIN_CONTACT_DC:
        violated.append("POOR_CONTACT")
    if qm["red_ir_correlation"] < MIN_RED_IR_CORRELATION:
        violated.append("LOW_RED_IR_CORRELATION")
    if qm["red"]["pulsatility"] < MIN_RED_PULSATILITY:
        violated.append("LOW_PULSATILITY")
    if is_flatline(red) or is_flatline(ir):
        violated.append("FLATLINE")
    if qm["red"]["psnr_db"] < MIN_PSNR_DB or qm["ir"]["psnr_db"] < MIN_PSNR_DB:
        violated.append("LOW_PSNR")
    if any(not is_finite_sample(v) for v in red) or any(not is_finite_sample(v) for v in ir):
        violated.append("NONFINITE_SAMPLE")

    return (len(violated) == 0, violated)
