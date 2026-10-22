"""Fail-closed Stage 9 OAAT plan and batch-ledger helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from qa_capture_builder import RegisterSettings


EXPECTED_CONFIGS = (
    (16, "0x1F", 6.2, 16384),
    (17, "0x3E", 12.4, 16384),
    (18, "0x5E", 18.8, 16384),
    (19, "0x7F", 25.4, 16384),
    (20, "0x9F", 31.8, 16384),
    (21, "0xBE", 38.0, 16384),
    (22, "0x7F", 25.4, 4096),
    (23, "0x7F", 25.4, 8192),
)

STAGE9B_CONFIGS = {
    16: ("0x1F", 6.2, 16384),
    17: ("0x3E", 12.4, 16384),
    18: ("0x5E", 18.8, 16384),
}
STAGE9B_ZONES = {"Z23", "Z34", "Z45"}

METRIC_RECORDING_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "stage9_metric_recording_contract_v2.json"
)


class Stage9PlanError(ValueError):
    pass


@dataclass(frozen=True)
class CaptureSlot:
    config_id: int
    repetition: int
    settings: RegisterSettings
    zone: str | None = None
    attempt_ordinal: int | None = None
    profile_id: str | None = None
    red_pa_hex: str | None = None
    ir_pa_hex: str | None = None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_metric_recording_contract(path: Path = METRIC_RECORDING_CONTRACT_PATH) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_metric = {
        "metric_version": "2.0.0",
        "effective_sample_rate_hz": 100,
        "window_samples": 3000,
        "detrending": "least_squares_linear",
        "spectral_window": "hann_symmetric",
        "cardiac_band_hz": [0.7, 4.0],
        "cardiac_band_edges": "inclusive_dft_bins",
        "signal_power": "sum_squared_dft_magnitude_in_cardiac_band",
        "noise_power": "sum_squared_dft_magnitude_in_all_other_positive_frequency_bins",
        "red_ir_agreement": "minimum_normalized_real_cross_power_across_overlapping_cardiac_band_sub_blocks",
        "zero_power_clamp_db": 120.0,
    }
    expected_thresholds = {
        "minimum_cardiac_snr_db": None,
        "minimum_cardiac_red_ir_correlation": None,
        "selection_status": "NOT_SELECTED",
        "selection_constraint": "Must not be selected from the seven retained Stage 9 v1 accepted windows.",
    }
    expected_recording = {
        "diagnostic_schema_version": "2.0.0",
        "retain_raw_red_ir_for_every_authorized_attempt": True,
        "retain_zero_length_raw_file_when_no_samples_arrive": True,
        "raw_filename": "raw-red-ir.jsonl",
        "atomic_attempt_directory": True,
        "checksum_every_file": True,
        "historical_attempts_immutable": True,
    }
    expected_review_gate = {
        "required_verdict": "ACCEPT_V2.2",
        "requires_new_version_for_thresholds_or_scope_expansion": True,
        "stage9b_authorized": True,
        "stage9b_completion_authorized": False,
        "stage10_authorized": False,
    }
    expected_authorization = {
        "scope": "STAGE9B_EXPOSURE_CHARACTERIZATION_ONLY",
        "experiment_id": "STAGE9B-VN001-EXPOSURE-001",
        "plan_path": "config/stage9b_exposure_batch_v2_2.json",
        "plan_sha256": "cb3624059972b1b524610c79cc6d615f6d2e26edde43122eb4339f02b19fb17e",
        "attempt_limit": 18,
        "allowed_zones": ["Z23", "Z34", "Z45"],
        "allowed_config_ids": [16, 17, 18],
        "required_adc_range_na": 16384,
        "quality_decision": "RECORD_ONLY_NO_NUMERIC_V1_OR_V2_QUALITY_GATE",
        "retain_every_attempt": True,
    }
    if (
        payload.get("schema_version") != "2.2.0"
        or payload.get("record_type") != "stage9_metric_recording_contract"
        or payload.get("status") != "STAGE9B_EXPOSURE_BATCH_AUTHORIZED_THRESHOLDS_PENDING"
        or payload.get("physical_capture_authorized") is not True
        or payload.get("metric") != expected_metric
        or payload.get("thresholds") != expected_thresholds
        or payload.get("recording") != expected_recording
        or payload.get("authorization") != expected_authorization
        or payload.get("review_gate") != expected_review_gate
    ):
        raise Stage9PlanError("invalid Stage 9 metric/recording contract v2")
    return payload


def require_metric_recording_contract_reviewed(
    *, plan_path: Path, plan: dict, path: Path = METRIC_RECORDING_CONTRACT_PATH
) -> dict:
    contract = load_metric_recording_contract(path)
    authorization = contract["authorization"]
    expected_plan_path = METRIC_RECORDING_CONTRACT_PATH.parent.parent / authorization["plan_path"]
    if (
        plan_path.resolve() != expected_plan_path.resolve()
        or sha256_file(plan_path) != authorization["plan_sha256"]
        or plan.get("experiment_id") != authorization["experiment_id"]
        or plan.get("batch_type") != "EXPOSURE_CHARACTERIZATION_ONLY"
        or len(plan.get("attempts", [])) != authorization["attempt_limit"]
    ):
        raise Stage9PlanError("contract v2.2 does not authorize this Stage 9 plan")
    return contract


def load_plan(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("record_type") == "stage9_z23_poc_experiment_plan":
        from z23_poc_plan import load_z23_poc_plan

        return load_z23_poc_plan(path)
    if payload.get("schema_version") == "2.2.0":
        return _load_stage9b_exposure_plan(payload)
    if payload.get("schema_version") != "1.0.0" or payload.get("engineering_only") is not True:
        raise Stage9PlanError("unsupported or non-engineering Stage 9 plan")
    if payload.get("hardware_uid") != "A90AFDAF672D66F1" or payload.get("short_code") != "VN-001":
        raise Stage9PlanError("Stage 9 plan identity is not the verified VN-001 identity")
    if payload.get("repetitions_per_configuration") != 3 or payload.get("capture_duration_s") != 30:
        raise Stage9PlanError("each configuration must have exactly three 30-second captures")
    locked = payload.get("locked_settings")
    if locked != {
        "sample_rate_sps": 400,
        "sample_avg": 4,
        "effective_sample_rate_hz": 100,
        "pulse_width_us": 411,
        "led_mode": 2,
    }:
        raise Stage9PlanError("locked acquisition settings changed")
    observed = tuple(
        (row.get("config_id"), row.get("led_pa_hex"), row.get("led_current_ma_approx"), row.get("adc_range_na"))
        for row in payload.get("configurations", [])
    )
    if observed != EXPECTED_CONFIGS:
        raise Stage9PlanError("OAAT exposure configuration order/content changed")
    if not payload.get("placement", {}).get("contact_pressure") or len(payload.get("stop_conditions", [])) < 5:
        raise Stage9PlanError("physical placement or stop rules are incomplete")
    return payload


def _load_stage9b_exposure_plan(payload: dict) -> dict:
    if (
        payload.get("batch_type") != "EXPOSURE_CHARACTERIZATION_ONLY"
        or payload.get("engineering_only") is not True
        or payload.get("experiment_id") != "STAGE9B-VN001-EXPOSURE-001"
        or payload.get("hardware_uid") != "A90AFDAF672D66F1"
        or payload.get("short_code") != "VN-001"
        or payload.get("capture_duration_s") != 30
        or payload.get("locked_settings") != {
            "sample_rate_sps": 400,
            "sample_avg": 4,
            "effective_sample_rate_hz": 100,
            "pulse_width_us": 411,
            "led_mode": 2,
        }
        or payload.get("quality_decision") != {
            "mode": "RECORD_ONLY_NO_NUMERIC_QUALITY_GATE",
            "v1_metrics_may_accept_or_reject": False,
            "v2_metrics_may_accept_or_reject": False,
        }
        or len(payload.get("stop_conditions", [])) < 5
    ):
        raise Stage9PlanError("invalid Stage 9B exposure-characterization plan")
    attempts = payload.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 18:
        raise Stage9PlanError("Stage 9B exposure batch requires exactly 18 attempts")
    counts = {(zone, config_id): 0 for zone in STAGE9B_ZONES for config_id in STAGE9B_CONFIGS}
    for ordinal, attempt in enumerate(attempts, start=1):
        zone = attempt.get("zone")
        config_id = attempt.get("config_id")
        expected_config = STAGE9B_CONFIGS.get(config_id)
        if (
            attempt.get("attempt_ordinal") != ordinal
            or zone not in STAGE9B_ZONES
            or attempt.get("repetition") not in {1, 2}
            or expected_config is None
            or (
                attempt.get("led_pa_hex"),
                attempt.get("led_current_ma_approx"),
                attempt.get("adc_range_na"),
            ) != expected_config
        ):
            raise Stage9PlanError("invalid or missing immutable Stage 9B attempt metadata")
        counts[(zone, config_id)] += 1
    if set(counts.values()) != {2}:
        raise Stage9PlanError("Stage 9B plan must contain two attempts per zone/configuration")
    return payload


def load_informational_baseline_disposition(path: Path, *, plan_path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    plan = load_plan(plan_path)
    if (
        payload.get("schema_version") != "1.0.0"
        or payload.get("record_type") != "stage9_informational_baseline_disposition"
        or payload.get("experiment_id") != plan["experiment_id"]
        or payload.get("hardware_uid") != plan["hardware_uid"]
        or payload.get("short_code") != plan["short_code"]
        or payload.get("base_plan_sha256") != sha256_file(plan_path)
        or payload.get("config_id") != 16
        or payload.get("planned_slots_disposed") != 3
        or payload.get("disposition") != "INFORMATIONAL_LOW_PSNR_BASELINE_NOT_ACCEPTED"
        or payload.get("next_config_id") != 17
    ):
        raise Stage9PlanError("invalid config-16 informational baseline disposition")
    attempts = payload.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 3:
        raise Stage9PlanError("informational baseline requires exactly three attempts")
    seen = set()
    for sequence, attempt in enumerate(attempts, start=1):
        attempt_id = attempt.get("attempt_id")
        digest = attempt.get("diagnostic_sha256")
        if (
            attempt.get("baseline_sequence") != sequence
            or attempt.get("source_slot_repetition") != 1
            or attempt.get("reason") != "LOW_PSNR"
            or not isinstance(attempt_id, str)
            or re.fullmatch(r"transport-VN-001-c16-r01-[A-Za-z0-9-]+", attempt_id) is None
            or attempt_id in seen
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise Stage9PlanError("malformed informational baseline attempt")
        seen.add(attempt_id)
    return payload


def planned_slots(plan: dict) -> list[CaptureSlot]:
    locked = plan["locked_settings"]
    if plan.get("batch_type") == "Z23_POC_IR_COMPARISON":
        return [
            CaptureSlot(
                row["config_id"],
                1,
                RegisterSettings(
                    sample_rate_sps=locked["sample_rate_sps"],
                    sample_avg=locked["sample_avg"],
                    effective_sample_rate_hz=locked["effective_sample_rate_hz"],
                    pulse_width_us=locked["pulse_width_us"],
                    led_mode=locked["led_mode"],
                    led_brightness_hex=row["red_pa"],
                    adc_range_na=locked["adc_range_na"],
                ),
                zone="Z23",
                attempt_ordinal=index,
                profile_id=row["profile_id"],
                red_pa_hex=row["red_pa"],
                ir_pa_hex=row["ir_pa"],
            )
            for index, row in enumerate(plan["profiles"], start=1)
        ]
    if plan.get("batch_type") == "EXPOSURE_CHARACTERIZATION_ONLY":
        return [
            CaptureSlot(
                row["config_id"],
                row["repetition"],
                RegisterSettings(
                    sample_rate_sps=locked["sample_rate_sps"],
                    sample_avg=locked["sample_avg"],
                    effective_sample_rate_hz=locked["effective_sample_rate_hz"],
                    pulse_width_us=locked["pulse_width_us"],
                    led_mode=locked["led_mode"],
                    led_brightness_hex=row["led_pa_hex"],
                    adc_range_na=row["adc_range_na"],
                ),
                zone=row["zone"],
                attempt_ordinal=row["attempt_ordinal"],
            )
            for row in plan["attempts"]
        ]
    slots = []
    for row in plan["configurations"]:
        settings = RegisterSettings(
            sample_rate_sps=locked["sample_rate_sps"],
            sample_avg=locked["sample_avg"],
            effective_sample_rate_hz=locked["effective_sample_rate_hz"],
            pulse_width_us=locked["pulse_width_us"],
            led_mode=locked["led_mode"],
            led_brightness_hex=row["led_pa_hex"],
            adc_range_na=row["adc_range_na"],
        )
        for repetition in range(1, plan["repetitions_per_configuration"] + 1):
            slots.append(CaptureSlot(row["config_id"], repetition, settings))
    return slots


def next_slot(plan: dict, ledger: dict | None, *, skipped_slots: int = 0) -> CaptureSlot | None:
    slots = planned_slots(plan)
    if plan.get("batch_type") == "Z23_POC_IR_COMPARISON":
        if skipped_slots != 0:
            raise Stage9PlanError("Z23 POC plan does not accept skipped slots")
        completed = [] if ledger is None else ledger.get("attempts", [])
        if len(completed) > len(slots):
            raise Stage9PlanError("Z23 POC ledger exceeds the fixed attempt budget")
        for index, row in enumerate(completed):
            expected = slots[index]
            if (
                row.get("attempt_ordinal"),
                row.get("profile_id"),
                row.get("config_id"),
                row.get("red_pa"),
                row.get("ir_pa"),
            ) != (
                expected.attempt_ordinal,
                expected.profile_id,
                expected.config_id,
                expected.red_pa_hex,
                expected.ir_pa_hex,
            ):
                raise Stage9PlanError("Z23 POC ledger order/profile does not match the frozen plan")
        return slots[len(completed)] if len(completed) < len(slots) else None
    if plan.get("batch_type") == "EXPOSURE_CHARACTERIZATION_ONLY":
        if skipped_slots != 0:
            raise Stage9PlanError("Stage 9B does not accept historical slot dispositions")
        completed = [] if ledger is None else ledger.get("attempts", [])
        if len(completed) > len(slots):
            raise Stage9PlanError("batch ledger contains too many attempts")
        for index, row in enumerate(completed):
            expected = slots[index]
            if (
                row.get("attempt_ordinal"),
                row.get("zone"),
                row.get("config_id"),
                row.get("repetition"),
            ) != (
                expected.attempt_ordinal,
                expected.zone,
                expected.config_id,
                expected.repetition,
            ):
                raise Stage9PlanError("batch ledger order does not match the authorized plan")
        return slots[len(completed)] if len(completed) < len(slots) else None
    if skipped_slots not in {0, 3} or (skipped_slots == 3 and any(slot.config_id != 16 for slot in slots[:3])):
        raise Stage9PlanError("invalid informational baseline slot disposition")
    slots = slots[skipped_slots:]
    completed = [] if ledger is None else ledger.get("captures", [])
    if len(completed) > len(slots):
        raise Stage9PlanError("batch ledger contains too many captures")
    for index, row in enumerate(completed):
        expected = slots[index]
        if (row.get("config_id"), row.get("repetition")) != (expected.config_id, expected.repetition):
            raise Stage9PlanError("batch ledger order does not match the frozen plan")
    return slots[len(completed)] if len(completed) < len(slots) else None


def read_ledger(batch_root: Path) -> dict | None:
    ledger_path = batch_root / "stage9-batch.json"
    checksum_path = batch_root / "stage9-batch.sha256"
    if not ledger_path.exists() and not checksum_path.exists():
        return None
    if not ledger_path.is_file() or not checksum_path.is_file():
        raise Stage9PlanError("batch ledger is incomplete")
    data = ledger_path.read_bytes()
    fields = checksum_path.read_text(encoding="ascii").strip().split()
    if fields != [hashlib.sha256(data).hexdigest(), "stage9-batch.json"]:
        raise Stage9PlanError("batch ledger checksum mismatch")
    return json.loads(data)


def write_ledger(batch_root: Path, ledger: dict) -> None:
    batch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    for name, content in (
        (".stage9-batch.json.tmp", data),
        (".stage9-batch.sha256.tmp", f"{digest}  stage9-batch.json\n".encode("ascii")),
    ):
        fd = os.open(batch_root / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    os.replace(batch_root / ".stage9-batch.json.tmp", batch_root / "stage9-batch.json")
    os.replace(batch_root / ".stage9-batch.sha256.tmp", batch_root / "stage9-batch.sha256")
    fd = os.open(batch_root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
