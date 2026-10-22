"""Prototype continuous-measurement mode (opt-in, engineering prototype only).

Enabled ONLY when the environment variable VEINNET_PROTOTYPE_CONTINUOUS=1.
With the variable unset every existing production path is byte-for-byte
unchanged (the hooks in veinnet_edge_service.py, veinnet_sibling_adapter.py,
patient_care.py and stage10_z23_controller.py all check `enabled()` first).

What changes when enabled (decision record: output/prototype_continuous_20261020/README_HE.md):

BP (VeinNet)
  * Current window: the RED-channel gates (RED_SQI_CORRELATION, RED_SQI_PULSATILITY)
    and the 1-s development motion/contact gate no longer stop inference. RED is
    not part of the model tensor (max30102.py builds ppg/morphology from IR only).
    The window is predicted and the triggered conditions are returned as
    quality warnings next to the value.
  * Baseline (calibration) window stays strict on what poisons a whole session:
    the 1-s motion/contact gate must PASS. RED gates and high IR pulsatility are
    warnings here (no BP evidence supports rejecting on them: the Oct-08 A0
    baseline, IR pulsatility 0.00145, gave SBP MAE 5.8 mmHg).
  * Integrity gates (loss, duplicates, overflow, clipping, POOR_CONTACT,
    FLATLINE, model hash/shape) are unchanged.
HR
  * Always a value from the IR channel. HIGH confidence when MSPTD, Quotient and
    Elgendi (IR) agree within 5 BPM, a frequency-domain IR estimate (3-segment
    Welch or autocorrelation) confirms them within 10 BPM (guards against all three
    peak detectors double-counting the dicrotic wave), IR pulsatility <= 0.0008 and
    the 1-s motion gate passes -> median of the three. Otherwise the value is the
    median of six IR estimators, labelled:
      LOW      - signal clean (motion PASS, IR pulsatility <= 0.0008) but the
                 estimators disagree (e.g. time/frequency harmonic split);
                 replay: n=3, MAE 1.9 BPM.
      ARTIFACT - 1-s motion/contact gate FAIL or IR pulsatility > 0.0008;
                 replay: n=4, MAE 14.6 BPM. Still displayed (continuous, no HOLD);
                 label only, no threshold changed. HR only - not used for BP.
    No dependency on the raw-correlation sqi_gate.
SpO2 (committed secondary output; BP and HR are the primary outputs)
  * Always displayed. Ratio-of-ratios R from five 5-s blocks (5-30 s, 0.65-3.5 Hz
    RMS / block median DC). pct = 104 - 17*R (Maxim linear curve), labelled
    UNCALIBRATED_MAXIM_LINEAR, no clamp to a "normal" range. Values outside 0..100
    -> INVALID. The project has NO oximeter (none planned), so accuracy is
    NOT_EVALUABLE. The optional reference-anchored mode (pct = S_ref - 17*(R - R_ref))
    stays dormant unless a reference value is ever entered.
Calibration
  * Calibration survives a BLE reconnect (sensor_session_id change) when the
    hardware/config are unchanged; cuff association window 600 s; an optional
    repeat cuff reading is averaged with the first.
Stage-10 pre-flight
  * Deadline 15 s instead of 5 s; host-clock monotonicity jitter is logged, not fatal.

Thresholds 0.0008 and 5 BPM were chosen on the 16 retained cuff windows
(2026-10-20 audit) and must be confirmed prospectively.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import numpy as np
from scipy import signal

VERSION = "prototype-continuous-1.1.0"
ENV_FLAG = "VEINNET_PROTOTYPE_CONTINUOUS"
FS = 100.0

CONFIG = {
    "ir_pulsatility_max": 0.0008,          # HR high-confidence tier
    "hr_agree_bpm": 5.0,
    "hr_spectral_confirm_bpm": 10.0,
    "association_margin_s": 600.0,
    "preflight_deadline_s": 15.0,
    "cuff_repeat_max_diff_sbp": 10.0,
    "cuff_repeat_max_diff_dbp": 8.0,
    "dc_step_limit_fraction": 0.03,         # identical to veinnet_sibling_adapter
    "difference_rms_burst_ratio": 5.0,      # identical to veinnet_sibling_adapter
}

# Codes accepted by the frozen dashboard schema (sqi.reason_codes enum).
SCHEMA_WARNING_CODES = ("RED_SQI_CORRELATION", "RED_SQI_PULSATILITY", "EXCESSIVE_MOTION", "CONTACT_CHANGED")


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip() == "1"


# ---------------------------------------------------------------- signal helpers
def _bandpass(x, lo=0.5, hi=4.0, order=3):
    sos = signal.butter(order, [lo, hi], btype="bandpass", fs=FS, output="sos")
    return signal.sosfiltfilt(sos, signal.detrend(np.asarray(x, dtype=np.float64), type="linear"))


def motion_reason(red, ir):
    """Exact re-implementation of veinnet_sibling_adapter._development_motion_reason on arrays."""
    from statistics import median
    red, ir = list(red), list(ir)
    if len(red) != 3000 or len(ir) != 3000:
        return None
    for samples in (red, ir):
        blocks = [samples[s:s + 100] for s in range(0, 3000, 100)]
        dc = median(samples)
        block_dc = [median(b) for b in blocks]
        if dc > 0 and any(abs(r - l) / dc > CONFIG["dc_step_limit_fraction"] for l, r in zip(block_dc, block_dc[1:])):
            return "CONTACT_CHANGED"
        drms = [math.sqrt(sum((r - l) ** 2 for l, r in zip(b, b[1:])) / 99) for b in blocks]
        typical = median(drms)
        if typical > 0 and max(drms) / typical > CONFIG["difference_rms_burst_ratio"]:
            return "EXCESSIVE_MOTION"
    return None


def channel_quality(red, ir):
    """RED/IR quality metrics with the frozen VeinNet filter (detrend + Butterworth-4 0.5-8 Hz)."""
    red = np.asarray(red, dtype=np.float64); ir = np.asarray(ir, dtype=np.float64)
    sos = signal.butter(4, [0.5, 8.0], btype="bandpass", fs=FS, output="sos")
    r_ac = signal.sosfiltfilt(sos, signal.detrend(red, type="linear"), padtype="odd")
    i_ac = signal.sosfiltfilt(sos, signal.detrend(ir, type="linear"), padtype="odd")
    corr = float(np.corrcoef(r_ac, i_ac)[0, 1]) if np.std(r_ac) > 0 and np.std(i_ac) > 0 else float("nan")
    return {
        "red_ir_correlation": corr,
        "red_pulsatility": float(np.std(r_ac) / max(abs(float(np.median(red))), 1.0)),
        "ir_pulsatility": float(np.std(i_ac) / max(abs(float(np.median(ir))), 1.0)),
        "red_dc": float(np.median(red)),
        "ir_dc": float(np.median(ir)),
    }


# ---------------------------------------------------------------- HR estimators
# Same implementations that produced the hr_* columns of the 2026-10-20 audit.
def msptd_peaks(raw, refine_radius=5):
    x100 = _bandpass(raw, 0.5, 4.0, 3)
    x20 = x100[::5]
    n = len(x20)
    if n < 40:
        return np.array([], dtype=int)
    win_len = min(120, n); step = max(1, win_len // 2)
    votes = np.zeros(n, dtype=int)
    for start in range(0, max(1, n - win_len + 1), step):
        seg = x20[start:start + win_len]; L = len(seg)
        max_scale = min(max(2, int(round(20.0 / 0.5 / 2.0))), (L // 2) - 1)
        if max_scale < 2:
            continue
        M = np.zeros((max_scale, L), dtype=bool)
        for k in range(1, max_scale + 1):
            M[k - 1, k:L - k] = (seg[k:L - k] > seg[:L - 2 * k]) & (seg[k:L - k] > seg[2 * k:])
        lam = int(np.argmax(np.sum(M, axis=1))) + 1
        votes[start + np.where(np.all(M[:lam, :], axis=0))[0]] += 1
    cand = np.where(votes > 0)[0]
    if len(cand) == 0:
        return np.array([], dtype=int)
    refined = sorted({max(0, c * 5 - refine_radius) + int(np.argmax(x100[max(0, c * 5 - refine_radius):min(len(x100), c * 5 + refine_radius + 1)])) for c in cand})
    refined = np.array(refined, dtype=int)
    order = refined[np.argsort(-x100[refined], kind="stable")]
    kept = []
    for idx in order:
        if all(abs(int(idx) - k) >= 30 for k in kept):
            kept.append(int(idx))
    return np.array(sorted(kept), dtype=int)


def hr_from_peaks(peaks, total_sec=30.0):
    if len(peaks) < 2:
        return None
    ibis = np.diff(peaks) / FS
    kept = ibis[(ibis >= 0.38) & (ibis <= 1.45)]
    if len(kept) < 8:
        return None
    med = float(np.median(kept)); mad = float(np.median(np.abs(kept - med)))
    inl = kept[np.abs(kept - med) <= max(0.12, 3.5 * 1.4826 * mad)]
    if len(inl) < 6:
        inl = kept
    return round(60.0 / float(np.median(inl)), 1)


def quotient_hr(peaks):
    if len(peaks) < 2:
        return None
    ibis = np.diff(peaks) / FS
    ibis = ibis[(ibis >= 0.38) & (ibis <= 1.45)]
    if len(ibis) < 8:
        return None
    mask = np.zeros(len(ibis), dtype=bool)
    for _ in range(2):
        for i in range(len(ibis) - 1):
            if mask[i] or mask[i + 1]:
                continue
            ratio = ibis[i] / ibis[i + 1]
            if ratio < 0.80 or ratio > 1.20:
                mask[i] = True
    kept = ibis[~mask]
    return round(60.0 / float(np.median(kept)), 1) if len(kept) >= 8 else None


def elgendi_hr(raw):
    y = _bandpass(raw, 0.5, 8.0, 3)
    s = np.maximum(0.0, y) ** 2
    w1 = max(3, int(round(0.111 * FS))); w2 = max(11, int(round(0.667 * FS)))
    ma_peak = np.convolve(s, np.ones(w1) / w1, mode="same")
    ma_beat = np.convolve(s, np.ones(w2) / w2, mode="same")
    blocks = ma_peak > ma_beat + 0.02 * np.mean(s)
    edges = np.diff(blocks.astype(int))
    starts = np.where(edges == 1)[0] + 1; ends = np.where(edges == -1)[0] + 1
    if len(blocks) and blocks[0]:
        starts = np.r_[0, starts]
    if len(blocks) and blocks[-1]:
        ends = np.r_[ends, len(blocks)]
    peaks = [st + int(np.argmax(y[st:en])) for st, en in zip(starts, ends) if en - st >= w1 // 2]
    peaks = np.array(peaks, dtype=int)
    if len(peaks) > 1:
        order = peaks[np.argsort(-y[peaks], kind="stable")]
        kept = []
        for idx in order:
            if all(abs(int(idx) - k) >= 30 for k in kept):
                kept.append(int(idx))
        peaks = np.array(sorted(kept), dtype=int)
    return hr_from_peaks(peaks)


def welch_hr(raw, lo=0.65, hi=3.5):
    y = _bandpass(raw, lo, hi, 3)
    f, p = signal.welch(y, fs=FS, nperseg=1000, noverlap=500, nfft=4096, detrend=False)
    m = (f >= lo) & (f <= hi)
    return round(float(f[m][np.argmax(p[m])] * 60.0), 1) if np.any(m) and np.sum(p[m]) > 0 else None


def welch3seg_hr(raw, lo=0.95, hi=3.2):
    y = _bandpass(raw, 0.55, 3.8, 3); rates = []
    for seg in np.array_split(y, 3):
        f, p = signal.welch(seg, fs=FS, nperseg=len(seg), nfft=4096, detrend=False)
        m = (f >= lo) & (f <= hi)
        if np.any(m) and np.sum(p[m]) > 0:
            rates.append(float(f[m][np.argmax(p[m])] * 60.0))
    return round(float(np.median(rates)), 1) if rates else None


def autocorr_hr(raw, lo=0.85, hi=3.2):
    y = _bandpass(raw, 0.55, 3.8, 3)
    ac = signal.correlate(y, y, mode="full", method="fft")[len(y) - 1:]
    if ac[0] <= 0:
        return None
    lags = np.arange(int(math.ceil(FS / hi)), min(len(ac) - 1, int(math.floor(FS / lo))) + 1)
    scores = ac[lags] / ac[0]
    pks, _ = signal.find_peaks(scores, distance=10)
    best = pks[int(np.argmax(scores[pks]))] if len(pks) else int(np.argmax(scores))
    return round(60.0 * FS / int(lags[best]), 1)


@dataclass
class HrResult:
    bpm: float | None
    tier: str                     # HIGH | LOW | ARTIFACT | NONE
    reason: str
    components: dict = field(default_factory=dict)
    algorithm_name: str = "prototype_tiered_ir_hr"
    algorithm_version: str = VERSION


def tiered_hr(red, ir, *, motion=None, ir_pulsatility=None) -> HrResult:
    ir = np.asarray(ir, dtype=np.float64)
    if len(ir) < 1000 or not np.all(np.isfinite(ir)) or np.ptp(ir) < 1:
        return HrResult(None, "NONE", "PROTOTYPE_NO_SIGNAL")
    pk = msptd_peaks(ir)
    comp = {
        "msptd_ir": hr_from_peaks(pk), "quotient_ir": quotient_hr(pk), "elgendi_ir": elgendi_hr(ir),
        "welch_ir": welch_hr(ir), "welch3seg_ir": welch3seg_hr(ir), "autocorr_ir": autocorr_hr(ir),
    }
    if motion is None:
        motion = motion_reason(red, ir) if red is not None else None
    if ir_pulsatility is None and red is not None:
        ir_pulsatility = channel_quality(red, ir)["ir_pulsatility"]
    td = [comp[k] for k in ("msptd_ir", "quotient_ir", "elgendi_ir") if comp[k] is not None]
    agree = len(td) == 3 and (max(td) - min(td)) <= CONFIG["hr_agree_bpm"]
    if agree:
        m = float(np.median(td))
        spectral = [comp[k] for k in ("welch3seg_ir", "autocorr_ir") if comp[k] is not None]
        confirmed = any(abs(v - m) <= CONFIG["hr_spectral_confirm_bpm"] for v in spectral)
    else:
        confirmed = False
    clean = motion is None and ir_pulsatility is not None and ir_pulsatility <= CONFIG["ir_pulsatility_max"]
    if agree and confirmed and clean:
        return HrResult(round(float(np.median(td)), 1), "HIGH", "PROTOTYPE_HIGH_CONFIDENCE", comp)
    allv = [v for v in comp.values() if v is not None]
    if not allv:
        return HrResult(None, "NONE", "PROTOTYPE_NO_ESTIMATE", comp)
    why = [] if agree else ["IR_METHODS_DISAGREE"]
    if agree and not confirmed:
        why.append("NO_SPECTRAL_CONFIRMATION")
    if motion is not None:
        why.append(motion)
    if ir_pulsatility is not None and ir_pulsatility > CONFIG["ir_pulsatility_max"]:
        why.append("IR_PULSATILITY_HIGH")
    comp["low_confidence_because"] = why
    artifact = motion is not None or (ir_pulsatility is not None and ir_pulsatility > CONFIG["ir_pulsatility_max"])
    if artifact:
        return HrResult(round(float(np.median(allv)), 1), "ARTIFACT", "PROTOTYPE_ARTIFACT", comp)
    return HrResult(round(float(np.median(allv)), 1), "LOW", "PROTOTYPE_LOW_CONFIDENCE", comp)


# ---------------------------------------------------------------- SpO2
@dataclass
class Spo2Result:
    pct: float | None
    status: str                   # VALID | INVALID
    r: float | None
    mode: str                     # UNCALIBRATED_MAXIM_LINEAR | REFERENCE_ANCHORED
    reason: str | None = None
    algorithm_name: str = "prototype_ratio_of_ratios_spo2"
    algorithm_version: str = VERSION


def ratio_of_ratios(red, ir):
    r = np.asarray(red, dtype=np.float64); i = np.asarray(ir, dtype=np.float64)
    if len(r) < 3000 or np.median(r) < 1000 or np.median(i) < 1000:
        return None
    rb = _bandpass(r, 0.65, 3.5, 3); ib = _bandpass(i, 0.65, 3.5, 3); vals = []
    for s in range(500, 3000, 500):
        acr = float(np.sqrt(np.mean(rb[s:s + 500] ** 2))); aci = float(np.sqrt(np.mean(ib[s:s + 500] ** 2)))
        dcr = float(np.median(r[s:s + 500])); dci = float(np.median(i[s:s + 500]))
        if dcr > 1000 and dci > 1000 and aci > 0:
            vals.append((acr / dcr) / (aci / dci))
    return float(np.median(vals)) if vals else None


def spo2(red, ir, *, reference=None) -> Spo2Result:
    """reference: optional {"spo2_pct": S_ref, "r": R_ref} recorded at calibration."""
    R = ratio_of_ratios(red, ir)
    if R is None or not math.isfinite(R):
        return Spo2Result(None, "INVALID", None, "NONE", "NO_RATIO")
    if reference and reference.get("spo2_pct") is not None and reference.get("r") is not None:
        pct, mode = float(reference["spo2_pct"]) - 17.0 * (R - float(reference["r"])), "REFERENCE_ANCHORED"
    else:
        pct, mode = 104.0 - 17.0 * R, "UNCALIBRATED_MAXIM_LINEAR"
    if not (0.0 <= pct <= 100.0):
        return Spo2Result(None, "INVALID", round(R, 4), mode, "OUT_OF_PHYSICAL_RANGE")
    return Spo2Result(round(pct, 1), "VALID", round(R, 4), mode)


# ---------------------------------------------------------------- BP preprocessing policy
def relaxed_spec(spec: dict) -> dict:
    s = dict(spec)
    s["min_red_ir_correlation"] = -2.0   # RED gates -> warnings (RED is not a model input)
    s["min_red_pulsatility"] = 0.0
    return s


def bp_warnings(red, ir, spec: dict, motion=None):
    q = channel_quality(red, ir)
    w = []
    if not math.isfinite(q["red_ir_correlation"]) or q["red_ir_correlation"] < spec["min_red_ir_correlation"]:
        w.append("RED_SQI_CORRELATION")
    if q["red_pulsatility"] < spec["min_red_pulsatility"]:
        w.append("RED_SQI_PULSATILITY")
    if motion is not None:
        w.append(motion)
    return w, q


def baseline_rejection(red, ir, motion=None):
    """Strict check kept for the calibration baseline (1-s motion/contact gate).
    Returns (reason_code|None, internal_reason|None)."""
    if motion is not None:
        return motion, "BASELINE_" + motion
    return None, None


def average_cuff(sbp, dbp, repeat_sbp=None, repeat_dbp=None):
    if repeat_sbp is None or repeat_dbp is None:
        return float(sbp), float(dbp), None
    flag = None
    if abs(sbp - repeat_sbp) > CONFIG["cuff_repeat_max_diff_sbp"] or abs(dbp - repeat_dbp) > CONFIG["cuff_repeat_max_diff_dbp"]:
        flag = "CUFF_REPEAT_DISAGREE"
    return (float(sbp) + float(repeat_sbp)) / 2.0, (float(dbp) + float(repeat_dbp)) / 2.0, flag
