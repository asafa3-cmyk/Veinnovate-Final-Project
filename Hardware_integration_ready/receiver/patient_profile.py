"""patient_profile.py — the pairing-time patient identity/demographics record
(owner-authorized POC scope change, 2026-09-29 — see AGENTS.md "Pairing-time
patient identity and demographics").

Pairing is now the single source of truth for `age_years`/`sex`, the two
VeinNet demographics inputs (VeinNet_integration_ready/contracts/
demographics_contract.json). `/v1/calibration` reads them from the active
session's profile and rejects any caller-supplied value that disagrees.

Storage: only inside `patient_assignment.PatientAssignmentStore`'s own
0700/0600 SQLite file — never the adapter inventory, never the vitals
result schema or WebSocket stream, never logs or fixtures. Nothing in this
module logs or formats a profile into an exception message: every
rejection carries a fixed reason code and a field-free message.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

# Same bound the VeinNet demographics runtime guard enforces
# (demographics_contract.json "age_plausible_range").
AGE_MIN_YEARS = 0.0
AGE_MAX_YEARS = 130.0
NAME_MAX_LEN = 100
LOCATION_MAX_LEN = 50


class ProfileReason:
    """Stable, versioned API surface (identity contract 1.1.0) — the
    dashboard matches on these; never respell without a version bump."""

    PROFILE_FIELD_MISSING = "PROFILE_FIELD_MISSING"
    PROFILE_FIELD_INVALID = "PROFILE_FIELD_INVALID"
    NATIONAL_ID_INVALID = "NATIONAL_ID_INVALID"
    AGE_OUT_OF_RANGE = "AGE_OUT_OF_RANGE"
    SEX_INVALID = "SEX_INVALID"


class ProfileInvalid(ValueError):
    def __init__(self, reason_code: str, field: str) -> None:
        # The field *name* only — never its value (PHI must not reach logs).
        super().__init__(f"{reason_code}: {field}")
        self.reason_code = reason_code
        self.field = field


@dataclass(frozen=True)
class PatientProfile:
    first_name: str
    last_name: str
    national_id: str
    age_years: float
    sex: str  # exactly "F" or "M" (calibration_store.py convention)
    department: str | None
    bed: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def israeli_id_valid(value: str) -> bool:
    """Teudat Zehut check digit: up to 9 digits, left-padded with zeros;
    weights alternate 1,2; two-digit products have their digits summed."""
    if not value.isdigit() or not 1 <= len(value) <= 9:
        return False
    total = 0
    for i, ch in enumerate(value.zfill(9)):
        v = int(ch) * (1 if i % 2 == 0 else 2)
        total += v - 9 if v > 9 else v
    return total % 10 == 0 and int(value) != 0


def _text(raw: object, field: str, *, required: bool, max_len: int) -> str | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise ProfileInvalid(ProfileReason.PROFILE_FIELD_MISSING, field)
        return None
    if not isinstance(raw, str):
        raise ProfileInvalid(ProfileReason.PROFILE_FIELD_INVALID, field)
    value = raw.strip()
    if len(value) > max_len or any(ord(c) < 32 for c in value):
        raise ProfileInvalid(ProfileReason.PROFILE_FIELD_INVALID, field)
    return value


def validate_profile(
    *,
    first_name: object,
    last_name: object,
    national_id: object,
    age_years: object,
    sex: object,
    department: object = None,
    bed: object = None,
) -> PatientProfile:
    """Server-side validation. Name, national ID, age and sex are required;
    department/bed are optional ward-location labels."""
    first = _text(first_name, "first_name", required=True, max_len=NAME_MAX_LEN)
    last = _text(last_name, "last_name", required=True, max_len=NAME_MAX_LEN)

    nid = _text(national_id, "national_id", required=True, max_len=9)
    if not israeli_id_valid(nid):  # type: ignore[arg-type]
        raise ProfileInvalid(ProfileReason.NATIONAL_ID_INVALID, "national_id")

    if age_years is None:
        raise ProfileInvalid(ProfileReason.PROFILE_FIELD_MISSING, "age_years")
    if isinstance(age_years, bool) or not isinstance(age_years, (int, float)) or not math.isfinite(age_years):
        raise ProfileInvalid(ProfileReason.PROFILE_FIELD_INVALID, "age_years")
    if not AGE_MIN_YEARS <= float(age_years) <= AGE_MAX_YEARS:
        raise ProfileInvalid(ProfileReason.AGE_OUT_OF_RANGE, "age_years")

    if sex is None:
        raise ProfileInvalid(ProfileReason.PROFILE_FIELD_MISSING, "sex")
    if sex not in ("F", "M"):
        raise ProfileInvalid(ProfileReason.SEX_INVALID, "sex")

    return PatientProfile(
        first_name=first,  # type: ignore[arg-type]
        last_name=last,  # type: ignore[arg-type]
        national_id=nid,  # type: ignore[arg-type]
        age_years=float(age_years),
        sex=sex,  # type: ignore[arg-type]
        department=_text(department, "department", required=False, max_len=LOCATION_MAX_LEN),
        bed=_text(bed, "bed", required=False, max_len=LOCATION_MAX_LEN),
    )
