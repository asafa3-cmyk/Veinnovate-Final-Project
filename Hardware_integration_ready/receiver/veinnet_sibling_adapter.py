"""veinnet_sibling_adapter.py — the real, production ModelInputBuilder +
ModelRuntime implementation, wrapping the sibling VeinNet_integration_ready
component's public API (`veinnet_runtime`).

This is the ONLY class Hardware production code should ever use to reach
the model: it owns nothing about the ONNX file itself (no independent
hash/opset/I-O verification, no separate onnxruntime session) — all of that
is delegated to `veinnet_runtime.VeinNetRuntime.load()`/`.infer()`, which
performs it authoritatively (see model_runtime.py's module docstring for
why the earlier independent `OnnxModelRuntime` was removed).

Installation contract (matches VeinNet_integration_ready/pyproject.toml
exactly — this bundle does not restate or relax it):

    python -m pip install -e ../VeinNet_integration_ready

`import veinnet_runtime` is deliberately NOT at module scope — importing
this module must never fail just because the sibling isn't installed
(tests import this file to exercise its pure-Python pieces, e.g. via the
loader's own error-path tests). The import happens inside
`VeinNetSiblingAdapter.__init__`, and any failure there — ImportError,
`ModelContractError`, or a `RuntimeError` from the sibling's own
`hash_verify.verify_file_hash` — propagates up and is wrapped by
`model_input_builder.load_input_builder()` into
`ModelInputBuilderUnavailable`, which `veinnet_edge_service.main()` treats
as fatal BEFORE any BLE acquisition starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from statistics import median

from model_input_builder import BuildOutcome, RawInputBundle, SqiOutcome
from model_runtime import ModelInputs, ModelOutputs
from window_assembler import AssembledWindow

import prototype_continuous as _proto

# MAX30102 FIFO samples are always 18-bit regardless of the configured nA
# full-scale range (that setting changes counts-per-nA, not the digital
# sample width) — see firmware/veinnet_sensor_v1/common/sensor_profiles.h.
_MAX30102_DIGITAL_FULL_SCALE = (1 << 18) - 1
_EFFECTIVE_SAMPLE_RATE_HZ = 100.0

# Development-only Z23 motion/contact heuristics. Five retained rest windows
# informed these bounds; no movement-labeled validation exists yet.
_DC_STEP_LIMIT_FRACTION = 0.03  # adjacent 1 s block medians / window DC
_DIFFERENCE_RMS_BURST_RATIO = 5.0  # 1 s / median 1 s sample-difference RMS


def _development_motion_reason(window: AssembledWindow) -> str | None:
    if len(window.red) != 3000 or len(window.ir) != 3000:
        return None  # frozen preprocessing owns the length rejection
    for samples in (window.red, window.ir):
        blocks = [samples[start:start + 100] for start in range(0, 3000, 100)]
        dc = median(samples)
        block_dc = [median(block) for block in blocks]
        if dc > 0 and any(
            abs(right - left) / dc > _DC_STEP_LIMIT_FRACTION
            for left, right in zip(block_dc, block_dc[1:])
        ):
            return "CONTACT_CHANGED"
        difference_rms = [
            sqrt(sum((right - left) ** 2 for left, right in zip(block, block[1:])) / 99)
            for block in blocks
        ]
        typical = median(difference_rms)
        if typical > 0 and max(difference_rms) / typical > _DIFFERENCE_RMS_BURST_RATIO:
            return "EXCESSIVE_MOTION"
    return None


@dataclass
class WindowPreprocessResult:
    """Result of running ONE raw window through the sibling's frozen
    assemble_and_preprocess(). Used both for the current-window path
    (build()) and for a calibration-capture baseline window (called
    directly by http_service.py's /api/v1 calibration endpoint, once, at
    capture time — the result is cached in the CalibrationRecord rather
    than recomputed on every subsequent inference)."""

    accepted: bool
    reason_code: str | None = None
    ppg_flat: list[float] | None = None  # flattened [3,1000], 3000 floats
    morphology: list[float] | None = None  # 13 floats
    red_ir_correlation: float | None = None
    red_pulsatility: float | None = None
    warnings: tuple = ()  # prototype-continuous mode only


class VeinNetSiblingAdapter:
    """Production ModelInputBuilder. is_test_only = False — this is real."""

    is_test_only = False

    def __init__(self) -> None:
        try:
            import veinnet_runtime  # type: ignore
            from veinnet_runtime.onnx_loader import load_model_contract  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "veinnet_runtime is not importable — install it with "
                "`python -m pip install -e ../VeinNet_integration_ready` before "
                "starting production hardware mode"
            ) from exc

        # VeinNetRuntime.load() performs the AUTHORITATIVE verification:
        # SHA-256 of the actual ONNX file bytes, then the loaded session's
        # real input/output names/ranks/shapes against
        # contracts/model_contract.json. Raises ModelContractError/
        # RuntimeError on any mismatch — propagated, not caught here.
        self._runtime = veinnet_runtime.VeinNetRuntime.load()
        self._veinnet_runtime_module = veinnet_runtime

        contract = load_model_contract()
        self._model_relative_path = contract["model"]["relative_path"]
        self._model_sha256 = contract["model"]["sha256"]
        self._model_opset = contract["model"]["opset"]

        # The sibling bundle's own version, read from ITS bundle_manifest.json
        # (resolved relative to the installed package's real on-disk
        # location — valid under an editable install, exactly as
        # VeinNet_integration_ready/pyproject.toml's installation-contract
        # comment documents).
        sibling_root = Path(veinnet_runtime.__file__).resolve().parent.parent
        self._sibling_bundle_version = self._read_sibling_bundle_version(sibling_root)
        self._preprocessing_contract_version = self._read_contract_version(
            sibling_root / "contracts" / "preprocessing_contract.json"
        )
        self._morphology_contract_version = self._read_contract_version(
            sibling_root / "contracts" / "morphology_contract.json"
        )

    @staticmethod
    def _read_sibling_bundle_version(sibling_root: Path) -> str | None:
        import json

        manifest_path = sibling_root / "bundle_manifest.json"
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text()).get("bundle_version")

    @staticmethod
    def _read_contract_version(path: Path) -> str | None:
        import json

        if not path.exists():
            return None
        return json.loads(path.read_text()).get("contract_version")

    # -- provenance -----------------------------------------------------

    @property
    def model_name(self) -> str:
        return Path(self._model_relative_path).stem

    @property
    def model_version(self) -> str | None:
        return self._sibling_bundle_version

    @property
    def model_sha256(self) -> str:
        return self._model_sha256

    @property
    def onnx_opset(self) -> int:
        return self._model_opset

    @property
    def preprocessing_version(self) -> str:
        return f"veinnet-max30102-preprocessing-contract-{self._preprocessing_contract_version or 'unknown'}"

    @property
    def morphology_version(self) -> str:
        return f"veinnet-morphology-contract-{self._morphology_contract_version or 'unknown'}"

    # -- preprocessing ----------------------------------------------------

    def _window_to_ble_packet(self, window: AssembledWindow):
        import numpy as np

        BLEPacket = self._veinnet_runtime_module.BLEPacket
        return BLEPacket(
            red=np.asarray(window.red, dtype=np.float64),
            ir=np.asarray(window.ir, dtype=np.float64),
            first_sample_index=window.first_sample_index,
            timestamp_seconds=0.0,  # not used by assemble_packets()/preprocess() decision logic
            sample_rate_hz=_EFFECTIVE_SAMPLE_RATE_HZ,
            led_current_red=0,
            led_current_ir=0,
            adc_range=_MAX30102_DIGITAL_FULL_SCALE,
            saturation=False,  # no separate hardware saturation flag in this bundle; the
                                # sibling's own clipping check runs on the real sample values
            packet_loss_counter=window.integrity.lost_samples,
            contact_changed=window.integrity.session_changed or window.integrity.config_changed,
            motion_detected=False,  # not implemented in this bundle — see README Known limitations
        )

    def preprocess_raw_window(self, window: AssembledWindow, role: str = "current") -> WindowPreprocessResult:
        """Runs ONE raw window through the sibling's frozen
        assemble_and_preprocess(). Used for the current window (via
        build()) and for a calibration baseline window (called directly by
        http_service.py at capture time)."""
        integrity = window.integrity
        if integrity.fifo_overflow_detected:
            return WindowPreprocessResult(accepted=False, reason_code="FIFO_OVERFLOW")
        if integrity.duplicate_samples or integrity.out_of_order_packets:
            return WindowPreprocessResult(accepted=False, reason_code="DUPLICATE_OR_REORDERED_SAMPLE")
        if integrity.lost_samples or integrity.received_samples != integrity.expected_samples:
            return WindowPreprocessResult(accepted=False, reason_code="PACKET_LOSS")
        if integrity.session_changed or integrity.config_changed:
            return WindowPreprocessResult(accepted=False, reason_code="CONTACT_CHANGED")
        motion_reason = _development_motion_reason(window)
        if _proto.enabled():
            return self._prototype_preprocess(window, role, motion_reason)
        if motion_reason is not None:
            return WindowPreprocessResult(accepted=False, reason_code=motion_reason)
        packet = self._window_to_ble_packet(window)
        result = self._runtime.assemble_and_preprocess([packet])
        if not result.get("accepted"):
            return WindowPreprocessResult(accepted=False, reason_code=result.get("reason", "UNKNOWN"))

        ppg = result["ppg"]  # numpy float32 [3,1000]
        morphology = result["morphology"]  # numpy float32 [13]
        sqi = result.get("sqi", {})
        return WindowPreprocessResult(
            accepted=True,
            ppg_flat=[float(v) for v in ppg.reshape(-1)],
            morphology=[float(v) for v in morphology.reshape(-1)],
            red_ir_correlation=sqi.get("red_ir_correlation"),
            red_pulsatility=sqi.get("red_pulsatility"),
        )

    def _prototype_preprocess(self, window: AssembledWindow, role: str, motion_reason: str | None) -> WindowPreprocessResult:
        """VEINNET_PROTOTYPE_CONTINUOUS=1 path. Same frozen assemble_packets()/
        preprocess() code; only the two RED thresholds are relaxed (RED is not a
        model input) and the 1-s motion/contact gate becomes a warning for the
        current window. A calibration baseline keeps motion + IR-pulsatility checks."""
        from veinnet_runtime.preprocessing.max30102 import assemble_packets, preprocess

        spec = self._runtime._max30102_spec
        if role == "baseline":
            code, _internal = _proto.baseline_rejection(window.red, window.ir, motion_reason)
            if code is not None:
                return WindowPreprocessResult(accepted=False, reason_code=code)
        assembled = assemble_packets([self._window_to_ble_packet(window)], spec)
        if not assembled.get("accepted"):
            return WindowPreprocessResult(accepted=False, reason_code=assembled.get("reason", "UNKNOWN"))
        result = preprocess(assembled["red"], assembled["ir"], assembled["sample_rate_hz"], _proto.relaxed_spec(spec))
        if not result.get("accepted"):
            return WindowPreprocessResult(accepted=False, reason_code=result.get("reason", "UNKNOWN"))
        warnings, quality = _proto.bp_warnings(window.red, window.ir, spec, motion_reason)
        return WindowPreprocessResult(
            accepted=True,
            ppg_flat=[float(v) for v in result["ppg"].reshape(-1)],
            morphology=[float(v) for v in result["morphology"].reshape(-1)],
            red_ir_correlation=quality["red_ir_correlation"],
            red_pulsatility=quality["red_pulsatility"],
            warnings=tuple(warnings),
        )

    def demographics(self, age_years: float | None, sex: str | None) -> list[float]:
        """Frozen sibling demographics transform — raw (age_years, sex) in,
        [age_z, is_female, age_missing, sex_missing] out. Raises ValueError/
        TypeError on invalid input (propagated to the caller, e.g. the
        calibration HTTP endpoint, which turns it into a 400)."""
        vector = self._runtime.demographics(age_years, sex)
        return [float(v) for v in vector]

    def elapsed_segments(self, elapsed_seconds: float) -> float:
        return float(self._runtime.elapsed_segments(elapsed_seconds))

    # -- ModelInputBuilder ------------------------------------------------

    def build(self, bundle: RawInputBundle) -> BuildOutcome:
        current = self.preprocess_raw_window(bundle.window)
        if not current.accepted:
            return BuildOutcome(sqi=SqiOutcome(accepted=False, reason_code=current.reason_code))

        record = bundle.calibration
        if not record.baseline_ppg_processed or not record.baseline_morphology:
            # Should be unreachable — calibration_store.capture() refuses to
            # store a record without these — but never silently proceed
            # with a degraded/empty baseline if it somehow happened.
            return BuildOutcome(sqi=SqiOutcome(accepted=False, reason_code="UNKNOWN"))

        demographics_vec = self.demographics(bundle.age_years, bundle.sex)
        elapsed = self.elapsed_segments(bundle.elapsed_seconds)

        ppg_3x1000 = _reshape_3x1000(current.ppg_flat)
        baseline_ppg_3x1000 = _reshape_3x1000(record.baseline_ppg_processed)

        inputs = ModelInputs(
            ppg=ppg_3x1000,
            morphology=current.morphology,
            baseline_ppg=baseline_ppg_3x1000,
            baseline_morphology=record.baseline_morphology,
            demographics=demographics_vec,
            baseline_cuff=[record.baseline_cuff_sbp, record.baseline_cuff_dbp],
            elapsed_segments=[elapsed],
        )
        return BuildOutcome(
            sqi=SqiOutcome(
                accepted=True,
                red_ir_correlation=current.red_ir_correlation,
                red_pulsatility=current.red_pulsatility,
                warnings=tuple(current.warnings or ()),
            ),
            inputs=inputs,
        )

    # -- ModelRuntime -------------------------------------------------------

    def predict(self, inputs: ModelInputs) -> ModelOutputs:
        import numpy as np

        output = self._runtime.infer(
            ppg=np.asarray([inputs.ppg], dtype=np.float32),
            morphology=np.asarray([inputs.morphology], dtype=np.float32),
            baseline_ppg=np.asarray([inputs.baseline_ppg], dtype=np.float32),
            baseline_morphology=np.asarray([inputs.baseline_morphology], dtype=np.float32),
            demographics=np.asarray([inputs.demographics], dtype=np.float32),
            baseline_cuff=np.asarray([inputs.baseline_cuff], dtype=np.float32),
            elapsed_segments=np.asarray([inputs.elapsed_segments], dtype=np.float32),
        )
        return ModelOutputs(sbp=float(output[0][0]), dbp=float(output[0][1]))

    @property
    def provenance(self) -> dict:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_sha256": self.model_sha256,
            "onnx_opset": self.onnx_opset,
        }


def _reshape_3x1000(flat: list[float]) -> list[list[float]]:
    if len(flat) != 3000:
        raise ValueError(f"expected 3000 flattened PPG values, got {len(flat)}")
    return [flat[i * 1000 : (i + 1) * 1000] for i in range(3)]
