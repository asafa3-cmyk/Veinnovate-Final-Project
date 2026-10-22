"""Strict, fail-closed ONNX Runtime session loader.

Verifies the model file's SHA-256 before it is ever loaded, then verifies
the loaded session's actual input/output names, ranks, and static
(non-batch) dimensions against contracts/model_contract.json before handing
back a usable session.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import onnxruntime as ort

from .hash_verify import verify_file_hash

_BUNDLE_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = _BUNDLE_ROOT / "contracts" / "model_contract.json"
_DEFAULT_MODEL_PATH = _BUNDLE_ROOT / "model" / "personaldelta_timeindependent_ensemble_42_43_44.onnx"

_ONNX_FLOAT32_TYPES = {"tensor(float)"}


class ModelContractError(RuntimeError):
    """Raised when a loaded ONNX model's actual I/O does not match the frozen contract."""


def load_model_contract() -> dict[str, Any]:
    return json.loads(_CONTRACT_PATH.read_text())


def load_verified_session(
    model_path: str | Path | None = None,
    contract: dict[str, Any] | None = None,
    providers: tuple[str, ...] = ("CPUExecutionProvider",),
) -> ort.InferenceSession:
    contract = contract or load_model_contract()
    path = Path(model_path) if model_path is not None else _DEFAULT_MODEL_PATH
    expected_sha256 = contract["model"]["sha256"]
    verify_file_hash(path, expected_sha256)

    session = ort.InferenceSession(str(path), providers=list(providers))

    actual_inputs = {node.name: node for node in session.get_inputs()}
    expected_input_names = {spec["name"] for spec in contract["inputs"]}
    if set(actual_inputs) != expected_input_names:
        raise ModelContractError(
            f"Model input set {sorted(actual_inputs)} does not match contract "
            f"{sorted(expected_input_names)}"
        )
    for spec in contract["inputs"]:
        node = actual_inputs[spec["name"]]
        _check_shape(spec["name"], list(node.shape), spec["shape"])
        _check_dtype(spec["name"], node.type, spec["dtype"])

    actual_outputs = {node.name: node for node in session.get_outputs()}
    expected_output_names = {spec["name"] for spec in contract["outputs"]}
    if set(actual_outputs) != expected_output_names:
        raise ModelContractError(
            f"Model output set {sorted(actual_outputs)} does not match contract "
            f"{sorted(expected_output_names)}"
        )
    for spec in contract["outputs"]:
        node = actual_outputs[spec["name"]]
        _check_shape(spec["name"], list(node.shape), spec["shape"])
        _check_dtype(spec["name"], node.type, spec["dtype"])

    return session


def _check_shape(name: str, actual_shape: list, expected_shape: list) -> None:
    if len(actual_shape) != len(expected_shape):
        raise ModelContractError(
            f"Rank mismatch for '{name}': model={actual_shape}, contract={expected_shape}"
        )
    for actual_dim, expected_dim in zip(actual_shape, expected_shape):
        if expected_dim == "batch":
            continue  # dynamic batch axis: any symbolic name or int is accepted
        if actual_dim != expected_dim:
            raise ModelContractError(
                f"Shape mismatch for '{name}': model={actual_shape}, contract={expected_shape}"
            )


def _check_dtype(name: str, actual_type: str, expected_dtype: str) -> None:
    if expected_dtype == "float32" and actual_type not in _ONNX_FLOAT32_TYPES:
        raise ModelContractError(
            f"Dtype mismatch for '{name}': model={actual_type}, contract={expected_dtype}"
        )
