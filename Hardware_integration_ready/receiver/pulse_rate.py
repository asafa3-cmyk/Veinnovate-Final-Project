"""Independent, unvalidated Z23 pulse-rate estimator; never a VeinNet BP input.

Version 0.1.1 is a development revision. A returned bpm is an engineering
candidate, not permission to publish HR to the dashboard.
"""

from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, find_peaks, sosfiltfilt


ALGORITHM_NAME = "z23_dual_channel_peak_interval"
ALGORITHM_VERSION = "0.1.1"
SAMPLE_RATE_HZ = 100


@dataclass(frozen=True)
class PulseRateEstimate:
    bpm: float | None
    reason: str
    algorithm_name: str = ALGORITHM_NAME
    algorithm_version: str = ALGORITHM_VERSION


def estimate_pulse_rate(window, *, sqi_accepted: bool) -> PulseRateEstimate:
    """Return unavailable on uncertain pulse morphology, timing, or quality."""
    if not sqi_accepted or not window.integrity.is_complete:
        return PulseRateEstimate(None, "QUALITY_REJECTED")
    if len(window.red) != 3000 or len(window.ir) != 3000:
        return PulseRateEstimate(None, "WINDOW_LENGTH")

    sos = butter(3, [0.65, 3.5], btype="bandpass", fs=SAMPLE_RATE_HZ, output="sos")
    rates = []
    for samples in (window.red, window.ir):
        raw = np.asarray(samples, dtype=np.float64)
        if not np.all(np.isfinite(raw)) or np.ptp(raw) < 1:
            return PulseRateEstimate(None, "WEAK_SIGNAL")
        signal = sosfiltfilt(sos, raw)
        amplitude = np.percentile(signal, 95) - np.percentile(signal, 5)
        if amplitude < 5:
            return PulseRateEstimate(None, "WEAK_SIGNAL")
        peaks, props = find_peaks(signal, distance=28, prominence=amplitude * 0.30)
        if len(peaks) < 18:
            return PulseRateEstimate(None, "MISSED_BEATS")
        intervals = np.diff(peaks) / SAMPLE_RATE_HZ
        median = float(np.median(intervals))
        if median < 0.33 or median > 1.5:
            return PulseRateEstimate(None, "RATE_OUT_OF_RANGE")
        # Tolerate a few weak/missed candidates, but reject sustained irregularity
        # and a gap large enough to indicate multiple missing beats.
        outliers = np.abs(intervals - median) > max(0.12, median * 0.22)
        if np.mean(outliers) > 0.25 or np.max(intervals) > 2.1 * median:
            return PulseRateEstimate(None, "IRREGULAR_PEAKS")
        prominence = props["prominences"]
        if np.percentile(prominence, 10) < np.median(prominence) * 0.35:
            return PulseRateEstimate(None, "AMBIGUOUS_PEAKS")
        even, odd = prominence[::2], prominence[1::2]
        alternation_ratio = min(np.median(even), np.median(odd)) / max(
            np.median(even), np.median(odd)
        )
        lag = round(median * SAMPLE_RATE_HZ)
        if 2 * lag < len(signal) // 2:
            one = np.corrcoef(signal[:-lag], signal[lag:])[0, 1]
            two = np.corrcoef(signal[:-2 * lag], signal[2 * lag:])[0, 1]
            if two > one + 0.12 and alternation_ratio < 0.75:
                return PulseRateEstimate(None, "DOUBLE_RATE_AMBIGUITY")
        # A prominent alternating pattern can make a double-rate estimate.
        if alternation_ratio < 0.6:
            return PulseRateEstimate(None, "DOUBLE_RATE_AMBIGUITY")
        rates.append(60.0 / median)
    if abs(rates[0] - rates[1]) > max(3.0, 0.04 * min(rates)):
        return PulseRateEstimate(None, "CHANNEL_DISAGREEMENT")
    return PulseRateEstimate(round(float(np.mean(rates)), 1), "CANDIDATE")
