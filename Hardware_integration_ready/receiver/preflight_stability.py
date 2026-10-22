"""preflight_stability.py — Stage 9 priority-2 preflight stability gate.

Runs against the sensor actively streaming, before the real 30s/3000-sample
measurement window is trusted (see STAGE9_METRIC_CORRECTION_HANDOFF.md,
execution priority 2). Splits the first ~1-2s of raw samples into ~0.5s
blocks, per channel, and requires ALL of: no clipping; DC magnitude in each
block clearing a calibrated in-contact floor (not just near the sensor's
no-contact/ambient baseline); and DC shift between consecutive blocks under
a bounded threshold. On failure the caller should ABSTAIN (stop, log the raw
preflight samples, do not start the measurement window) rather than
power-cycle or retry silently.

The DC-magnitude check exists because stability alone is not proof of
contact: an on-device check (2026-09-10) confirmed a sensor with nothing on
it reads a stable, low DC baseline that passes a DC-shift-only check just as
cleanly as real steady contact would. See CONTACT_CALIBRATION below.

Pure Python, no BLE/asyncio dependency, so it is testable without hardware.
Deliberately does not touch config/stage9_metric_recording_contract_v2.json
or the Stage 9B ledger/QA schema -- this is a pre-window gate, not a
capture-acceptance metric, and has its own minimal ABSTAIN log format.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from capture_quality_metrics import ADC_18BIT_MAX, mean
from window_assembler import EFFECTIVE_RATE_HZ

DEFAULT_BLOCK_SECONDS = 0.5
DEFAULT_MIN_SECONDS = 1.0          # lower bound of the "~1-2s" preflight window
DEFAULT_TIMEOUT_SECONDS = 3.0      # hard cap; unmet by DEFAULT_TIMEOUT_SECONDS -> ABSTAIN
DEFAULT_DC_SHIFT_THRESHOLD_PCT = 8.0

ABSTAIN = "ABSTAIN"
PASS = "PASS"


@dataclass(frozen=True)
class ContactBaseline:
    """No-contact DC baseline for one sensor profile (config_id), from a real
    on-device calibration pass -- never a guessed/extrapolated number. LED
    brightness differs drastically across Stage 9 profiles (led_brightness
    0x1F/0x3E/0x5E/... for configs 16/17/18/...), and a 2026-09-10 on-device
    check confirmed DC does not scale in any simple way you could extrapolate
    from one config to another: at config 16, no-contact DC held flat at
    ~342-349 (red) across four 0.5s blocks, while firm contact climbed
    358 -> 1840 -> 21157 -> 27715 in the same four blocks (raw fixture:
    tests/fixtures/preflight/config16_no_contact_2026-09-10.json /
    config16_firm_contact_2026-09-10.json). That is specific to config 16's
    LED brightness (0x1F, the dimmest Stage 9 profile) -- do not reuse
    min_contact_dc_multiplier or the DC values below for any other
    config_id."""

    config_id: int
    red_no_contact_dc: float
    ir_no_contact_dc: float
    min_contact_dc_multiplier: float
    calibration_note: str


# Real, on-device-calibrated no-contact baselines. Add an entry here ONLY
# after running the same no-contact/firm-contact calibration pass for that
# specific config_id -- see the raw fixtures cited above. Any unlisted Stage 9
# profile is NOT calibrated: evaluate_preflight() ABSTAINs
# with reason NO_CONTACT_CALIBRATION for any config_id not in this table,
# rather than silently skipping the contact-presence check for them.
CONTACT_CALIBRATION: dict[int, ContactBaseline] = {
    16: ContactBaseline(
        config_id=16,
        red_no_contact_dc=191.965,
        ir_no_contact_dc=169.92,
        # Fresh continuity-012 calibration. The minimum stable Z23 contact
        # block was 126.85x RED and 97.34x IR above the no-contact means.
        # A 3x floor is directly separated from both observed C16 states and
        # is not extrapolated from either C17 or C18.
        min_contact_dc_multiplier=3.0,
        calibration_note="on-device continuity-012 pass, 2026-09-15, config 16 (led_brightness=0x1F) only",
    ),
    17: ContactBaseline(
        config_id=17,
        red_no_contact_dc=580.96,
        ir_no_contact_dc=517.81,
        # Fresh continuity-013/014 calibration. The minimum stable Z23
        # contact block was 105.00x RED and 74.20x IR above the no-contact
        # means. A 3x floor is directly separated from both observed C17
        # states and is not extrapolated from C16 or C18.
        min_contact_dc_multiplier=3.0,
        calibration_note="on-device continuity-013/014 pass, 2026-09-15, config 17 (led_brightness=0x3E) only",
    ),
    18: ContactBaseline(
        config_id=18,
        red_no_contact_dc=600.605,
        ir_no_contact_dc=550.135,
        # Fresh continuity-011 calibration. The minimum stable Z23 contact
        # block was 139.11x RED and 95.22x IR above the no-contact means.
        # A 3x floor is directly separated from both observed C18 states and
        # is not extrapolated from either C16 or C17.
        min_contact_dc_multiplier=3.0,
        calibration_note="on-device continuity-011 pass, 2026-09-15, config 18 (led_brightness=0x5E) only",
    ),
    # Other profiles: deliberately absent. See CONTACT_CALIBRATION docstring.
}


@dataclass(frozen=True)
class PreflightBlockStats:
    index: int
    start_sample: int
    end_sample: int
    red_dc: float
    ir_dc: float
    red_clipped_fraction: float
    ir_clipped_fraction: float

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "red_dc": self.red_dc,
            "ir_dc": self.ir_dc,
            "red_clipped_fraction": self.red_clipped_fraction,
            "ir_clipped_fraction": self.ir_clipped_fraction,
        }


@dataclass(frozen=True)
class PreflightResult:
    status: str  # PASS or ABSTAIN
    reason: str | None
    blocks: list[PreflightBlockStats] = field(default_factory=list)
    red_dc_shift_pct: list[float] = field(default_factory=list)
    ir_dc_shift_pct: list[float] = field(default_factory=list)
    samples_evaluated: int = 0

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "blocks": [b.to_dict() for b in self.blocks],
            "red_dc_shift_pct": self.red_dc_shift_pct,
            "ir_dc_shift_pct": self.ir_dc_shift_pct,
            "samples_evaluated": self.samples_evaluated,
        }


def _split_blocks(n_samples: int, block_samples: int) -> list[tuple[int, int]]:
    """Consecutive, non-overlapping [start, end) ranges. A trailing partial
    block is dropped -- a half-filled block would distort its own DC."""
    blocks = []
    start = 0
    while start + block_samples <= n_samples:
        blocks.append((start, start + block_samples))
        start += block_samples
    return blocks


def evaluate_preflight(
    red: list[int],
    ir: list[int],
    *,
    config_id: int,
    sample_rate_hz: int = EFFECTIVE_RATE_HZ,
    block_seconds: float = DEFAULT_BLOCK_SECONDS,
    dc_shift_threshold_pct: float = DEFAULT_DC_SHIFT_THRESHOLD_PCT,
    clip_value: int = ADC_18BIT_MAX,
    contact_baseline: ContactBaseline | None = None,
) -> PreflightResult:
    """Evaluate already-collected preflight samples. Does not know about
    wall-clock timeouts -- a caller that times out before collecting enough
    samples should still call this with whatever it has; too few complete
    blocks is itself an ABSTAIN reason (INSUFFICIENT_SAMPLES).

    config_id selects which CONTACT_CALIBRATION entry gates the contact-
    presence (DC magnitude) check. A stable-but-flat reading passes the DC-
    shift check on its own -- that is exactly what a sensor with nothing on
    it looks like (confirmed on-device 2026-09-10: no-contact DC held flat
    within ~2% across four 0.5s blocks). config_id is required, not optional
    with a silent skip, so a config with no real calibration data can never
    quietly bypass the contact-presence check."""
    if len(red) != len(ir):
        raise ValueError(f"red/ir length mismatch: {len(red)} vs {len(ir)}")

    block_samples = max(1, round(block_seconds * sample_rate_hz))
    ranges = _split_blocks(len(red), block_samples)

    if len(ranges) < 2:
        return PreflightResult(
            status=ABSTAIN,
            reason="INSUFFICIENT_SAMPLES",
            samples_evaluated=len(red),
        )

    blocks: list[PreflightBlockStats] = []
    for i, (start, end) in enumerate(ranges):
        red_block = red[start:end]
        ir_block = ir[start:end]
        blocks.append(
            PreflightBlockStats(
                index=i,
                start_sample=start,
                end_sample=end,
                red_dc=mean(red_block),
                ir_dc=mean(ir_block),
                red_clipped_fraction=sum(v >= clip_value for v in red_block) / len(red_block),
                ir_clipped_fraction=sum(v >= clip_value for v in ir_block) / len(ir_block),
            )
        )

    for b in blocks:
        if b.red_clipped_fraction > 0 or b.ir_clipped_fraction > 0:
            return PreflightResult(
                status=ABSTAIN,
                reason="CLIPPING",
                blocks=blocks,
                samples_evaluated=len(red),
            )

    if contact_baseline is not None and contact_baseline.config_id != config_id:
        raise ValueError("contact baseline config_id mismatch")
    baseline = contact_baseline or CONTACT_CALIBRATION.get(config_id)
    if baseline is None:
        return PreflightResult(
            status=ABSTAIN,
            reason="NO_CONTACT_CALIBRATION",
            blocks=blocks,
            samples_evaluated=len(red),
        )

    red_floor = baseline.red_no_contact_dc * baseline.min_contact_dc_multiplier
    ir_floor = baseline.ir_no_contact_dc * baseline.min_contact_dc_multiplier
    if any(b.red_dc < red_floor or b.ir_dc < ir_floor for b in blocks):
        return PreflightResult(
            status=ABSTAIN,
            reason="NO_CONTACT_DETECTED",
            blocks=blocks,
            samples_evaluated=len(red),
        )

    def _shift_pct(prev_dc: float, cur_dc: float) -> float:
        if prev_dc == 0:
            return float("inf")
        return abs(cur_dc - prev_dc) / abs(prev_dc) * 100.0

    red_shifts = [_shift_pct(blocks[i - 1].red_dc, blocks[i].red_dc) for i in range(1, len(blocks))]
    ir_shifts = [_shift_pct(blocks[i - 1].ir_dc, blocks[i].ir_dc) for i in range(1, len(blocks))]

    if any(s > dc_shift_threshold_pct for s in red_shifts) or any(
        s > dc_shift_threshold_pct for s in ir_shifts
    ):
        return PreflightResult(
            status=ABSTAIN,
            reason="DC_SHIFT_EXCEEDS_THRESHOLD",
            blocks=blocks,
            red_dc_shift_pct=red_shifts,
            ir_dc_shift_pct=ir_shifts,
            samples_evaluated=len(red),
        )

    return PreflightResult(
        status=PASS,
        reason=None,
        blocks=blocks,
        red_dc_shift_pct=red_shifts,
        ir_dc_shift_pct=ir_shifts,
        samples_evaluated=len(red),
    )


def write_abstain_log(
    out_dir: Path,
    *,
    hardware_uid: str,
    config_id: int,
    result: PreflightResult,
    red: list[int],
    ir: list[int],
    elapsed_s: float,
) -> Path:
    """Log an ABSTAIN decision with the raw preflight samples that caused it.
    Never called on PASS -- a passing preflight has nothing to log; the real
    measurement window carries its own record. Per STAGE9_METRIC_CORRECTION_
    HANDOFF.md: keep every failed attempt, never delete an ABSTAIN entry."""
    if result.passed:
        raise ValueError("write_abstain_log() must only be called for a failed (ABSTAIN) preflight")

    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = out_dir / f"preflight_abstain_{hardware_uid}_config{config_id}_{timestamp}.json"
    payload = {
        "hardware_uid": hardware_uid,
        "config_id": config_id,
        "elapsed_s": elapsed_s,
        "raw_red": red,
        "raw_ir": ir,
        **result.to_dict(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
