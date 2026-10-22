"""sqi_gate.py — signal-quality gating. A BP prediction may only be produced
from a window that passes every rule here (spec §5). This module never
"softens" a rejection into a lower-confidence acceptance — a failing window
always yields ABSTAIN, never a labeled BP value.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from window_assembler import AssembledWindow, WINDOW_SAMPLE_COUNT

# Minimum rejection rules, spec §5. Kept as named constants (not magic
# numbers) so they can be surfaced verbatim in SQI-reason reporting and
# tuned centrally if a validation study demands it.
MAX_PACKET_LOSS_FRACTION = 0.02
MAX_CLIPPED_FRACTION = 0.01
MIN_CONTACT_DC = 1000
MIN_RED_IR_CORRELATION = 0.2
MIN_RED_PULSATILITY = 0.0001

# 18-bit ADC full-scale — a sample at or above this is considered clipped.
ADC_18BIT_MAX = (1 << 18) - 1


@dataclass
class SqiResult:
    accepted: bool
    reasons: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.accepted = False
        self.reasons.append(reason)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _pearson_correlation(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    ma, mb = _mean(a), _mean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    denom = (va * vb) ** 0.5
    return cov / denom if denom > 0 else 0.0


def evaluate_window(window: AssembledWindow) -> SqiResult:
    """Runs every minimum-rejection rule from spec §5 that can be computed
    from raw RED/IR alone. Motion/optical-proxy gating beyond DC/pulsatility
    thresholds is intentionally left to a documented follow-on (see
    README "Known Limitations") rather than approximated here."""
    result = SqiResult(accepted=True)
    integ = window.integrity

    if integ.loss_fraction > MAX_PACKET_LOSS_FRACTION:
        result.reject(f"packet_loss={integ.loss_fraction:.4f} > {MAX_PACKET_LOSS_FRACTION}")
    if integ.duplicate_samples > 0:
        result.reject(f"duplicate_samples={integ.duplicate_samples}")
    if integ.out_of_order_packets > 0:
        result.reject(f"out_of_order_packets={integ.out_of_order_packets}")
    if integ.session_changed:
        result.reject("sensor_session_change_inside_window")
    if integ.config_changed:
        result.reject("sensor_configuration_changed_inside_window")
    if not integ.loss_evidence_available or integ.acquisition_uncertainty:
        result.reject("acquisition_continuity_uncertain")
    if integ.software_overflow_count:
        result.reject("software_acquisition_overflow")
    if integ.fifo_overflow_detected:
        result.reject("fifo_overflow_unaccounted")
    if len(window.red) != WINDOW_SAMPLE_COUNT or len(window.ir) != WINDOW_SAMPLE_COUNT:
        result.reject(f"incorrect_window_sample_count={len(window.red)}")

    if not window.red or not window.ir:
        result.reject("empty_window")
        return result

    if any((not _is_finite(v)) for v in window.red) or any((not _is_finite(v)) for v in window.ir):
        result.reject("nan_or_invalid_sample_present")

    clipped = sum(1 for v in window.red if v >= ADC_18BIT_MAX) + sum(1 for v in window.ir if v >= ADC_18BIT_MAX)
    clipped_fraction = clipped / (2 * len(window.red))
    if clipped_fraction > MAX_CLIPPED_FRACTION:
        result.reject(f"clipped_fraction={clipped_fraction:.4f} > {MAX_CLIPPED_FRACTION}")

    red_f = [float(v) for v in window.red]
    ir_f = [float(v) for v in window.ir]
    contact_dc = _mean(ir_f)
    if contact_dc < MIN_CONTACT_DC:
        result.reject(f"contact_dc={contact_dc:.1f} < {MIN_CONTACT_DC}")

    correlation = _pearson_correlation(red_f, ir_f)
    if correlation < MIN_RED_IR_CORRELATION:
        result.reject(f"red_ir_correlation={correlation:.4f} < {MIN_RED_IR_CORRELATION}")

    red_mean = _mean(red_f)
    red_pulsatility = 0.0
    if red_mean > 0:
        red_ac = [v - red_mean for v in red_f]
        red_pulsatility = (sum(v * v for v in red_ac) / len(red_ac)) ** 0.5 / red_mean
    if red_pulsatility < MIN_RED_PULSATILITY:
        result.reject(f"red_pulsatility={red_pulsatility:.6f} < {MIN_RED_PULSATILITY}")

    if _mean(red_f) == red_f[0] and all(v == red_f[0] for v in red_f):
        result.reject("flat_line_signal")

    return result


def _is_finite(v: int | float) -> bool:
    try:
        return v == v and abs(v) != float("inf")
    except TypeError:
        return False
