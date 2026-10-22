"""Frozen demographics feature transform.

This is a DIRECT IMPLEMENTATION of the frozen manifest contract recorded in
gate7_max30102_personaldelta/gemini_stage/demographics/MANIFEST.json
(feature_schema.contract + train_normalization). It is NOT training source
code copied verbatim -- no equivalent single-reading transform exists
upstream. See ../../contracts/demographics_contract.json for full provenance
and the approval record.

This module performs NO canonicalization of sex values: only the exact
strings "F" and "M" (or None) are accepted. A caller-side adapter may
canonicalize alternate spellings before calling demographics_transform();
that adapter is explicitly out of scope for this frozen module.
"""
from __future__ import annotations

import numpy as np

TRAIN_AGE_MEAN = 59.82504970178926
TRAIN_AGE_STD = 15.9430331873034  # ddof=1

FEATURE_NAMES = ("age_z", "is_female", "age_missing", "sex_missing")

CANONICAL_FEMALE = "F"
CANONICAL_MALE = "M"
_CANONICAL_SEX_VALUES = (CANONICAL_FEMALE, CANONICAL_MALE)

_MIN_PLAUSIBLE_AGE_YEARS = 0.0
_MAX_PLAUSIBLE_AGE_YEARS = 130.0


def demographics_transform(age: float | int | None, sex: str | None) -> np.ndarray:
    """Frozen runtime implementation of the demographics feature contract.

    age: patient age in years, or None if unknown/missing.
    sex: must be exactly "F", "M", or None. Any other value (including
         lowercase variants such as "female"/"male") raises ValueError.

    Returns a numpy.float32 array of shape (4,):
        [age_z, is_female, age_missing, sex_missing]
    """
    if age is not None:
        if isinstance(age, bool) or not isinstance(age, (int, float)):
            raise TypeError(f"age must be int, float, or None; got {type(age)!r}")
        age = float(age)
        if not np.isfinite(age):
            raise ValueError(f"age must be finite; got {age!r}")
        if age < _MIN_PLAUSIBLE_AGE_YEARS or age > _MAX_PLAUSIBLE_AGE_YEARS:
            raise ValueError(
                f"age out of plausible human range "
                f"[{_MIN_PLAUSIBLE_AGE_YEARS}, {_MAX_PLAUSIBLE_AGE_YEARS}]; got {age!r}"
            )

    if sex is not None and sex not in _CANONICAL_SEX_VALUES:
        raise ValueError(
            f"sex must be exactly {_CANONICAL_SEX_VALUES!r} or None; got {sex!r}. "
            "This frozen transform performs no canonicalization of alternate spellings."
        )

    if age is None:
        age_z = 0.0
        age_missing = 1.0
    else:
        age_z = (age - TRAIN_AGE_MEAN) / TRAIN_AGE_STD
        age_missing = 0.0

    is_female = 1.0 if sex == CANONICAL_FEMALE else 0.0
    sex_missing = 1.0 if sex is None else 0.0

    return np.array([age_z, is_female, age_missing, sex_missing], dtype=np.float32)


def demographics_transform_batch(rows: list[tuple[float | int | None, str | None]]) -> np.ndarray:
    """Deterministic batch wrapper: stacks demographics_transform() per row.

    rows: list of (age, sex) tuples, each validated identically to a scalar call.
    Returns numpy.float32 array of shape (len(rows), 4).
    """
    if not rows:
        raise ValueError("rows must be non-empty")
    return np.stack([demographics_transform(age, sex) for age, sex in rows], axis=0).astype(np.float32)
