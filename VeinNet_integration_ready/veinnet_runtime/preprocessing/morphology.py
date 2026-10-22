"""Frozen, deterministic, label-independent PPG morphology feature transform.

Computes a 13-dimensional per-window morphology vector directly from the
sealed 100 Hz PPG waveform (shape [1000] per window, already band-passed,
detrended, and robust-normalized). No labels, no demographics, no
cross-patient statistics, and no randomness enter this function: given the
same PPG samples it always returns the same 13 numbers.

Copied VERBATIM (no lines altered) from gate5_v6pro/morphology.py --
see ../../contracts/morphology_contract.json for source hash and provenance.
Only NumPy is used so this module has no dependency beyond numpy.
"""
from __future__ import annotations

import numpy as np

N_MORPHOLOGY = 13
WINDOW_SAMPLES = 1000
WINDOW_HZ = 100.0


def _find_peaks(x: np.ndarray, min_distance: int) -> np.ndarray:
    """Deterministic local-maxima peak finder (no SciPy dependency)."""
    if x.shape[0] < 3:
        return np.empty(0, dtype=np.int64)
    is_peak = (x[1:-1] > x[:-2]) & (x[1:-1] >= x[2:])
    candidates = np.flatnonzero(is_peak) + 1
    if candidates.size == 0:
        return candidates
    order = candidates[np.argsort(-x[candidates], kind="stable")]
    kept: list[int] = []
    for idx in order:
        if all(abs(idx - k) >= min_distance for k in kept):
            kept.append(int(idx))
    return np.array(sorted(kept), dtype=np.int64)


def compute_morphology(window: np.ndarray) -> np.ndarray:
    """window: float32/float64 array of shape (1000,) -- one 10s @100Hz PPG window.

    Returns float32 array of shape (13,). Deterministic, label-independent.
    """
    x = np.asarray(window, dtype=np.float64)
    if x.shape != (WINDOW_SAMPLES,):
        raise ValueError(f"expected shape ({WINDOW_SAMPLES},), got {x.shape}")
    if not np.all(np.isfinite(x)):
        raise ValueError("non-finite PPG window passed to compute_morphology")

    peaks = _find_peaks(x, min_distance=int(0.3 * WINDOW_HZ))  # <=200 bpm cap
    n_peaks = peaks.shape[0]

    if n_peaks >= 2:
        intervals = np.diff(peaks) / WINDOW_HZ
        mean_ibi = float(np.mean(intervals))
        std_ibi = float(np.std(intervals))
        heart_rate = 60.0 / mean_ibi if mean_ibi > 1e-6 else 0.0
    else:
        mean_ibi = 0.0
        std_ibi = 0.0
        heart_rate = 0.0

    amp = float(np.ptp(x))
    mean_val = float(np.mean(x))
    std_val = float(np.std(x))
    skew = float(np.mean((x - mean_val) ** 3) / (std_val ** 3 + 1e-8))
    kurt = float(np.mean((x - mean_val) ** 4) / (std_val ** 4 + 1e-8) - 3.0)

    d1 = np.diff(x, prepend=x[0])
    d2 = np.diff(d1, prepend=d1[0])
    mean_abs_d1 = float(np.mean(np.abs(d1)))
    max_abs_d1 = float(np.max(np.abs(d1)))
    mean_abs_d2 = float(np.mean(np.abs(d2)))

    zero_crossings = int(np.sum(np.diff(np.signbit(x - mean_val)) != 0))
    zc_rate = float(zero_crossings) / WINDOW_SAMPLES

    energy = float(np.mean(x ** 2))
    peak_density = float(n_peaks) / (WINDOW_SAMPLES / WINDOW_HZ)  # peaks per second

    feat = np.array(
        [
            heart_rate,
            mean_ibi,
            std_ibi,
            amp,
            std_val,
            skew,
            kurt,
            mean_abs_d1,
            max_abs_d1,
            mean_abs_d2,
            zc_rate,
            energy,
            peak_density,
        ],
        dtype=np.float32,
    )
    if feat.shape[0] != N_MORPHOLOGY:
        raise RuntimeError("internal error: feature vector length drift")
    if not np.all(np.isfinite(feat)):
        raise RuntimeError("compute_morphology produced non-finite output")
    return feat


def compute_morphology_reading(windows: np.ndarray) -> np.ndarray:
    """windows: shape (3, 1000), the 3 contiguous windows of one sealed reading.

    Returns shape (13,): mean of the per-window morphology vectors, matching
    veinnet_mil_v3.py's `morphology.mean(dim=1)` reduction when a (K,13) tensor
    is supplied (Veinnovate_VeinNet_Vs_ResNet34/src/models/veinnet_mil_v3.py:320-321).
    """
    if windows.shape != (3, WINDOW_SAMPLES):
        raise ValueError(f"expected shape (3,{WINDOW_SAMPLES}), got {windows.shape}")
    per_window = np.stack([compute_morphology(windows[k]) for k in range(3)], axis=0)
    return per_window.mean(axis=0).astype(np.float32)


def _self_test() -> None:
    rng = np.random.default_rng(20260823)
    t = np.arange(WINDOW_SAMPLES) / WINDOW_HZ
    synthetic = np.sin(2 * np.pi * 1.2 * t) + 0.05 * rng.standard_normal(WINDOW_SAMPLES)
    f1 = compute_morphology(synthetic.astype(np.float32))
    f2 = compute_morphology(synthetic.astype(np.float32))
    assert np.array_equal(f1, f2), "compute_morphology is not deterministic"
    assert f1.shape == (N_MORPHOLOGY,)
    reading = np.stack([synthetic, synthetic, synthetic], axis=0).astype(np.float32)
    r1 = compute_morphology_reading(reading)
    r2 = compute_morphology_reading(reading)
    assert np.array_equal(r1, r2), "compute_morphology_reading is not deterministic"
    print(f"morphology self-test OK: n_morphology={N_MORPHOLOGY}, sample={f1}")


if __name__ == "__main__":
    _self_test()
