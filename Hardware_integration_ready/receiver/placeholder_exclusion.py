"""placeholder_exclusion.py — diagnostic-only exclusion of the one confirmed
firmware leading-placeholder sample, by session provenance.

Background: firmware/veinnet_sensor_v1/MAX30105.h documents a pre-existing,
unfixed, one-sample structural "lag" in the vendored SparkFun ring buffer --
on a fresh session (a NEW_SESSION+START immediately after clearFIFO()), the
very first delivered sample is always whatever sits in sense.red[0]/IR[0] at
that point, which the priority-1 fix guarantees is a known, harmless zero
(not stale data from a prior session -- that was the bug priority-1 fixed).
Real sample #1 is delivered second. That first-delivery placeholder is left
in every fresh-session window by design; nothing downstream currently
excludes or flags it (see the priority-4 investigation this module follows
up on).

This module does NOT detect the placeholder by inspecting sample values
(value == 0 is the wrong approach -- a real PPG sample could legitimately be
near ambient/no-contact-low, and a value-based check would silently misfire
or silently miss). It excludes by EXPLICIT PROVENANCE instead: the caller
who issued the BLE START (and therefore knows whether this window's first
delivered sample followed a fresh NEW_SESSION+START handshake, with no
power-cycle since) passes fresh_session=True/False. Exactly one sample is
excluded when fresh_session=True, per the firmware's documented one-sample
lag -- never a value-based guess.

Diagnostic-only: this never touches capture_quality_metrics.py's frozen
cardiac_snr_db computation or any production QA threshold. The raw window
(including the placeholder) is still what gets recorded and what the frozen
formula runs on; this module produces an ADDITIONAL, separately labeled,
diagnostic corrected view alongside it.

Scope note / follow-up not implemented here: the real, permanent fix belongs
in firmware/window assembly -- capture one extra sample per fresh session so
the delivered window still contains exactly 3000 REAL measured pairs after
the known placeholder is dropped, rather than diagnostically discarding one
of the 3000 after the fact as this module does. That firmware/window-count
change is a separate, not-yet-planned piece of work; this module is a
diagnostic aid for existing captures in the meantime, not a replacement for
it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Exactly one leading placeholder sample per fresh session, per the firmware
# doc above -- a fixed, documented count, never inferred from sample values.
# Dropping a second sample is a separate, explicitly experimental ablation
# (see the earlier one-off diagnostic script); this module only ever
# excludes this fixed count of 1.
FRESH_SESSION_PLACEHOLDER_COUNT = 1

PLACEHOLDER_EXCLUSION_VERSION = "diagnostic-placeholder-exclusion-1.0.0"

PROVENANCE_FRESH_SESSION = "fresh_session_leading_placeholder"
PROVENANCE_NONE = "none"


class _ExcludedPlaceholderList(list):
    """Marker type for an already-corrected sample list. build_corrected_view()
    refuses input of this type so a caller cannot silently double-exclude by
    accidentally feeding a CorrectedWindow's own red/ir back in."""


@dataclass(frozen=True)
class CorrectedWindow:
    red: list[int]
    ir: list[int]
    excluded_count: int
    valid_sample_count: int
    metric_version: str
    provenance: str

    def to_dict(self) -> dict:
        return {
            "excluded_count": self.excluded_count,
            "valid_sample_count": self.valid_sample_count,
            "metric_version": self.metric_version,
            "provenance": self.provenance,
        }


def build_corrected_view(
    red: list[int], ir: list[int], *, fresh_session: bool
) -> CorrectedWindow:
    """Build a diagnostic corrected view of one capture window.

    fresh_session must be supplied by the caller from its own knowledge of
    the BLE handshake it issued (True only when this window's first
    delivered sample followed a NEW_SESSION+START with no power-cycle
    since) -- never inferred from red/ir's contents. When True, exactly
    FRESH_SESSION_PLACEHOLDER_COUNT (1) leading sample is excluded from
    both channels; when False, nothing is excluded (provenance says there
    is no confirmed placeholder in this window, e.g. a continuation window
    within an already-passed preflight)."""
    if isinstance(red, _ExcludedPlaceholderList) or isinstance(ir, _ExcludedPlaceholderList):
        raise ValueError(
            "build_corrected_view() called on an already-corrected view -- "
            "double exclusion blocked"
        )
    if len(red) != len(ir):
        raise ValueError(f"red/ir length mismatch: {len(red)} vs {len(ir)}")

    excluded = FRESH_SESSION_PLACEHOLDER_COUNT if fresh_session else 0
    if excluded >= len(red):
        raise ValueError(
            f"window of {len(red)} samples is too short to exclude "
            f"{excluded} confirmed placeholder sample(s)"
        )

    corrected_red = _ExcludedPlaceholderList(red[excluded:])
    corrected_ir = _ExcludedPlaceholderList(ir[excluded:])
    return CorrectedWindow(
        red=corrected_red,
        ir=corrected_ir,
        excluded_count=excluded,
        valid_sample_count=len(corrected_red),
        metric_version=PLACEHOLDER_EXCLUSION_VERSION,
        provenance=PROVENANCE_FRESH_SESSION if fresh_session else PROVENANCE_NONE,
    )
