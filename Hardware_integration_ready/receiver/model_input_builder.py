"""model_input_builder.py — the injectable interface between raw acquired
windows and the frozen seven-input VeinNet model contract.

This bundle does NOT implement the frozen preprocessing (band-pass,
resample, robust-MAD normalization, 13-feature morphology extraction) —
that belongs to the sibling VeinNet_integration_ready component
(veinnet_sibling_adapter.py wraps its `veinnet_runtime.VeinNetRuntime`).
What this module provides is the *contract* a preprocessing implementation
must satisfy to be pluggable into receiver/veinnet_edge_service.py without
editing the orchestrator.

`build()` returns a `BuildOutcome`, not a bare `ModelInputs` — the sibling's
own `assemble_and_preprocess()` can legitimately REJECT a window (poor
contact, saturation, insufficient pulsatility, ...) as part of its frozen
SQI gating, and that rejection reason must reach the dashboard as one of
its closed `sqi.reason_codes` enum values (dashboard_api.SQI_REASON_CODES).
A bare exception could not carry that distinction from "hard failure,
INFERENCE_ERROR" cleanly.

Production startup requires a real implementation to be supplied via
`--input-builder module.path:ClassName` (loaded with importlib) BEFORE BLE
acquisition starts — see veinnet_edge_service.main(). If an ONNX model path
is given but no compatible builder is supplied (or loading/validation
fails), startup fails immediately with a clear error, never waiting for a
completed window and crashing on the interface later.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Protocol

from calibration_store import CalibrationRecord
from model_runtime import ModelInputs
from window_assembler import AssembledWindow


class ModelInputBuilderUnavailable(RuntimeError):
    """Raised when an input-builder spec cannot be loaded or does not
    satisfy the ModelInputBuilder contract. Fatal at startup — never caught
    to fall back to a mock in production."""


@dataclass
class RawInputBundle:
    """Everything a real preprocessing implementation needs, gathered by the
    orchestrator from the current window and the active calibration record.
    Deliberately raw/untransformed — no filtering or normalization has been
    applied to any field here."""

    window: AssembledWindow
    calibration: CalibrationRecord
    age_years: float | None
    sex: str | None  # exactly "F", "M", or None — canonicalized upstream (see calibration_store.py)
    elapsed_seconds: float


@dataclass
class SqiOutcome:
    """The preprocessing implementation's own accept/reject decision and
    metrics. `reason_code`, when rejected, must be one of
    dashboard_api.SQI_REASON_CODES — build() implementations are expected to
    return the sibling's own reason strings, which already match that enum
    exactly (verified against veinnet_runtime.preprocessing.max30102)."""

    accepted: bool
    reason_code: str | None = None
    red_ir_correlation: float | None = None
    red_pulsatility: float | None = None
    # Prototype-continuous mode only (VEINNET_PROTOTYPE_CONTINUOUS=1): quality
    # conditions that no longer stop inference. Always empty in strict mode.
    warnings: tuple = ()


@dataclass
class BuildOutcome:
    sqi: SqiOutcome
    inputs: ModelInputs | None = None  # populated iff sqi.accepted is True

    def __post_init__(self) -> None:
        if self.sqi.accepted and self.inputs is None:
            raise ValueError("BuildOutcome.sqi.accepted=True requires inputs")
        if not self.sqi.accepted and self.inputs is not None:
            raise ValueError("BuildOutcome.sqi.accepted=False must not carry inputs")
        if not self.sqi.accepted and not self.sqi.reason_code:
            raise ValueError("BuildOutcome.sqi.accepted=False requires a reason_code")


class ModelInputBuilder(Protocol):
    """Contract every real (sibling-supplied) or test-only implementation
    must satisfy."""

    def build(self, bundle: RawInputBundle) -> BuildOutcome: ...

    @property
    def preprocessing_version(self) -> str: ...

    @property
    def morphology_version(self) -> str: ...

    @property
    def is_test_only(self) -> bool:
        """Must be True for any builder not intended for production use.
        Checked at production startup — a test-only builder can never be
        loaded when an ONNX model path is also given (see
        veinnet_edge_service.main())."""
        ...


def load_input_builder(spec: str) -> ModelInputBuilder:
    """Loads a ModelInputBuilder from a `module.path:ClassName` spec via
    importlib, instantiates it with no arguments, and validates it exposes
    the required interface — all before BLE acquisition starts. Raises
    ModelInputBuilderUnavailable on any failure; never returns a partial or
    guessed implementation."""
    if ":" not in spec:
        raise ModelInputBuilderUnavailable(
            f"invalid --input-builder spec {spec!r}; expected 'module.path:ClassName'"
        )
    module_name, class_name = spec.split(":", 1)

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ModelInputBuilderUnavailable(f"could not import module {module_name!r}: {exc}") from exc

    try:
        cls = getattr(module, class_name)
    except AttributeError as exc:
        raise ModelInputBuilderUnavailable(f"module {module_name!r} has no attribute {class_name!r}") from exc

    try:
        instance = cls()
    except Exception as exc:
        raise ModelInputBuilderUnavailable(f"failed to instantiate {spec!r}: {exc}") from exc

    required_attrs = ("build", "preprocessing_version", "morphology_version", "is_test_only")
    missing = [a for a in required_attrs if not hasattr(instance, a)]
    if missing:
        raise ModelInputBuilderUnavailable(
            f"{spec!r} does not implement ModelInputBuilder — missing {missing}"
        )

    return instance


def require_production_builder(spec: str | None) -> ModelInputBuilder:
    """The only sanctioned way to obtain a builder for production use.
    Fails closed: raises ModelInputBuilderUnavailable if spec is None, if
    loading fails, or if the loaded builder identifies itself as
    is_test_only=True (a mock/test builder can never satisfy a production
    startup path, even if explicitly named)."""
    if spec is None:
        raise ModelInputBuilderUnavailable(
            "an ONNX model path was given but no --input-builder was supplied; "
            "production inference requires a real preprocessing implementation "
            "from the sibling VeinNet_integration_ready component"
        )
    builder = load_input_builder(spec)
    if getattr(builder, "is_test_only", True):
        raise ModelInputBuilderUnavailable(
            f"{spec!r} is marked is_test_only=True and cannot be used for production inference"
        )
    return builder
