"""veinnet_edge_service.py — Jetson-side orchestration entry point.

Wires together: BLE central (ble_central.BleCentralController, real hardware
via bleak_adapter, or the deterministic simulator with --simulate) ->
protocol_decoder -> window_assembler -> the production model adapter
(veinnet_sibling_adapter.VeinNetSiblingAdapter, which owns BOTH frozen
preprocessing AND inference) -> dashboard_api's frozen vitals_result schema,
and exposes the HTTP/WebSocket network boundary (http_service.py) for
VeinoGate_Dashboard_integration_ready.

Production startup order matters and is enforced here, not discovered at
runtime:
  1. Require --patient-id (no placeholder default).
  2. Real hardware/production mode (i.e. NOT --simulate) REQUIRES the full
     model pipeline: resolve+verify the sibling model artifact
     (sibling_resolver), then construct the production adapter
     (--input-builder, which must be a real, non-test-only implementation —
     see model_input_builder.require_production_builder). Any failure here
     is fatal BEFORE any BLE connection is attempted.
  3. --simulate MAY run without the model (useful for protocol/window/SQI
     testing) — but a simulate run without a model can only ever produce
     INFERENCE_ERROR/RECALIBRATE/ABSTAIN, never VALID.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
import uuid
from ble_identity import canonical_uid
from pathlib import Path

from calibration_store import CalibrationStatus, CalibrationStore
from dashboard_api import (
    BpInfo,
    CalibrationInfo,
    ConnectionInfo,
    ConnectionStatus,
    HrInfo,
    ResultState,
    SqiInfo,
    SqiMetrics,
    ProvenanceInfo,
    build_result,
    map_calibration_status,
    normalize_sqi_reason_code,
    to_iso_utc,
)
from model_input_builder import ModelInputBuilderUnavailable, RawInputBundle, require_production_builder
from model_runtime import ModelRuntimeUnavailable
from pulse_rate import PulseRateEstimate, estimate_pulse_rate
from protocol_decoder import ProtocolError, StatusPacket, decode_data_packet, decode_status_packet
from sibling_resolver import SiblingResolutionError, load_sibling_paths_config, resolve_model_artifact
from window_assembler import WINDOW_SAMPLE_COUNT, SessionRingBuffer
import prototype_continuous as _proto

logger = logging.getLogger("veinnet.edge_service")

BLE_PROTOCOL_VERSION_STR = "ble-protocol-v3"
CALIBRATION_PROTOCOL_VERSION = "hardware-calibration-protocol-v1.0.0"
SENSOR_CONFIG_VERSION = "hardware-sensor-config-v1.0.0"

BUNDLE_ROOT = Path(__file__).resolve().parent.parent


class ProductionStartupError(RuntimeError):
    """Raised when production startup preconditions are not met. Callers
    must exit before any BLE acquisition begins — never start acquisition
    and fail later on a completed window."""


class EdgeOrchestrator:
    """Owns the raw-acquisition -> preprocessing/SQI -> calibration ->
    inference -> dashboard-result pipeline for one patient/device pairing.
    Does not own the BLE transport itself (see ble_central.BleCentralController)
    or the HTTP/WebSocket transport (see http_service.py) — both call into
    this class's public methods.

    `model_adapter`, when not None, must implement BOTH the
    model_input_builder.ModelInputBuilder protocol (build(), preprocessing_version,
    morphology_version, is_test_only) AND model_runtime.ModelRuntime
    (predict(), provenance) — veinnet_sibling_adapter.VeinNetSiblingAdapter
    is the only production implementation of this combined shape."""

    def __init__(
        self,
        *,
        model_adapter,
        calibration_store: CalibrationStore,
        patient_id: str,
        expected_hardware_uid: str | None = None,
        short_code: str | None = None,
        logical_session_id: str | None = None,
    ) -> None:
        if not patient_id:
            raise ProductionStartupError(
                "patient_id is required — production must not default to a placeholder identity"
            )

        self.model_adapter = model_adapter
        self.calibration_store = calibration_store
        self.patient_id = patient_id
        self.expected_hardware_uid = expected_hardware_uid
        self.short_code = short_code
        self.logical_session_id = logical_session_id

        self.ring = SessionRingBuffer()
        self._next_window_start = 0
        self.packet_notifications = 0
        self.packet_decode_errors = 0
        self.packet_identity_rejections = 0
        self.packet_decoded = 0
        self.samples_decoded = 0
        self.session_rollovers = 0
        self.disconnect_events = 0
        self.completed_windows = 0
        self._window_first_notification_monotonic: float | None = None
        self.last_window_receive_duration_s: float | None = None
        self._device_id: str | None = short_code
        self._current_session_id: int | None = None
        self.connection_status: str = "DISCONNECTED"
        self.latest_result = None  # last VitalsResult, for the /api/v1 snapshot endpoint
        self.last_window = None  # last AssembledWindow (regardless of SQI outcome), for calibration capture
        self.last_pulse_rate = PulseRateEstimate(None, "NO_WINDOW")  # research candidate; never published before validation
        self.last_status: StatusPacket | None = None
        self.last_status_at_utc: float | None = None
        self.last_status_monotonic: float | None = None
        self.last_communication_at_utc = None
        self.baseline_interval = (None, None)
        self._window_first_notification_utc = None
        self.baseline_listeners = []
        self.final_verified_window = None  # fixed-25 window whose config FINAL v2 bound
        self.result_listeners: list = []  # callables(VitalsResult) -> Awaitable[None], set by http_service

    def _device_id_str(self, raw_device_id: int) -> str:
        if self.short_code is not None:
            return self.short_code
        return f"veinnet-sensor-{raw_device_id}"

    def _result_session_id(self, sensor_session_id: int | None) -> str:
        if self.logical_session_id is not None:
            return self.logical_session_id
        return str(sensor_session_id) if sensor_session_id is not None else "n/a"

    def calibration_device_key(self, raw_device_id: int | None = None) -> str:
        """Internal calibration binding uses immutable UID, not display code."""
        if self.expected_hardware_uid is not None:
            return self.expected_hardware_uid
        if raw_device_id is not None:
            return self._device_id_str(raw_device_id)
        if self._device_id is None:
            raise ProductionStartupError("calibration device identity is unavailable")
        return self._device_id

    async def set_connection_status(self, status: str) -> None:
        if status == "CONNECTED" and getattr(self, "invalidate_calibration_on_restored_connect", False):
            # BleCentral reaches CONNECTED only after issuing NEW_SESSION.
            self.calibration_store.invalidate(self.patient_id, self.calibration_device_key(),
                                             "RUNTIME_RESTART_NEW_SENSOR_SESSION")
            self.invalidate_calibration_on_restored_connect = False
        self.connection_status = status

    async def handle_status_packet(self, raw: bytes) -> None:
        try:
            status = decode_status_packet(raw)
        except ProtocolError:
            return
        if self.expected_hardware_uid is not None:
            from ble_identity import canonical_uid
            if canonical_uid(status.device_id) != canonical_uid(self.expected_hardware_uid):
                return
        self.last_status = status
        self.last_status_at_utc = time.time()
        self.last_status_monotonic = time.monotonic()
        self.last_communication_at_utc = time.time()

    async def handle_raw_packet(self, raw: bytes) -> None:
        self.packet_notifications += 1
        try:
            packet = decode_data_packet(raw)
        except ProtocolError:
            self.packet_decode_errors += 1
            return  # corrupt/short packet — never repaired, just dropped

        if self.expected_hardware_uid is not None:
            from ble_identity import canonical_uid
            if canonical_uid(packet.device_id) != canonical_uid(self.expected_hardware_uid):
                self.packet_identity_rejections += 1
                return

        self.packet_decoded += 1
        self.last_communication_at_utc = time.time()
        if self._current_session_id is not None and packet.sensor_session_id != self._current_session_id:
            self.session_rollovers += 1
            self.ring.reset()
            self._next_window_start = 0
            self._window_first_notification_monotonic = None
            self.last_window = None
            self.last_status = None
            self.last_pulse_rate = PulseRateEstimate(None, "NO_WINDOW")  # never carry HR across sessions
        self._device_id = self._device_id_str(packet.device_id)
        self._current_session_id = packet.sensor_session_id
        self.samples_decoded += len(packet.red)
        if self._window_first_notification_monotonic is None:
            self._window_first_notification_monotonic = time.monotonic()
            self._window_first_notification_utc = time.time()

        self.ring.ingest(packet)
        window = self.ring.try_cut_window(self._next_window_start)
        if window is None:
            return
        self._next_window_start += WINDOW_SAMPLE_COUNT
        self.ring.prune_before(self._next_window_start)
        self.completed_windows += 1
        self.last_window_receive_duration_s = time.monotonic() - self._window_first_notification_monotonic
        self._window_first_notification_monotonic = None
        self.baseline_interval = (self._window_first_notification_utc, time.time())
        self._window_first_notification_utc = None
        await self._process_window(window)

    async def handle_stage10_verified_window(self, window, *, policy_id: str, policy_sha256: str) -> None:
        """Accept one exposure-gated real window into the existing pipeline.

        Stage 10 owns preflight and freezes exposure before constructing this
        exact 3000-pair window.  The existing SQI, calibration, inference, and
        dashboard emission path remains unchanged and is called only after
        transport integrity is rechecked here.
        """
        from stage10_z23_controller import EXPECTED_POLICY_SHA256

        if policy_id != "STAGE10-Z23-POC-EXPOSURE-v1" or policy_sha256 != EXPECTED_POLICY_SHA256:
            raise ProductionStartupError("unverified Stage 10 exposure policy")
        if not window.integrity.is_complete or len(window.red) != 3000 or len(window.ir) != 3000:
            raise ProductionStartupError("Stage 10 window integrity failed")
        if self.expected_hardware_uid is not None and canonical_uid(window.device_id) != canonical_uid(self.expected_hardware_uid):
            raise ProductionStartupError("Stage 10 window identity mismatch")
        self._device_id = self._device_id_str(window.device_id)
        self._current_session_id = window.sensor_session_id
        await self._process_window(window)

    async def handle_fixed25_verified_window(self, capture) -> None:
        """FIXED-PROFILE-25-DIAGNOSTIC-v1 entry: accept only a measurement
        window whose FINAL v2 witness was matched by the bench verifier.
        Contact is not independently established on this path (no no-contact
        reference); the frozen integrity/SQI gates in _process_window remain."""
        from ble_identity import canonical_uid
        from bench_capture_integrity import WarmupBenchCapture

        window, w = getattr(capture, "window", None), getattr(capture, "witness", None)
        if (
            not isinstance(capture, WarmupBenchCapture) or capture.result != "INTEGRITY_VERIFIED"
            or w is None or w.witness_version != 2 or window is None
            or not window.integrity.is_complete or len(window.red) != 3000 or len(window.ir) != 3000
            or window.device_id != capture.uid or w.hardware_uid != capture.uid
            or w.sensor_session_id != window.sensor_session_id
            or window.sensor_config_id != 25 or w.sensor_config_id != 25
        ):
            raise ProductionStartupError("fixed-profile-25 FINAL verification failed")
        if self.expected_hardware_uid is None or canonical_uid(window.device_id) != canonical_uid(self.expected_hardware_uid):
            raise ProductionStartupError("fixed-profile-25 window identity mismatch")
        # Config 25 is bound by the FINAL witness and window above. Firmware
        # closure clears the config and emits STATUS(config=0) just before
        # FINAL (VN-002 fingertip_warmup_02), so current STATUS cannot bind it.
        if self._current_session_id is not None and window.sensor_session_id != self._current_session_id:
            self.last_window = None  # superseded session: no baseline carries over
            self.last_pulse_rate = PulseRateEstimate(None, "NO_WINDOW")
        self._device_id = self._device_id_str(window.device_id)
        self._current_session_id = window.sensor_session_id
        self.final_verified_window = window
        await self._process_window(window)

    async def handle_capture_failure(self, reason: str) -> None:
        """A failed scheduled/forced attempt never serves a previous BP value."""
        self.last_window = None
        if self.connection_status != "CONNECTED":
            await self.handle_disconnected()
            return
        now = time.time()
        cal_status = self.calibration_store.status(
            self.patient_id, self.calibration_device_key(),
            current_sensor_session_id=self._current_session_id,
            current_sensor_config_id=self.last_status.sensor_config_id if self.last_status else None,
        )
        record = self.calibration_store.get(self.patient_id, self.calibration_device_key())
        result = build_result(
            reading_id=str(uuid.uuid4()), session_id=self._result_session_id(self._current_session_id),
            patient_id=self.patient_id, device_id=self._device_id or self.short_code or "unknown-device",
            acquisition_start_utc=now, acquisition_end_utc=now,
            result_state=ResultState.ABSTAIN, sbp_mmhg=None, dbp_mmhg=None,
            connection=self._connection_info(now),
            calibration=self._calibration_info(cal_status, record, reason=reason),
            sqi=SqiInfo(accepted=False, reason_codes=["UNKNOWN"], metrics=SqiMetrics()),
            provenance=self._provenance(),
        )
        await self._emit(result)

    async def handle_disconnected(self) -> None:
        """Called by ble_central on any disconnect (including a failed
        connect attempt). Emits a DEVICE_DISCONNECTED result rather than
        silently going quiet."""
        self.disconnect_events += 1
        self.ring.reset()
        self._next_window_start = 0
        self._window_first_notification_monotonic = None
        self.last_window = None
        self.baseline_interval = (None, None)
        self._window_first_notification_utc = None
        self.last_status = None
        self.last_pulse_rate = PulseRateEstimate(None, "NO_WINDOW")
        now = time.time()
        result = build_result(
            reading_id=str(uuid.uuid4()),
            session_id=self._result_session_id(self._current_session_id),
            patient_id=self.patient_id,
            device_id=self._device_id or "unknown-device",
            acquisition_start_utc=now,
            acquisition_end_utc=now,
            result_state=ResultState.DEVICE_DISCONNECTED,
            sbp_mmhg=None,
            dbp_mmhg=None,
            connection=ConnectionInfo(status=ConnectionStatus.DISCONNECTED, data_age_seconds=None),
            calibration=self._calibration_info(CalibrationStatus.MISSING, None, reason="device_disconnected"),
            sqi=SqiInfo(accepted=False, reason_codes=[], metrics=SqiMetrics()),
            provenance=self._provenance(),
        )
        await self._emit(result)

    def _calibration_info(self, status: CalibrationStatus, record, *, reason: str | None) -> CalibrationInfo:
        return CalibrationInfo(
            status=map_calibration_status(status),
            calibration_id=record.calibration_id if record else None,
            calibrated_at_utc=to_iso_utc(record.captured_at_utc) if record else None,
            warning_threshold_seconds=float(self.calibration_store.warning_after_s),
            expiry_threshold_seconds=float(self.calibration_store.hard_expiry_s),
            age_seconds=record.age_s() if record else None,
            reason=reason,
        )

    def _provenance(self) -> ProvenanceInfo:
        if self.model_adapter is None:
            return ProvenanceInfo(
                model_name=None, model_version=None, model_sha256=None, onnx_opset=None,
                preprocessing_version=None, morphology_version=None, sqi_version=None,
                calibration_protocol_version=CALIBRATION_PROTOCOL_VERSION,
                ble_protocol_version=BLE_PROTOCOL_VERSION_STR,
                sensor_config_version=SENSOR_CONFIG_VERSION,
            )
        return ProvenanceInfo(
            model_name=self.model_adapter.model_name,
            model_version=self.model_adapter.model_version,
            model_sha256=self.model_adapter.model_sha256,
            onnx_opset=self.model_adapter.onnx_opset,
            preprocessing_version=self.model_adapter.preprocessing_version,
            morphology_version=self.model_adapter.morphology_version,
            sqi_version=(f"{_proto.VERSION}+{self.model_adapter.preprocessing_version}" if _proto.enabled()
                         else self.model_adapter.preprocessing_version),  # SQI screening lives inside the same frozen preprocessing contract
            calibration_protocol_version=CALIBRATION_PROTOCOL_VERSION,
            ble_protocol_version=BLE_PROTOCOL_VERSION_STR,
            sensor_config_version=SENSOR_CONFIG_VERSION,
        )

    def _connection_info(self, acquisition_end_utc: float) -> ConnectionInfo:
        status = ConnectionStatus(self.connection_status) if self.connection_status in (
            "CONNECTED", "RECONNECTING", "DISCONNECTED"
        ) else ConnectionStatus.DISCONNECTED
        data_age = max(0.0, time.time() - acquisition_end_utc)
        return ConnectionInfo(status=status, data_age_seconds=data_age)

    async def _process_window(self, window) -> None:
        # Single production boundary: only an integrity-complete window may
        # become a calibration baseline (last_window) or reach HR/BP. The
        # fixed-profile-25 path verifies FINAL v2 before calling this; the
        # Stage-10 and raw-notification paths have no FINAL witness.
        integrity = window.integrity
        self.last_window = window if integrity.is_complete else None
        self.last_pulse_rate = PulseRateEstimate(None, "QUALITY_NOT_EVALUATED")
        reading_id = str(uuid.uuid4())
        now = time.time()
        start, end = self.baseline_interval
        self.baseline_interval = (None, None)
        acquisition_start = now - 30.0
        device_id = self._device_id_str(window.device_id)
        calibration_device_id = self.calibration_device_key(window.device_id)

        metrics = SqiMetrics(
            packet_loss_fraction=integrity.loss_fraction,
            duplicate_or_reordered_count=integrity.duplicate_samples + integrity.out_of_order_packets,
            overflow_count=integrity.fifo_overflow_count + integrity.software_overflow_count,
            sample_count=integrity.received_samples,
        )
        connection = self._connection_info(now)

        if not integrity.is_complete:
            self._update_pulse_rate(window, False)
            reason = ("FIFO_OVERFLOW" if integrity.fifo_overflow_detected
                      else "DUPLICATE_OR_REORDERED_SAMPLE" if integrity.duplicate_samples or integrity.out_of_order_packets
                      else "PACKET_LOSS")  # loss, software overflow, uncertainty or missing loss evidence
            await self._emit(
                build_result(
                    reading_id=reading_id, session_id=self._result_session_id(window.sensor_session_id),
                    patient_id=self.patient_id, device_id=device_id,
                    acquisition_start_utc=acquisition_start, acquisition_end_utc=now,
                    result_state=ResultState.ABSTAIN, sbp_mmhg=None, dbp_mmhg=None,
                    connection=connection, hr=self._hr_info(start, end),
                    calibration=self._calibration_info(CalibrationStatus.MISSING, None, reason="window_integrity_incomplete"),
                    sqi=SqiInfo(accepted=False, reason_codes=[reason], metrics=metrics),
                    provenance=self._provenance(),
                )
            )
            return

        from sqi_gate import evaluate_window
        if _proto.enabled():
            self._prototype_window_eval(window, reading_id)
        else:
            self._update_pulse_rate(window, evaluate_window(window).accepted)
        for listener in list(self.baseline_listeners):
            await listener(self, reading_id, window, start, end)

        if self.model_adapter is None:
            await self._emit(
                build_result(
                    reading_id=reading_id, session_id=self._result_session_id(window.sensor_session_id),
                    patient_id=self.patient_id, device_id=device_id,
                    acquisition_start_utc=acquisition_start, acquisition_end_utc=now,
                    result_state=ResultState.INFERENCE_ERROR, sbp_mmhg=None, dbp_mmhg=None,
                    connection=connection, hr=self._hr_info(start, end),
                    calibration=self._calibration_info(CalibrationStatus.MISSING, None, reason="no_model_adapter_loaded"),
                    sqi=SqiInfo(accepted=False, reason_codes=["UNKNOWN"], metrics=metrics),
                    provenance=self._provenance(),
                )
            )
            return

        cal_status = self.calibration_store.status(
            self.patient_id, calibration_device_id,
            # prototype mode: a BLE reconnect (new sensor_session_id) on the same
            # hardware/config keeps the calibration; config change still invalidates.
            current_sensor_session_id=None if _proto.enabled() else window.sensor_session_id,
            current_sensor_config_id=window.sensor_config_id,
        )
        record = self.calibration_store.get(self.patient_id, calibration_device_id)
        if cal_status not in (CalibrationStatus.VALID, CalibrationStatus.WARNING):
            await self._emit(
                build_result(
                    reading_id=reading_id, session_id=self._result_session_id(window.sensor_session_id),
                    patient_id=self.patient_id, device_id=device_id,
                    acquisition_start_utc=acquisition_start, acquisition_end_utc=now,
                    result_state=ResultState.RECALIBRATE, sbp_mmhg=None, dbp_mmhg=None,
                    connection=connection, hr=self._hr_info(start, end),
                    calibration=self._calibration_info(cal_status, record, reason=cal_status.value),
                    sqi=SqiInfo(accepted=False, reason_codes=[], metrics=metrics),
                    provenance=self._provenance(),
                )
            )
            return

        try:
            bundle = RawInputBundle(
                window=window,
                calibration=record,
                age_years=record.age_years,
                sex=record.sex,
                elapsed_seconds=record.age_s(now),
            )
            outcome = self.model_adapter.build(bundle)
        except Exception as exc:
            logger.warning("preprocessing failed for reading %s: %s", reading_id, exc)
            await self._emit(
                build_result(
                    reading_id=reading_id, session_id=self._result_session_id(window.sensor_session_id),
                    patient_id=self.patient_id, device_id=device_id,
                    acquisition_start_utc=acquisition_start, acquisition_end_utc=now,
                    result_state=ResultState.INFERENCE_ERROR, sbp_mmhg=None, dbp_mmhg=None,
                    connection=connection, hr=self._hr_info(start, end),
                    calibration=self._calibration_info(cal_status, record, reason=None),
                    sqi=SqiInfo(accepted=False, reason_codes=["UNKNOWN"], metrics=metrics),
                    provenance=self._provenance(),
                )
            )
            return

        if not outcome.sqi.accepted:
            metrics.red_ir_correlation = outcome.sqi.red_ir_correlation
            metrics.red_pulsatility = outcome.sqi.red_pulsatility
            await self._emit(
                build_result(
                    reading_id=reading_id, session_id=self._result_session_id(window.sensor_session_id),
                    patient_id=self.patient_id, device_id=device_id,
                    acquisition_start_utc=acquisition_start, acquisition_end_utc=now,
                    result_state=ResultState.ABSTAIN, sbp_mmhg=None, dbp_mmhg=None,
                    connection=connection, hr=self._hr_info(start, end),
                    calibration=self._calibration_info(cal_status, record, reason=None),
                    sqi=SqiInfo(
                        accepted=False,
                        reason_codes=[normalize_sqi_reason_code(outcome.sqi.reason_code or "UNKNOWN")],
                        metrics=metrics,
                    ),
                    provenance=self._provenance(),
                )
            )
            return


        try:
            outputs = self.model_adapter.predict(outcome.inputs)
        except (ModelRuntimeUnavailable, Exception) as exc:
            logger.warning("inference failed for reading %s: %s", reading_id, exc)
            await self._emit(
                build_result(
                    reading_id=reading_id, session_id=self._result_session_id(window.sensor_session_id),
                    patient_id=self.patient_id, device_id=device_id,
                    acquisition_start_utc=acquisition_start, acquisition_end_utc=now,
                    result_state=ResultState.INFERENCE_ERROR, sbp_mmhg=None, dbp_mmhg=None,
                    connection=connection, hr=self._hr_info(start, end),
                    calibration=self._calibration_info(cal_status, record, reason=None),
                    sqi=SqiInfo(accepted=False, reason_codes=["UNKNOWN"], metrics=metrics),
                    provenance=self._provenance(),
                )
            )
            return

        metrics.red_ir_correlation = outcome.sqi.red_ir_correlation
        metrics.red_pulsatility = outcome.sqi.red_pulsatility
        await self._emit(
            build_result(
                reading_id=reading_id, session_id=self._result_session_id(window.sensor_session_id),
                patient_id=self.patient_id, device_id=device_id,
                acquisition_start_utc=acquisition_start, acquisition_end_utc=now,
                result_state=ResultState.VALID, sbp_mmhg=outputs.sbp, dbp_mmhg=outputs.dbp,
                connection=connection, hr=self._hr_info(start, end),
                calibration=self._calibration_info(cal_status, record, reason=None),
                sqi=SqiInfo(accepted=True, metrics=metrics, reason_codes=[
                    c for c in getattr(outcome.sqi, "warnings", ()) if c in _proto.SCHEMA_WARNING_CODES]),
                provenance=self._provenance(),
            )
        )

    def _hr_info(self, start=None, end=None):
        from dashboard_api import HrInfo, to_iso_utc
        hr = self.last_pulse_rate
        valid = hr.bpm is not None and (hr.reason == "CANDIDATE" or (
            _proto.enabled() and str(hr.reason).startswith("PROTOTYPE_") and hr.reason != "PROTOTYPE_NO_SIGNAL"))
        return HrInfo(bpm=hr.bpm if valid else None, valid=valid,
            status="VALID" if valid else "ABSTAIN", reason=hr.reason,
            algorithm_name=hr.algorithm_name, algorithm_version=hr.algorithm_version,
            window_start_utc=to_iso_utc(start) if start is not None else None,
            window_end_utc=to_iso_utc(end) if end is not None else None)

    def _update_pulse_rate(self, window, sqi_accepted: bool) -> None:
        """HR estimate for this window only; failures leave HR unavailable, never block BP."""
        try:
            self.last_pulse_rate = estimate_pulse_rate(window, sqi_accepted=sqi_accepted)
        except Exception:
            self.last_pulse_rate = PulseRateEstimate(None, "ESTIMATOR_ERROR")

    def _prototype_window_eval(self, window, reading_id: str) -> None:
        """VEINNET_PROTOTYPE_CONTINUOUS=1: tiered IR HR + SpO2 for this window."""
        from dashboard_api import Spo2Info, Spo2Status
        self._proto_reading_id = reading_id
        self._proto_spo2 = None
        try:
            motion = _proto.motion_reason(window.red, window.ir)
            quality = _proto.channel_quality(window.red, window.ir)
            hr = _proto.tiered_hr(window.red, window.ir, motion=motion, ir_pulsatility=quality["ir_pulsatility"])
            self.last_pulse_rate = PulseRateEstimate(hr.bpm, hr.reason, hr.algorithm_name, hr.algorithm_version)
            self.last_prototype_hr = hr
        except Exception:
            self.last_pulse_rate = PulseRateEstimate(None, "ESTIMATOR_ERROR")
        try:
            record = self.calibration_store.get(self.patient_id, self.calibration_device_key(window.device_id))
            reference = getattr(record, "spo2_reference", None) if record is not None else None
            s = _proto.spo2(window.red, window.ir, reference=reference)
            self.last_prototype_spo2 = s
            self._proto_spo2 = (Spo2Info(pct=s.pct, status=Spo2Status.VALID, algorithm_name=s.algorithm_name,
                                         algorithm_version=f"{s.algorithm_version}:{s.mode}")
                                if s.status == "VALID" else Spo2Info(status=Spo2Status.INVALID))
        except Exception:
            self._proto_spo2 = None

    async def _emit(self, result) -> None:
        if (_proto.enabled() and getattr(self, "_proto_spo2", None) is not None
                and getattr(self, "_proto_reading_id", None) == getattr(result, "reading_id", None)):
            result.spo2 = self._proto_spo2
        self.latest_result = result
        for listener in list(self.result_listeners):
            await listener(result)
        if not self.result_listeners:
            print(result.to_json())


def _require_production_pipeline(
    *, production: bool, onnx_path_arg: str | None, input_builder_spec: str | None, sibling_config_path: Path,
):
    """Resolves and verifies the full model pipeline (sibling artifact +
    production adapter) BEFORE any BLE acquisition starts. Returns the
    constructed adapter, or None if not in production mode."""
    if not production:
        return None

    sibling_config = load_sibling_paths_config(sibling_config_path)
    if onnx_path_arg is None or onnx_path_arg == "AUTO":
        resolved = resolve_model_artifact(BUNDLE_ROOT, sibling_config)
        logger.info(
            "resolved model artifact from sibling %s@%s: %s",
            resolved.sibling_bundle_name, resolved.sibling_bundle_version, resolved.onnx_path,
        )
    # else: an explicit --onnx-path is accepted for logging/diagnostics only —
    # the production adapter resolves and loads the model itself via
    # veinnet_runtime.VeinNetRuntime.load() (its own default path resolution,
    # under the editable install), which is the sole authoritative loader.

    return require_production_builder(input_builder_spec)


async def run_simulated(orchestrator: EdgeOrchestrator, packets: list[bytes]) -> None:
    for raw in packets:
        await orchestrator.handle_raw_packet(raw)


async def run_hardware(orchestrator: EdgeOrchestrator, device_handle, identity, stop_event: asyncio.Event) -> None:
    """Run one already-resolved V2 path; never discovers/selects by name."""
    from bleak_adapter import connect_to_device
    from ble_central import BleCentralController

    controller = BleCentralController(
        device_handle=device_handle,
        expected_identity=identity,
        connector=connect_to_device,
        on_data=orchestrator.handle_raw_packet,
        on_status=orchestrator.handle_status_packet,
        on_disconnected=orchestrator.handle_disconnected,
        on_connection_status_change=orchestrator.set_connection_status,
    )
    await controller.run(stop_event)


def main() -> None:
    parser = argparse.ArgumentParser(description="VeinNet Jetson edge service")
    parser.add_argument(
        "--simulate", action="store_true",
        help="run against the deterministic simulator instead of real BLE hardware; "
             "may run without the model pipeline (see --production)",
    )
    parser.add_argument(
        "--production", action="store_true",
        help="require the full verified model pipeline (sibling artifact + real adapter) even "
             "under --simulate; real (non-simulate) hardware mode always implies this",
    )
    parser.add_argument(
        "--onnx-path", default=None,
        help="diagnostic only: logged path hint for the resolved model artifact. The production "
             "adapter always loads the model itself via veinnet_runtime.VeinNetRuntime.load()",
    )
    parser.add_argument(
        "--input-builder", default="veinnet_sibling_adapter:VeinNetSiblingAdapter",
        help="module.path:ClassName of the production adapter (default: the real sibling adapter)",
    )
    parser.add_argument("--patient-id", required=True, help="explicit patient identity — no placeholder default")
    parser.add_argument("--calibration-store", type=Path, default=Path("calibration_store.json"))
    parser.add_argument(
        "--sibling-config", type=Path,
        default=BUNDLE_ROOT / "config" / "sibling_paths.example.json",
        help="relative sibling-path configuration; defaults to the shipped sibling layout",
    )
    parser.add_argument("--http-host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--no-http", action="store_true", help="disable the HTTP/WebSocket service")
    parser.add_argument("--measurement-policy", type=Path, default=None,
                        help="Stage 16 timing policy override; hardware defaults to measurement_policy_v1.json")
    parser.add_argument(
        "--identity-db-dir", type=Path, default=None,
        help="enable the software-only POC adapter identity/assignment API "
             "(AGENTS.md items 2-6) with its SQLite stores under this directory; "
             "omitted by default so existing single-device deployments are unaffected",
    )
    parser.add_argument(
        "--fixed25-diagnostic", action="store_true",
        help="FIXED-PROFILE-25-DIAGNOSTIC-v1: operator-requested warm-up/FINAL v2 acquisitions at profile 25 "
             "instead of the Stage-10 scheduled policy; no no-contact reference check, not Stage-10 completion",
    )
    args = parser.parse_args()
    measurement_policy = None
    if args.fixed25_diagnostic:
        if args.simulate or args.measurement_policy is not None:
            raise ProductionStartupError("--fixed25-diagnostic requires hardware mode without --measurement-policy")
    elif not args.simulate:
        from measurement_policy import MeasurementPolicy
        measurement_policy = MeasurementPolicy.load(
            args.measurement_policy or BUNDLE_ROOT / "config" / "measurement_policy_v1.json")
    elif args.measurement_policy is not None:
        raise ProductionStartupError("scheduled BLE policy requires hardware assignment runtime")

    # Real hardware mode ALWAYS requires the full pipeline; --simulate may
    # opt in via --production for a stronger (still hardware-free) check.
    require_pipeline = args.production or not args.simulate

    try:
        model_adapter = _require_production_pipeline(
            production=require_pipeline, onnx_path_arg=args.onnx_path,
            input_builder_spec=args.input_builder if require_pipeline else None,
            sibling_config_path=args.sibling_config,
        )
    except (ModelRuntimeUnavailable, ModelInputBuilderUnavailable, SiblingResolutionError) as exc:
        print(f"FATAL: {exc}")
        raise SystemExit(1) from exc

    calibration_store = CalibrationStore(args.calibration_store)

    try:
        orchestrator = EdgeOrchestrator(
            model_adapter=model_adapter, calibration_store=calibration_store, patient_id=args.patient_id,
        )
    except ProductionStartupError as exc:
        print(f"FATAL: {exc}")
        raise SystemExit(1) from exc

    identity_service = None
    if args.identity_db_dir is not None:
        from identity_service import IdentityService

        identity_service = IdentityService.from_db_dir(args.identity_db_dir)

    async def _run():
        tasks = []
        if args.simulate:
            if not args.no_http:
                from http_service import run_http_service
                tasks.append(asyncio.create_task(run_http_service(
                    orchestrator, args.http_host, args.http_port, identity_service=identity_service
                )))
            from simulated_ble_source import golden_window_packets

            await run_simulated(orchestrator, golden_window_packets())
        else:
            if identity_service is None:
                raise ProductionStartupError("BLE V2 hardware mode requires --identity-db-dir")
            from bleak_adapter import connect_to_device, run_v2_discovery, verify_connected_identity
            from http_service import run_http_service
            from multi_device_runtime import MultiDeviceRuntime

            runtime = MultiDeviceRuntime(
                connector=connect_to_device,
                model_adapter_factory=lambda: type(model_adapter)(),
                calibration_store=calibration_store,
                measurement_policy=measurement_policy,
                fixed25_diagnostic=args.fixed25_diagnostic,
            )
            from functools import partial
            identity_service.connected_identity_verifier = partial(
                verify_connected_identity, live_discovery=identity_service.live_discovery,
            diagnostic=lambda substep: identity_service.assignment_diagnostic.update(substep=substep))
            identity_service.session_started_handler = runtime.start_session
            identity_service.session_ended_handler = runtime.end_session
            from runtime_restore import ActiveRuntimeRestorer
            restorer = ActiveRuntimeRestorer(identity_service, runtime)
            identity_service.calibration_invalidator = lambda session: calibration_store.invalidate(
                session.patient_id, session.hardware_uid, "session_ended"
            )
            if not args.no_http:
                tasks.append(asyncio.create_task(run_http_service(
                    orchestrator, args.http_host, args.http_port,
                    identity_service=identity_service, multi_runtime=runtime,
                )))
            def device_available(uid, device_handle):
                identity_service.discovery_detected_at[uid] = time.time()
                path = runtime.manager.get(uid)
                if path is not None:
                    path.controller.device_available(device_handle)
                elif uid not in restorer.attempted:
                    tasks.append(asyncio.create_task(restorer.restore(uid, device_handle)))
            await run_v2_discovery(identity_service.live_discovery, on_available=device_available)

        for t in tasks:
            t.cancel()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
