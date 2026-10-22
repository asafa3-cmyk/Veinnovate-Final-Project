"""Stable runtime entry point for the Hardware/Jetson integration component.

Exposes each pipeline stage as a separate, independently testable operation:
  - VeinNetRuntime.load(...)                strict model load + hash/I-O verification
  - .assemble_and_preprocess(packets)       raw packet windows -> ppg/morphology (or ABSTAIN reason)
  - VeinNetRuntime.demographics(age, sex)   frozen age/sex -> 4-dim demographics vector
  - VeinNetRuntime.elapsed_segments(secs)   elapsed seconds -> elapsed_segments unit
  - .infer(...)                             the 7 preprocessed inputs -> personal_bp [B,2]

No import in this module or its dependencies references anything outside
this bundle. After the three integration_ready folders are placed as
siblings, the Hardware/Jetson component can `import veinnet_runtime` from
this bundle without any reference to the original research repository.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .onnx_loader import ModelContractError, load_model_contract, load_verified_session
from .preprocessing.calibration_timing import elapsed_seconds_to_segments
from .preprocessing.demographics import demographics_transform, demographics_transform_batch
from .preprocessing.max30102 import BLEPacket, assemble_packets, load_max30102_spec, preprocess

_OUTPUT_NAME = "personal_bp"

__all__ = ["VeinNetRuntime", "VeinNetRuntimeError", "BLEPacket"]


class VeinNetRuntimeError(RuntimeError):
    """Raised for input/output contract violations at the entry-point boundary."""


class VeinNetRuntime:
    def __init__(self, session, contract: dict[str, Any], max30102_spec: dict[str, Any]) -> None:
        self._session = session
        self._contract = contract
        self._max30102_spec = max30102_spec

    @classmethod
    def load(
        cls,
        model_path: str | Path | None = None,
        providers: tuple[str, ...] = ("CPUExecutionProvider",),
    ) -> "VeinNetRuntime":
        """Strict model load: verifies SHA-256, then loaded I/O against the contract."""
        contract = load_model_contract()
        session = load_verified_session(model_path=model_path, contract=contract, providers=providers)
        spec = load_max30102_spec()
        return cls(session, contract, spec)

    # -- preprocessing --------------------------------------------------

    def assemble_and_preprocess(self, packets: Sequence[BLEPacket]) -> dict[str, Any]:
        """Raw BLE packet windows -> {"accepted": True, "ppg", "morphology", "sqi"}
        or {"accepted": False, "reason": ...} if the reading is rejected.
        """
        assembled = assemble_packets(list(packets), self._max30102_spec)
        if not assembled["accepted"]:
            return assembled
        return preprocess(assembled["red"], assembled["ir"], assembled["sample_rate_hz"], self._max30102_spec)

    @staticmethod
    def demographics(age: float | int | None, sex: str | None) -> np.ndarray:
        return demographics_transform(age, sex)

    @staticmethod
    def demographics_batch(rows: list[tuple[float | int | None, str | None]]) -> np.ndarray:
        return demographics_transform_batch(rows)

    @staticmethod
    def elapsed_segments(elapsed_seconds: float | int) -> float:
        return elapsed_seconds_to_segments(elapsed_seconds)

    # -- inference --------------------------------------------------------

    def infer(
        self,
        ppg: np.ndarray,
        morphology: np.ndarray,
        baseline_ppg: np.ndarray,
        baseline_morphology: np.ndarray,
        demographics: np.ndarray,
        baseline_cuff: np.ndarray,
        elapsed_segments: np.ndarray,
    ) -> np.ndarray:
        """Runs the frozen ONNX ensemble on the 7 preprocessed inputs.

        All 7 inputs are validated as float32 with the exact names, ranks,
        and shapes from contracts/model_contract.json before ONNX execution.
        The output is validated as a finite float32 [B,2] array before return.
        """
        inputs = {
            "ppg": np.asarray(ppg),
            "morphology": np.asarray(morphology),
            "baseline_ppg": np.asarray(baseline_ppg),
            "baseline_morphology": np.asarray(baseline_morphology),
            "demographics": np.asarray(demographics),
            "baseline_cuff": np.asarray(baseline_cuff),
            "elapsed_segments": np.asarray(elapsed_segments),
        }
        self._validate_inputs(inputs)
        outputs = self._session.run([_OUTPUT_NAME], inputs)
        result = outputs[0]
        self._validate_output(result)
        return result

    # -- validation --------------------------------------------------------

    def _validate_inputs(self, inputs: dict[str, np.ndarray]) -> None:
        contract_by_name = {spec["name"]: spec for spec in self._contract["inputs"]}
        missing = set(contract_by_name) - set(inputs)
        if missing:
            raise VeinNetRuntimeError(f"Missing required inputs: {sorted(missing)}")
        extra = set(inputs) - set(contract_by_name)
        if extra:
            raise VeinNetRuntimeError(f"Unexpected inputs: {sorted(extra)}")

        batch: int | None = None
        for name, array in inputs.items():
            spec = contract_by_name[name]
            if array.dtype != np.float32:
                raise VeinNetRuntimeError(f"Input '{name}' must be float32; got {array.dtype}")
            expected_shape = spec["shape"]
            if array.ndim != len(expected_shape):
                raise VeinNetRuntimeError(
                    f"Input '{name}' rank mismatch: expected {len(expected_shape)} dims "
                    f"{expected_shape}, got shape {array.shape}"
                )
            for axis, (actual_dim, expected_dim) in enumerate(zip(array.shape, expected_shape)):
                if expected_dim == "batch":
                    if batch is None:
                        batch = actual_dim
                    elif actual_dim != batch:
                        raise VeinNetRuntimeError(
                            f"Input '{name}' batch dimension {actual_dim} does not match "
                            f"other inputs' batch dimension {batch}"
                        )
                    continue
                if actual_dim != expected_dim:
                    raise VeinNetRuntimeError(
                        f"Input '{name}' shape mismatch at axis {axis}: expected {expected_dim}, "
                        f"got {actual_dim} (full shape {array.shape})"
                    )

    def _validate_output(self, output: np.ndarray) -> None:
        contract_out = self._contract["outputs"][0]
        if output.dtype != np.float32:
            raise VeinNetRuntimeError(f"Output must be float32; got {output.dtype}")
        expected_shape = contract_out["shape"]
        if output.ndim != len(expected_shape):
            raise VeinNetRuntimeError(
                f"Output rank mismatch: expected {expected_shape}, got shape {output.shape}"
            )
        for axis, (actual_dim, expected_dim) in enumerate(zip(output.shape, expected_shape)):
            if expected_dim == "batch":
                continue
            if actual_dim != expected_dim:
                raise VeinNetRuntimeError(
                    f"Output shape mismatch at axis {axis}: expected {expected_dim}, got {actual_dim}"
                )
        if not np.isfinite(output).all():
            raise VeinNetRuntimeError("Output contains non-finite values")
