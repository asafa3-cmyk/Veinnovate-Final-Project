"""Elapsed-time -> elapsed_segments unit conversion.

This is a pure unit conversion, frozen from
gate7_max30102_personaldelta/max30102.py:178
(MAX30102Runtime.infer: `elapsed = age / 10.0`).

Calibration-expiry POLICY -- whether a given elapsed_seconds value should
still be honored -- is intentionally NOT enforced here. That decision
belongs to Hardware_integration_ready (warning at 14400 seconds / 4h, hard
expiry at 21600 seconds / 6h; see contracts/preprocessing_contract.json
"excluded_research_values"). This module performs only the frozen unit
conversion and rejects clearly invalid (negative or non-finite) input.
"""
from __future__ import annotations

SECONDS_PER_SEGMENT = 10.0


def elapsed_seconds_to_segments(elapsed_seconds: float | int) -> float:
    """Converts elapsed real time in seconds to the model's elapsed_segments unit.

    Raises TypeError/ValueError on invalid input. Does not enforce any
    calibration-expiry policy -- see module docstring.
    """
    if isinstance(elapsed_seconds, bool) or not isinstance(elapsed_seconds, (int, float)):
        raise TypeError(f"elapsed_seconds must be int or float; got {type(elapsed_seconds)!r}")
    value = float(elapsed_seconds)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"elapsed_seconds must be finite; got {elapsed_seconds!r}")
    if value < 0:
        raise ValueError(f"elapsed_seconds must be >= 0; got {elapsed_seconds!r}")
    return value / SECONDS_PER_SEGMENT
