"""extended_quality_metrics.py — Stage 9 priority-4 supplementary quality
metrics (see STAGE9_METRIC_CORRECTION_HANDOFF.md, execution priority 4).

Adds NEW, additional per-channel/per-window metrics alongside the existing,
frozen capture_quality_metrics.py cardiac metric (cardiac_snr_db) and its QA
threshold (config/stage9_metric_recording_contract_v2.json) — neither is
touched, replaced, or reinterpreted by anything in this file. Everything
here is diagnostic/reporting only, same stated scope as
capture_quality_metrics.py itself (not wired into receiver/sqi_gate.py's
accept/reject path, and not used to change any existing threshold).

Reuses, rather than reimplements, capture_quality_metrics.py's cardiac-band
DFT (_cardiac_spectrum), detrend/window helpers, sub-block splitting
(_cardiac_agreement_sub_blocks), and cross-channel agreement
(_cardiac_block_correlation / cardiac_band_correlation) — see each
function's docstring below for exactly what it reuses vs. adds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from capture_quality_metrics import (
    CARDIAC_BAND_HIGH_HZ,
    CARDIAC_BAND_LOW_HZ,
    _cardiac_agreement_sub_blocks,
    _cardiac_block_correlation,
    _cardiac_spectrum,
    _dft_bin,
    _hann,
    _linear_detrend,
    cardiac_band_correlation,
)

EXTENDED_METRIC_VERSION = "1.0.0"
DEFAULT_BASELINE_STABILITY_BLOCKS = 3  # matches complete_research/reproduce/analyze.py's block_peak_hz


@dataclass(frozen=True)
class BandPowerBreakdown:
    """L/B/H power split, extracted from the inline computation in
    complete_research/reproduce/analyze.py's calc(): L = (0, CARDIAC_BAND_LOW_HZ)
    exclusive of DC, B = cardiac band [CARDIAC_BAND_LOW_HZ, CARDIAC_BAND_HIGH_HZ]
    inclusive, H = everything above CARDIAC_BAND_HIGH_HZ up to Nyquist.

    cardiac_power here is the exact same number cardiac_channel_metrics()
    uses internally (both call _cardiac_spectrum with identical band edges),
    so this breakdown's B is guaranteed consistent with, not a reinvention
    of, the frozen cardiac_snr_db computation. L + B + H equals the total
    positive-frequency AC power (Parseval), by construction.
    """

    low_power: float
    cardiac_power: float
    high_power: float

    def to_dict(self) -> dict:
        return {
            "low_power": self.low_power,
            "cardiac_power": self.cardiac_power,
            "high_power": self.high_power,
        }


@dataclass(frozen=True)
class BaselineFrequencyStability:
    """How much the dominant cardiac-band peak frequency moves across
    consecutive sub-blocks of the window (complete_research's block_peak_hz
    convention: fixed, equal, non-overlapping blocks — 3 thirds for a
    3000-sample window). A None entry means that block had no detectable
    cardiac-band power at all (flat/no signal), not a frequency of 0 Hz.
    """

    block_peak_hz: list[float | None]
    spread_hz: float
    max_pairwise_shift_hz: float

    def to_dict(self) -> dict:
        return {
            "block_peak_hz": self.block_peak_hz,
            "spread_hz": self.spread_hz,
            "max_pairwise_shift_hz": self.max_pairwise_shift_hz,
        }


@dataclass(frozen=True)
class CrossChannelSubBlockCorrelation:
    """Per-sub-block RED/IR cardiac-band agreement, plus the worst (minimum)
    value across blocks. `worst` is exactly cardiac_band_correlation()'s
    return value — same function, same sub-blocks, not reimplemented; this
    just also exposes the full per-block list analyze.py discards."""

    sub_block_correlations: list[float]
    worst: float

    def to_dict(self) -> dict:
        return {
            "sub_block_correlations": self.sub_block_correlations,
            "worst": self.worst,
        }


@dataclass(frozen=True)
class ExtendedChannelMetrics:
    band_power: BandPowerBreakdown
    periodicity_index: float
    baseline_frequency_stability: BaselineFrequencyStability

    def to_dict(self) -> dict:
        return {
            "band_power": self.band_power.to_dict(),
            "periodicity_index": self.periodicity_index,
            "baseline_frequency_stability": self.baseline_frequency_stability.to_dict(),
        }


def band_power_breakdown(
    samples: list[int], effective_sample_rate_hz: int
) -> BandPowerBreakdown:
    """L/B/H power breakdown for one channel over the whole window. Reuses
    _cardiac_spectrum() unmodified for B (cardiac_power) and the lumped
    out-of-band power; only computes the FEW low-frequency bins itself
    (there are typically ~20 of them for a 3000-sample/100Hz window) rather
    than every individual out-of-band bin, then derives H by subtraction --
    the same Parseval-based approach _cardiac_spectrum already uses for its
    own out_of_band_power, just split one level further."""
    if not samples or effective_sample_rate_hz <= 0:
        return BandPowerBreakdown(low_power=0.0, cardiac_power=0.0, high_power=0.0)

    _, cardiac_power, out_of_band_power = _cardiac_spectrum(samples, effective_sample_rate_hz)

    values = [float(v) for v in samples]
    windowed = _hann(_linear_detrend(values))
    n = len(windowed)
    first_cardiac_bin = max(1, math.ceil(CARDIAC_BAND_LOW_HZ * n / effective_sample_rate_hz))
    low_power = sum(abs(_dft_bin(windowed, i)) ** 2 for i in range(1, first_cardiac_bin))
    high_power = max(0.0, out_of_band_power - low_power)

    return BandPowerBreakdown(low_power=low_power, cardiac_power=cardiac_power, high_power=high_power)


def periodicity_index(samples: list[int], effective_sample_rate_hz: int) -> float:
    """Normalized-autocorrelation peak within the cardiac-plausible lag range
    (1/CARDIAC_BAND_HIGH_HZ .. 1/CARDIAC_BAND_LOW_HZ seconds), on linearly
    detrended samples. 1.0 = perfectly periodic at some rate within the
    cardiac band; 0.0 = no periodicity at any cardiac-plausible lag; can be
    negative for a genuinely anti-periodic (alternating) signal, though
    that is not physiologically meaningful for PPG.

    This is a new, independent measure (not a byproduct of the cardiac-band
    DFT above) — periodicity in the time domain and a clean spectral peak
    are related but not identical properties of a signal."""
    if not samples or effective_sample_rate_hz <= 0:
        return 0.0
    values = _linear_detrend([float(v) for v in samples])
    n = len(values)
    zero_lag_energy = sum(v * v for v in values)
    if zero_lag_energy == 0.0:
        return 0.0
    min_lag = max(1, round(effective_sample_rate_hz / CARDIAC_BAND_HIGH_HZ))
    max_lag = min(n - 1, round(effective_sample_rate_hz / CARDIAC_BAND_LOW_HZ))
    if min_lag > max_lag:
        return 0.0
    best = -1.0
    for lag in range(min_lag, max_lag + 1):
        cross = sum(values[i] * values[i + lag] for i in range(n - lag))
        normalized = cross / zero_lag_energy
        if normalized > best:
            best = normalized
    return max(-1.0, min(1.0, best))


def _cardiac_peak_hz(samples: list[int], effective_sample_rate_hz: int) -> float | None:
    """Dominant cardiac-band frequency for one block, dependency-free
    equivalent of analyze.py's `peak=f[card][argmax(p[card])]`. Reuses
    _cardiac_spectrum()'s cardiac-bin coefficients; only adds the
    bin-index -> Hz conversion and argmax. Returns None if the block has no
    cardiac-band power at all (too short, or a flat/silent block)."""
    if not samples or effective_sample_rate_hz <= 0:
        return None
    coefficients, cardiac_power, _ = _cardiac_spectrum(samples, effective_sample_rate_hz)
    if not coefficients or cardiac_power == 0.0:
        return None
    n = len(samples)
    first_bin = max(1, math.ceil(CARDIAC_BAND_LOW_HZ * n / effective_sample_rate_hz))
    powers = [abs(c) ** 2 for c in coefficients]
    peak_offset = max(range(len(powers)), key=lambda i: powers[i])
    peak_bin = first_bin + peak_offset
    return peak_bin * effective_sample_rate_hz / n


def baseline_frequency_stability(
    samples: list[int],
    effective_sample_rate_hz: int,
    n_blocks: int = DEFAULT_BASELINE_STABILITY_BLOCKS,
) -> BaselineFrequencyStability:
    """Splits the window into n_blocks fixed, equal, non-overlapping,
    consecutive sub-blocks (analyze.py's block_peak_hz convention: 3 fixed
    thirds for a 3000-sample window; any remainder samples that don't form
    a full final block are dropped, not padded into a short block) and
    reports how much the dominant cardiac-band peak frequency moves across
    them. Large movement suggests the apparent heart rate is not
    physiologically stable across the capture (motion, contact change, or a
    spurious peak wandering) — this alone does not identify which."""
    n = len(samples)
    block_size = n // n_blocks if n_blocks > 0 else 0
    peaks: list[float | None] = []
    if block_size > 0:
        for i in range(n_blocks):
            block = samples[i * block_size : (i + 1) * block_size]
            peaks.append(_cardiac_peak_hz(block, effective_sample_rate_hz))

    valid = [p for p in peaks if p is not None]
    if len(valid) < 2:
        return BaselineFrequencyStability(block_peak_hz=peaks, spread_hz=0.0, max_pairwise_shift_hz=0.0)

    spread_hz = max(valid) - min(valid)
    consecutive_valid_pairs = [
        (peaks[i], peaks[i + 1])
        for i in range(len(peaks) - 1)
        if peaks[i] is not None and peaks[i + 1] is not None
    ]
    max_pairwise_shift_hz = (
        max(abs(b - a) for a, b in consecutive_valid_pairs) if consecutive_valid_pairs else 0.0
    )
    return BaselineFrequencyStability(
        block_peak_hz=peaks, spread_hz=spread_hz, max_pairwise_shift_hz=max_pairwise_shift_hz
    )


def cross_channel_sub_block_correlation(
    red: list[int], ir: list[int], effective_sample_rate_hz: int
) -> CrossChannelSubBlockCorrelation:
    """Per-sub-block RED/IR cardiac-band agreement. Calls the SAME
    _cardiac_agreement_sub_blocks()/_cardiac_block_correlation() helpers
    cardiac_band_correlation() uses internally (identical sub-block
    boundaries, identical per-block formula) — this does not reinvent that
    logic, it only additionally exposes the full per-block list instead of
    just the worst value. `worst` is recomputed via the real
    cardiac_band_correlation() entry point (not min() of the list here) so
    it is guaranteed to be the exact same number analyze.py's
    worst_cardiac_corr column already reports."""
    if not red or len(red) != len(ir):
        return CrossChannelSubBlockCorrelation(sub_block_correlations=[], worst=0.0)
    sub_block_correlations = [
        _cardiac_block_correlation(red[start:end], ir[start:end], effective_sample_rate_hz)
        for start, end in _cardiac_agreement_sub_blocks(len(red))
    ]
    worst = cardiac_band_correlation(red, ir, effective_sample_rate_hz)
    return CrossChannelSubBlockCorrelation(sub_block_correlations=sub_block_correlations, worst=worst)


def extended_channel_metrics(samples: list[int], effective_sample_rate_hz: int) -> ExtendedChannelMetrics:
    return ExtendedChannelMetrics(
        band_power=band_power_breakdown(samples, effective_sample_rate_hz),
        periodicity_index=periodicity_index(samples, effective_sample_rate_hz),
        baseline_frequency_stability=baseline_frequency_stability(samples, effective_sample_rate_hz),
    )


def extended_quality_metrics(red: list[int], ir: list[int], effective_sample_rate_hz: int) -> dict:
    """Complete additive payload, mirroring cardiac_quality_metrics()'s
    shape in capture_quality_metrics.py without touching that function:
    {'metric_version', 'red': {...}, 'ir': {...}, 'cross_channel': {...}}."""
    return {
        "metric_version": EXTENDED_METRIC_VERSION,
        "red": extended_channel_metrics(red, effective_sample_rate_hz).to_dict(),
        "ir": extended_channel_metrics(ir, effective_sample_rate_hz).to_dict(),
        "cross_channel": cross_channel_sub_block_correlation(red, ir, effective_sample_rate_hz).to_dict(),
    }
