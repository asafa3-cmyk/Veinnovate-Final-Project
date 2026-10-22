"""Frozen MAX30102 signal-quality gating and PPG/morphology extraction.

assemble_packets() and preprocess() are extracted from
gate7_max30102_personaldelta/max30102.py with unchanged function bodies.
The versioned contract sets the active gate limits, including the POC
packet-loss and clipping headroom. See ../../contracts/preprocessing_contract.json
for provenance and the preserved dead-variable quirk.

Excluded from extraction: CalibrationCache and MAX30102Runtime, which are
PyTorch-checkpoint-based (torch.load) and not part of this ONNX runtime path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal

from .morphology import compute_morphology_reading

_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "preprocessing_contract.json"


def load_max30102_spec() -> dict[str, Any]:
    payload = json.loads(_CONTRACT_PATH.read_text())
    return payload["max30102"]


@dataclass(frozen=True)
class BLEPacket:
    red: np.ndarray
    ir: np.ndarray
    first_sample_index: int
    timestamp_seconds: float
    sample_rate_hz: float
    led_current_red: int
    led_current_ir: int
    adc_range: int
    saturation: bool = False
    packet_loss_counter: int = 0
    contact_changed: bool = False
    motion_detected: bool = False


def assemble_packets(packets: list[BLEPacket], spec: dict | None = None) -> dict[str, Any]:
    spec = spec or load_max30102_spec()
    if not packets:
        return {"accepted": False, "reason": "NO_PACKETS"}
    ordered = sorted(packets, key=lambda item: item.first_sample_index)
    fs = float(ordered[0].sample_rate_hz)
    if any(float(packet.sample_rate_hz) != fs for packet in ordered):
        return {"accepted": False, "reason": "SAMPLE_RATE_CHANGED"}
    red, ir, missing = [], [], 0
    expected = ordered[0].first_sample_index
    for packet in ordered:
        if len(packet.red) != len(packet.ir) or len(packet.red) == 0:
            return {"accepted": False, "reason": "MALFORMED_PACKET"}
        if packet.first_sample_index < expected:
            return {"accepted": False, "reason": "DUPLICATE_OR_REORDERED_SAMPLE"}
        missing += packet.first_sample_index - expected
        expected = packet.first_sample_index + len(packet.red)
        red.append(np.asarray(packet.red, dtype=np.float64))
        ir.append(np.asarray(packet.ir, dtype=np.float64))
    red_array, ir_array = np.concatenate(red), np.concatenate(ir)
    reported_loss = max(packet.packet_loss_counter for packet in ordered)
    loss_fraction = max(missing, reported_loss) / max(1, len(ir_array) + missing)
    if loss_fraction > spec["max_packet_loss_fraction"]:
        return {"accepted": False, "reason": "PACKET_LOSS", "loss_fraction": loss_fraction}
    if any(packet.saturation for packet in ordered):
        return {"accepted": False, "reason": "SATURATION_FLAG"}
    adc_max = float(ordered[0].adc_range)
    clipped_fraction = float(
        np.mean((red_array <= 0) | (ir_array <= 0) | (red_array >= adc_max) | (ir_array >= adc_max))
    )
    if clipped_fraction > spec["max_clipped_fraction"]:
        return {"accepted": False, "reason": "CLIPPING", "clipped_fraction": clipped_fraction}
    if any(packet.contact_changed for packet in ordered):
        return {"accepted": False, "reason": "CONTACT_CHANGED"}
    if any(packet.motion_detected for packet in ordered):
        return {"accepted": False, "reason": "EXCESSIVE_MOTION"}
    return {
        "accepted": True, "red": red_array, "ir": ir_array, "sample_rate_hz": fs,
        "timestamp_seconds": ordered[0].timestamp_seconds, "loss_fraction": loss_fraction,
        "acquisition": {
            "led_current_red": ordered[0].led_current_red,
            "led_current_ir": ordered[0].led_current_ir,
            "adc_range": ordered[0].adc_range,
        },
    }


def preprocess(red: np.ndarray, ir: np.ndarray, sample_rate_hz: float, spec: dict | None = None) -> dict[str, Any]:
    spec = spec or load_max30102_spec()
    red, ir = np.asarray(red, dtype=np.float64), np.asarray(ir, dtype=np.float64)
    expected = int(round(sample_rate_hz * spec["window_seconds"]))
    if red.shape != ir.shape or red.ndim != 1 or len(red) != expected:
        return {"accepted": False, "reason": "WINDOW_LENGTH"}
    if not np.isfinite(red).all() or not np.isfinite(ir).all():
        return {"accepted": False, "reason": "NONFINITE"}
    if min(float(np.median(red)), float(np.median(ir))) < spec["min_contact_dc"]:
        return {"accepted": False, "reason": "POOR_CONTACT"}
    # NOTE: 'clipped' is intentionally never updated below -- this preserves a
    # known quirk in the frozen source (see preprocessing_contract.json
    # "known_quirk_not_fixed"). sqi.clipped_fraction is therefore always 0.0
    # here; the real clipping check already ran in assemble_packets().
    clipped = 0.0
    low, high = spec["bandpass_hz"]
    sos = signal.butter(4, [low, high], btype="bandpass", fs=sample_rate_hz, output="sos")
    red_ac = signal.sosfiltfilt(sos, signal.detrend(red, type="linear"), padtype="odd")
    ir_ac = signal.sosfiltfilt(sos, signal.detrend(ir, type="linear"), padtype="odd")
    correlation = float(np.corrcoef(red_ac, ir_ac)[0, 1])
    pulsatility = float(np.std(red_ac) / max(abs(float(np.median(red))), 1.0))
    if not np.isfinite(correlation) or correlation < spec["min_red_ir_correlation"]:
        return {"accepted": False, "reason": "RED_SQI_CORRELATION", "red_ir_correlation": correlation}
    if pulsatility < spec["min_red_pulsatility"]:
        return {"accepted": False, "reason": "RED_SQI_PULSATILITY", "red_pulsatility": pulsatility}
    target_hz = int(spec["target_hz"])
    if sample_rate_hz != target_hz:
        ratio = Fraction(target_hz / sample_rate_hz).limit_denominator(1000)
        ir_ac = signal.resample_poly(ir_ac, ratio.numerator, ratio.denominator, window=("kaiser", 5.0), padtype="line")
    if len(ir_ac) != target_hz * spec["window_seconds"]:
        return {"accepted": False, "reason": "RESAMPLE_LENGTH"}
    median = float(np.median(ir_ac))
    scale = float(1.4826 * np.median(np.abs(ir_ac - median)))
    if not np.isfinite(scale) or scale < 1e-6:
        return {"accepted": False, "reason": "FLATLINE"}
    ppg = ((ir_ac - median) / scale).astype(np.float32).reshape(3, 1000)
    morphology = compute_morphology_reading(ppg.astype(np.float64)).astype(np.float32)
    return {
        "accepted": True, "ppg": ppg, "morphology": morphology,
        "sqi": {
            "red_ir_correlation": correlation,
            "red_pulsatility": pulsatility,
            "clipped_fraction": float(clipped),
        },
    }
