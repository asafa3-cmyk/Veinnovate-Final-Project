"""model_runtime.py — thin protocol/data-container definitions only.

Ownership correction (E2E hardening pass): this module previously contained
its own independent ONNX loader (`OnnxModelRuntime`, using onnxruntime/onnx/
numpy directly) that duplicated exactly what the sibling
VeinNet_integration_ready component's `veinnet_runtime.onnx_loader` already
does — hash verification, opset check, I/O contract check, then an
onnxruntime session. Two independently-pinned runtimes loading the same
ONNX file is a real correctness and dependency-conflict risk (Hardware
previously pinned numpy==2.3.5/onnxruntime==1.20.1 while the sibling pins
numpy==2.4.6/onnxruntime==1.29.0), so that loader has been removed.

Final production model loading, hash verification, I/O validation,
preprocessing, and inference are owned entirely by
`veinnet_sibling_adapter.py`, which wraps the sibling's
`veinnet_runtime.VeinNetRuntime.load()`/`.infer()`. This module now only
defines the small data containers and Protocol that adapter (production) and
`MockModelRuntime` (test-only) both implement, so EdgeOrchestrator's calling
code never has to know which one it's talking to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ModelRuntimeUnavailable(RuntimeError):
    """Raised when the model cannot be loaded/verified/run. Callers must
    treat this as fatal for production inference — never substitute a mock
    or a stale cached model."""


@dataclass
class ModelInputs:
    """The frozen seven-input contract (config/inference_manifest.json),
    already preprocessed — never raw samples."""

    ppg: list  # [3][1000] float32
    morphology: list  # [13] float32
    baseline_ppg: list  # [3][1000] float32
    baseline_morphology: list  # [13] float32
    demographics: list  # [4] float32, order = [age_z, is_female, age_missing, sex_missing]
    baseline_cuff: list  # [2] float32 = [SBP, DBP]
    elapsed_segments: list  # [1] float32


@dataclass
class ModelOutputs:
    sbp: float
    dbp: float


class ModelRuntime(Protocol):
    """Interface any inference backend (the real sibling adapter, or a test
    double) must satisfy."""

    def predict(self, inputs: ModelInputs) -> ModelOutputs: ...

    @property
    def provenance(self) -> dict: ...


class MockModelRuntime:
    """Explicit test double — NEVER used unless a caller opts in by name.
    Returns a deterministic function of baseline_cuff so orchestration
    plumbing tests can assert on predictable output without any real
    weights or the sibling package installed. Must never be reachable from
    production startup code (see model_input_builder.require_production_builder,
    which refuses anything with is_test_only=True — the analogous guard for
    a ModelRuntime lives in veinnet_edge_service.py's production wiring,
    which only ever constructs the real adapter)."""

    def __init__(self, sbp_offset: float = 0.0, dbp_offset: float = 0.0) -> None:
        self._sbp_offset = sbp_offset
        self._dbp_offset = dbp_offset

    def predict(self, inputs: ModelInputs) -> ModelOutputs:
        sbp = inputs.baseline_cuff[0] + self._sbp_offset
        dbp = inputs.baseline_cuff[1] + self._dbp_offset
        return ModelOutputs(sbp=sbp, dbp=dbp)

    @property
    def provenance(self) -> dict:
        return {
            "model_name": "MOCK_NOT_A_REAL_MODEL",
            "model_version": None,
            "model_sha256": "MOCK_NOT_A_REAL_MODEL",
            "onnx_opset": None,
        }
