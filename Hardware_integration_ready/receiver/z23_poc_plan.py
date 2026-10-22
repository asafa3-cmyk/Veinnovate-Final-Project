"""Strict loader for the owner-authorized Stage 9 Z23 POC experiment plan."""

from __future__ import annotations

import json
from pathlib import Path


PLAN_PATH = Path(__file__).resolve().parent.parent / "config" / "stage9_z23_poc_experiment_v1.json"


class Z23PocPlanError(ValueError):
    pass


def load_z23_poc_plan(path: Path = PLAN_PATH) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    profiles = plan.get("profiles")
    observed = [
        (
            row.get("attempt"),
            row.get("profile_id"),
            row.get("config_id"),
            row.get("red_pa"),
            row.get("ir_pa"),
            row.get("red_current_ma_nominal"),
            row.get("ir_current_ma_nominal"),
        )
        for row in profiles or []
    ]
    expected = [
        ("A", "Z23-POC-R1F-I1F", 24, "0x1F", "0x1F", 6.2, 6.2),
        ("B", "Z23-POC-R1F-I3E", 25, "0x1F", "0x3E", 6.2, 12.4),
        ("C", "Z23-POC-R1F-I5E", 26, "0x1F", "0x5E", 6.2, 18.8),
    ]
    if (
        plan.get("schema_version") != "1.0.0"
        or plan.get("record_type") != "stage9_z23_poc_experiment_plan"
        or plan.get("batch_type") != "Z23_POC_IR_COMPARISON"
        or plan.get("experiment_id") != "STAGE9-Z23-POC-LED-COMPARISON-001"
        or plan.get("scope") != "Z23_ENGINEERING_POC_ONLY"
        or plan.get("engineering_only") is not True
        or plan.get("hardware_uid") != "A90AFDAF672D66F1"
        or plan.get("short_code") != "VN-001"
        or plan.get("zone") != "Z23"
        or plan.get("attempt_budget") != 3
        or plan.get("automatic_replacement_attempts") != 0
        or plan.get("historical_attempts_immutable") is not True
        or observed != expected
        or plan.get("locked_settings")
        != {
            "adc_range_na": 16384,
            "sample_rate_sps": 400,
            "sample_avg": 4,
            "effective_sample_rate_hz": 100,
            "pulse_width_us": 411,
            "led_mode": 2,
            "measurement_pairs": 3000,
            "measurement_duration_s": 30,
        }
        or plan.get("calibration", {}).get("same_profile_required") is not True
        or plan.get("calibration", {}).get("cross_profile_extrapolation_forbidden") is not True
        or plan.get("calibration", {}).get("min_contact_dc_multiplier") != 3.0
        or plan.get("calibration", {}).get("dc_shift_threshold_pct") != 8.0
        or plan.get("acquisition", {}).get("preflight_and_window_same_session") is not True
        or plan.get("acquisition", {}).get("restart_forbidden") is not True
        or plan.get("acquisition", {}).get("exposure_change_during_measurement_forbidden") is not True
        or plan.get("predeclared_comparison", {}).get("threshold_tuning_after_capture_forbidden") is not True
        or plan.get("historical_review_gate_unchanged") is not True
        or plan.get("legacy_stage10_authorization_unchanged") is not True
        or plan.get("stage10_may_begin_only_after") != "Z23_POC_POLICY_READY"
    ):
        raise Z23PocPlanError("invalid Stage 9 Z23 POC experiment plan")
    return plan
