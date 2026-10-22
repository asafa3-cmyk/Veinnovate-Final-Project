"""capture_quality_metrics.py — pure-Python (no NumPy/SciPy) derived
signal-quality metrics for Stage 7 QA capture records (spec:
docs/QA_CAPTURE_FORMAT_SPEC.md §4).

These are bench-review metrics for the QA capture format only. They are
deliberately not wired into receiver/sqi_gate.py's clinical accept/reject
path — that remains the sole authority for whether a window may reach the
model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ADC_18BIT_MAX = (1 << 18) - 1

# pSNR is clamped to keep the metric finite and JSON-representable even for
# degenerate (perfectly flat or all-noise) windows; see spec §4.
PSNR_CLAMP_DB = 120.0

# Versioned Stage 9A correction. These are definition constants, not
# acceptance thresholds. Threshold selection remains blocked pending the
# independent review required by config/stage9_metric_recording_contract_v2.json.
CARDIAC_METRIC_VERSION = "2.0.0"
CARDIAC_BAND_LOW_HZ = 0.7
CARDIAC_BAND_HIGH_HZ = 4.0


@dataclass(frozen=True)
class ChannelMetrics:
    dc: float
    ac_rms: float
    pulsatility: float
    clipped_fraction: float
    psnr_db: float

    def to_dict(self) -> dict:
        return {
            "dc": self.dc,
            "ac_rms": self.ac_rms,
            "pulsatility": self.pulsatility,
            "clipped_fraction": self.clipped_fraction,
            "psnr_db": self.psnr_db,
        }


@dataclass(frozen=True)
class CardiacChannelMetrics:
    dc: float
    ac_rms: float
    pulsatility: float
    clipped_fraction: float
    cardiac_band_power_fraction: float
    cardiac_snr_db: float

    def to_dict(self) -> dict:
        return {
            "dc": self.dc,
            "ac_rms": self.ac_rms,
            "pulsatility": self.pulsatility,
            "clipped_fraction": self.clipped_fraction,
            "cardiac_band_power_fraction": self.cardiac_band_power_fraction,
            "cardiac_snr_db": self.cardiac_snr_db,
        }


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def variance(values: list[float]) -> float:
    if not values:
        return 0.0
    m = mean(values)
    return sum((v - m) ** 2 for v in values) / len(values)


def pearson_correlation(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    ma, mb = mean(a), mean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    denom = (va * vb) ** 0.5
    return cov / denom if denom > 0 else 0.0


def _linear_detrend(values: list[float]) -> list[float]:
    """Remove the least-squares constant and linear trend."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.0]
    center = (n - 1) / 2.0
    value_mean = mean(values)
    denominator = sum((index - center) ** 2 for index in range(n))
    slope = sum(
        (index - center) * (value - value_mean)
        for index, value in enumerate(values)
    ) / denominator
    return [
        value - (value_mean + slope * (index - center))
        for index, value in enumerate(values)
    ]


def _hann(values: list[float]) -> list[float]:
    n = len(values)
    if n <= 1:
        return [0.0] * n
    return [
        value * (0.5 - 0.5 * math.cos(2.0 * math.pi * index / (n - 1)))
        for index, value in enumerate(values)
    ]


def _dft_bin(values: list[float], bin_index: int) -> complex:
    """One exact DFT bin, dependency-free; only cardiac bins are evaluated."""
    n = len(values)
    angle = 2.0 * math.pi * bin_index / n
    coefficient = 2.0 * math.cos(angle)
    previous = 0.0
    previous_2 = 0.0
    for value in values:
        current = value + coefficient * previous - previous_2
        previous_2 = previous
        previous = current
    return complex(
        previous - previous_2 * math.cos(angle),
        previous_2 * math.sin(angle),
    )


def _cardiac_spectrum(
    samples: list[int] | list[float], effective_sample_rate_hz: int
) -> tuple[list[complex], float, float]:
    """Return cardiac-bin coefficients, cardiac power, and all other AC power.

    Linear detrending prevents baseline slope from becoming signal. A Hann
    window limits leakage. Cardiac power uses the closed 0.7..4.0 Hz interval;
    every other positive-frequency bin is disjoint out-of-band power.
    """
    if not samples or effective_sample_rate_hz <= 0:
        return [], 0.0, 0.0
    values = [float(value) for value in samples]
    windowed = _hann(_linear_detrend(values))
    n = len(windowed)
    first_bin = math.ceil(CARDIAC_BAND_LOW_HZ * n / effective_sample_rate_hz)
    last_bin = math.floor(CARDIAC_BAND_HIGH_HZ * n / effective_sample_rate_hz)
    nyquist_bin = n // 2
    first_bin = max(1, first_bin)
    last_bin = min(nyquist_bin, last_bin)
    coefficients = (
        [_dft_bin(windowed, index) for index in range(first_bin, last_bin + 1)]
        if first_bin <= last_bin
        else []
    )
    cardiac_power = sum(abs(value) ** 2 for value in coefficients)

    # Parseval supplies total positive-frequency power without calculating
    # every DFT bin. DC and (for even N) Nyquist are single-sided bins.
    total_dft_power = n * sum(value * value for value in windowed)
    dc_power = sum(windowed) ** 2
    nyquist_power = 0.0
    if n % 2 == 0:
        nyquist_power = sum(
            value if index % 2 == 0 else -value
            for index, value in enumerate(windowed)
        ) ** 2
    positive_ac_power = max(0.0, (total_dft_power - dc_power - nyquist_power) / 2.0)
    out_of_band_power = max(0.0, positive_ac_power - cardiac_power)
    return coefficients, cardiac_power, out_of_band_power


def cardiac_channel_metrics(
    samples: list[int], effective_sample_rate_hz: int
) -> CardiacChannelMetrics:
    """Version-2 cardiac-band metrics; no acceptance threshold is implied."""
    if not samples:
        return CardiacChannelMetrics(
            dc=0.0,
            ac_rms=0.0,
            pulsatility=0.0,
            clipped_fraction=0.0,
            cardiac_band_power_fraction=0.0,
            cardiac_snr_db=-PSNR_CLAMP_DB,
        )
    values = [float(value) for value in samples]
    dc = mean(values)
    ac_rms = variance(values) ** 0.5
    pulsatility = ac_rms / dc if dc > 0 else 0.0
    clipped_fraction = sum(value >= ADC_18BIT_MAX for value in samples) / len(samples)
    _, cardiac_power, out_of_band_power = _cardiac_spectrum(
        samples, effective_sample_rate_hz
    )
    total_power = cardiac_power + out_of_band_power
    fraction = cardiac_power / total_power if total_power > 0 else 0.0
    if cardiac_power == 0.0:
        snr = -PSNR_CLAMP_DB
    elif out_of_band_power == 0.0:
        snr = PSNR_CLAMP_DB
    else:
        snr = 10.0 * math.log10(cardiac_power / out_of_band_power)
        snr = max(-PSNR_CLAMP_DB, min(PSNR_CLAMP_DB, snr))
    return CardiacChannelMetrics(
        dc=dc,
        ac_rms=ac_rms,
        pulsatility=pulsatility,
        clipped_fraction=clipped_fraction,
        cardiac_band_power_fraction=fraction,
        cardiac_snr_db=snr,
    )


def _cardiac_agreement_sub_blocks(n: int) -> list[tuple[int, int]]:
    """Overlapping sub-block spans covering a window of length `n`.

    A single whole-window cardiac-band correlation is not a stable measure
    of RED/IR phase agreement: independent-review evidence on real Stage 9
    captures (`research/stage9a_psnr_forensics_2026-09-08/`) showed the
    same 30 s window's RED/IR phase relationship can genuinely differ
    between its first and later thirds (motion, contact settling, or
    ambient change mid-capture), not merely appear to differ because of a
    windowing/taper artifact. A single Hann- or rectangular-windowed point
    estimate over the whole window then reports whichever part of the
    signal that window shape happens to weight most, and can land on
    either sign depending only on that arbitrary choice. Splitting the
    window into several overlapping thirds and reporting the worst
    (most-disagreeing) block instead means a genuine disagreement anywhere
    in the window is always reflected in the result, never diluted or
    tapered away by a different part of the window that happens to agree.
    """
    if n < 200:
        return [(0, n)]
    block = min(max(200, n // 3), n)
    step = max(1, block // 2)
    bounds = []
    start = 0
    while start + block <= n:
        bounds.append((start, start + block))
        start += step
    return bounds


def _cardiac_block_correlation(
    red: list[int], ir: list[int], effective_sample_rate_hz: int
) -> float:
    """Zero-lag agreement of linearly detrended, Hann-windowed cardiac bins
    within one sub-block. See `cardiac_band_correlation` for why a single
    call over the whole window is not used directly."""
    red_bins, red_power, _ = _cardiac_spectrum(red, effective_sample_rate_hz)
    ir_bins, ir_power, _ = _cardiac_spectrum(ir, effective_sample_rate_hz)
    denominator = (red_power * ir_power) ** 0.5
    if denominator == 0.0 or len(red_bins) != len(ir_bins):
        return 0.0
    cross_power = sum(
        (red_value * ir_value.conjugate()).real
        for red_value, ir_value in zip(red_bins, ir_bins)
    )
    return max(-1.0, min(1.0, cross_power / denominator))


def cardiac_band_correlation(
    red: list[int], ir: list[int], effective_sample_rate_hz: int
) -> float:
    """Worst-case (most-disagreeing) zero-lag cardiac-band RED/IR agreement
    across overlapping sub-blocks of the window; see
    `_cardiac_agreement_sub_blocks` for why a single whole-window estimate
    is not used. Reduces to a single whole-window call when the input is
    too short to sub-block."""
    if not red or len(red) != len(ir):
        return 0.0
    values = [
        _cardiac_block_correlation(red[start:end], ir[start:end], effective_sample_rate_hz)
        for start, end in _cardiac_agreement_sub_blocks(len(red))
    ]
    return min(values)


def cardiac_quality_metrics(
    red: list[int], ir: list[int], effective_sample_rate_hz: int
) -> dict:
    """Complete metric-contract-v2 payload, with cardiac-band agreement only."""
    return {
        "metric_version": CARDIAC_METRIC_VERSION,
        "red": cardiac_channel_metrics(red, effective_sample_rate_hz).to_dict(),
        "ir": cardiac_channel_metrics(ir, effective_sample_rate_hz).to_dict(),
        "cardiac_band_red_ir_correlation": cardiac_band_correlation(
            red, ir, effective_sample_rate_hz
        ),
    }


# ---------------------------------------------------------------------------
# Additive Stage 9A metric-defect remediation (metric_version 2.1.0).
#
# Gap analysis: receiver/extended_quality_metrics.py already provides an L/B/H
# power breakdown (band_power_breakdown) and a fixed-3-block dominant-
# frequency stability check (baseline_frequency_stability) -- this section
# does not reimplement either. It adds exactly the two fields the Stage 9A
# forensic pass found missing:
#
#   * drift_bounded_cardiac_snr_db: identical formula to cardiac_snr_db, but
#     the denominator excludes low-frequency (< CARDIAC_BAND_LOW_HZ) drift
#     power -- the same low-bin technique band_power_breakdown() uses for its
#     L term -- so sub-0.7 Hz baseline wander no longer dominates the "noise"
#     term the way it does in every retained C16/C17/C18 window.
#   * dominant_frequency_stability: per-channel dominant cardiac-band peak
#     plus a 0..1 consistency score across the SAME overlapping sub-blocks
#     _cardiac_agreement_sub_blocks() already defines for cross-channel
#     agreement -- a different, complementary convention to
#     extended_quality_metrics.py's separate fixed non-overlapping thirds.
#
# Neither field is wired into any accept/reject path, review_gate, or any
# caller outside this module's own tests. CARDIAC_METRIC_VERSION ("2.0.0")
# and cardiac_channel_metrics()/cardiac_quality_metrics() above this comment
# are byte-for-byte unchanged.
# ---------------------------------------------------------------------------

CARDIAC_METRIC_VERSION_2_1_0 = "2.1.0"


def _low_frequency_power(samples: list[int], effective_sample_rate_hz: int) -> float:
    """Power in bins (1 .. first cardiac bin - 1), i.e. sub-CARDIAC_BAND_LOW_HZ
    drift. Same technique band_power_breakdown() uses for its L term."""
    if not samples or effective_sample_rate_hz <= 0:
        return 0.0
    values = [float(value) for value in samples]
    windowed = _hann(_linear_detrend(values))
    n = len(windowed)
    first_cardiac_bin = max(1, math.ceil(CARDIAC_BAND_LOW_HZ * n / effective_sample_rate_hz))
    return sum(abs(_dft_bin(windowed, index)) ** 2 for index in range(1, first_cardiac_bin))


@dataclass(frozen=True)
class DriftBoundedCardiacMetrics:
    drift_bounded_cardiac_snr_db: float
    excluded_low_frequency_power: float

    def to_dict(self) -> dict:
        return {
            "drift_bounded_cardiac_snr_db": self.drift_bounded_cardiac_snr_db,
            "excluded_low_frequency_power": self.excluded_low_frequency_power,
        }


def drift_bounded_cardiac_metrics(
    samples: list[int], effective_sample_rate_hz: int
) -> DriftBoundedCardiacMetrics:
    """cardiac_snr_db recomputed with sub-CARDIAC_BAND_LOW_HZ drift power
    excluded from the denominator. Record-only; not an acceptance gate."""
    _, cardiac_power, out_of_band_power = _cardiac_spectrum(samples, effective_sample_rate_hz)
    low_power = _low_frequency_power(samples, effective_sample_rate_hz)
    bounded_noise_power = max(0.0, out_of_band_power - low_power)
    if cardiac_power == 0.0:
        snr = -PSNR_CLAMP_DB
    elif bounded_noise_power == 0.0:
        snr = PSNR_CLAMP_DB
    else:
        snr = 10.0 * math.log10(cardiac_power / bounded_noise_power)
        snr = max(-PSNR_CLAMP_DB, min(PSNR_CLAMP_DB, snr))
    return DriftBoundedCardiacMetrics(
        drift_bounded_cardiac_snr_db=snr, excluded_low_frequency_power=low_power
    )


def _cardiac_peak_hz_from_block(
    samples: list[int], effective_sample_rate_hz: int
) -> float | None:
    """Dominant cardiac-band frequency for one block. Reuses
    _cardiac_spectrum()'s cardiac-bin coefficients; only adds the
    bin-index -> Hz conversion and argmax."""
    if not samples or effective_sample_rate_hz <= 0:
        return None
    coefficients, cardiac_power, _ = _cardiac_spectrum(samples, effective_sample_rate_hz)
    if not coefficients or cardiac_power == 0.0:
        return None
    n = len(samples)
    first_bin = max(1, math.ceil(CARDIAC_BAND_LOW_HZ * n / effective_sample_rate_hz))
    powers = [abs(value) ** 2 for value in coefficients]
    peak_offset = max(range(len(powers)), key=lambda index: powers[index])
    return (first_bin + peak_offset) * effective_sample_rate_hz / n


@dataclass(frozen=True)
class DominantFrequencyStability:
    dominant_hz: float | None
    sub_block_hz: list[float]
    consistency_score: float

    def to_dict(self) -> dict:
        return {
            "dominant_hz": self.dominant_hz,
            "sub_block_hz": self.sub_block_hz,
            "consistency_score": self.consistency_score,
        }


def dominant_frequency_stability(
    samples: list[int], effective_sample_rate_hz: int
) -> DominantFrequencyStability:
    """Overall dominant cardiac-band peak plus a consistency score across
    _cardiac_agreement_sub_blocks()' overlapping sub-blocks -- the same
    boundaries cardiac_band_correlation() uses, not extended_quality_
    metrics.py's separate fixed-3-block convention. consistency_score is
    1.0 when every sub-block agrees on the same peak bin and falls toward
    0.0 as the peak wanders across the full cardiac bandwidth."""
    overall_hz = _cardiac_peak_hz_from_block(samples, effective_sample_rate_hz)
    sub_block_hz = []
    for start, end in _cardiac_agreement_sub_blocks(len(samples)):
        hz = _cardiac_peak_hz_from_block(samples[start:end], effective_sample_rate_hz)
        if hz is not None:
            sub_block_hz.append(hz)
    if len(sub_block_hz) >= 2:
        spread = max(sub_block_hz) - min(sub_block_hz)
        bandwidth = CARDIAC_BAND_HIGH_HZ - CARDIAC_BAND_LOW_HZ
        consistency_score = max(0.0, 1.0 - (spread / bandwidth))
    elif sub_block_hz:
        consistency_score = 1.0
    else:
        consistency_score = 0.0
    return DominantFrequencyStability(
        dominant_hz=overall_hz, sub_block_hz=sub_block_hz, consistency_score=consistency_score
    )


def cardiac_quality_metrics_v2_1_0(
    red: list[int], ir: list[int], effective_sample_rate_hz: int
) -> dict:
    """Additive v2.1.0 payload: the exact v2.0.0 cardiac_quality_metrics()
    result plus drift_bounded_cardiac_snr_db and dominant_frequency_stability
    per channel. Not called by any production path or default metric_version;
    test/record-only scope only."""
    payload = cardiac_quality_metrics(red, ir, effective_sample_rate_hz)
    payload["metric_version"] = CARDIAC_METRIC_VERSION_2_1_0
    for channel_name, samples in (("red", red), ("ir", ir)):
        payload[channel_name]["drift_bounded_cardiac_snr_db"] = drift_bounded_cardiac_metrics(
            samples, effective_sample_rate_hz
        ).drift_bounded_cardiac_snr_db
        payload[channel_name]["dominant_frequency_stability"] = dominant_frequency_stability(
            samples, effective_sample_rate_hz
        ).to_dict()
    return payload


def _moving_average(values: list[float], window: int) -> list[float]:
    """Centered moving average with edge-shrinking windows (no external
    padding assumption, no circular wraparound)."""
    n = len(values)
    half = window // 2
    out = [0.0] * n
    prefix = [0.0] * (n + 1)
    for i, v in enumerate(values):
        prefix[i + 1] = prefix[i] + v
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out[i] = (prefix[hi] - prefix[lo]) / (hi - lo)
    return out


def psnr_db(detrended: list[float], effective_sample_rate_hz: int) -> float:
    """Pulsatile signal-to-noise ratio proxy: variance of a moving-average
    smoothed component vs. variance of the residual. See spec §4 for the
    exact window-length formula and rationale."""
    if not detrended:
        return -PSNR_CLAMP_DB
    window = max(3, round(effective_sample_rate_hz / 8))
    signal = _moving_average(detrended, window)
    noise = [d - s for d, s in zip(detrended, signal)]
    var_signal = variance(signal)
    var_noise = variance(noise)
    if var_signal == 0.0:
        # No pulsatile component at all (e.g. a flat-line window) — never
        # reported as a false "great SNR", even if there is also no noise.
        return -PSNR_CLAMP_DB
    if var_noise == 0.0:
        return PSNR_CLAMP_DB
    value = 10.0 * math.log10(var_signal / var_noise)
    return max(-PSNR_CLAMP_DB, min(PSNR_CLAMP_DB, value))


def channel_metrics(samples: list[int], effective_sample_rate_hz: int) -> ChannelMetrics:
    if not samples:
        return ChannelMetrics(dc=0.0, ac_rms=0.0, pulsatility=0.0, clipped_fraction=0.0, psnr_db=-PSNR_CLAMP_DB)

    values = [float(v) for v in samples]
    dc = mean(values)
    detrended = [v - dc for v in values]
    ac_rms = variance(values) ** 0.5
    pulsatility = (ac_rms / dc) if dc > 0 else 0.0
    clipped = sum(1 for v in samples if v >= ADC_18BIT_MAX)
    clipped_fraction = clipped / len(samples)
    snr = psnr_db(detrended, effective_sample_rate_hz)

    return ChannelMetrics(
        dc=dc,
        ac_rms=ac_rms,
        pulsatility=pulsatility,
        clipped_fraction=clipped_fraction,
        psnr_db=snr,
    )


def is_flatline(samples: list[int]) -> bool:
    return bool(samples) and all(v == samples[0] for v in samples)


def is_finite_sample(v: object) -> bool:
    try:
        return isinstance(v, int) and not isinstance(v, bool) and v == v
    except TypeError:
        return False
