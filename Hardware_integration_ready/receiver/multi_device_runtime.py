"""Session-indexed orchestration facade for simultaneous V2 adapters."""

from __future__ import annotations

import time

from ble_identity import canonical_uid
from battery_telemetry import battery_snapshot, load_validations
from multi_ble_manager import MultiBleManager
from veinnet_edge_service import EdgeOrchestrator


class MultiDeviceRuntime:
    def __init__(self, *, connector, model_adapter_factory, calibration_store, measurement_policy=None,
                 fixed25_diagnostic: bool = False) -> None:
        self.battery_validations = load_validations()
        if fixed25_diagnostic and measurement_policy is not None:
            raise ValueError("fixed-profile-25 diagnostic and Stage-10 scheduled policy are mutually exclusive")
        self.fixed25_diagnostic = fixed25_diagnostic
        self.manager = MultiBleManager(connector)
        self.model_adapter_factory = model_adapter_factory
        self.calibration_store = calibration_store
        self.measurement_policy = measurement_policy
        from pathlib import Path
        from measurement_policy import MeasurementPolicy
        self.fixed25_schedule_policy = MeasurementPolicy.load(Path(__file__).resolve().parent.parent / "config" / "measurement_policy_v1.json") if fixed25_diagnostic else None
        self._by_session: dict[str, EdgeOrchestrator] = {}
        self._session_uid: dict[str, str] = {}
        self.result_listeners: list = []
        self.baseline_listeners: list = []

    async def start_session(self, device_handle, identity, result: dict, *, restored=False) -> None:
        session = result["session"]
        assignment = result["assignment"]
        uid = canonical_uid(identity.hardware_uid)
        orchestrator = EdgeOrchestrator(
            model_adapter=self.model_adapter_factory(),
            calibration_store=self.calibration_store,
            patient_id=assignment["patient_id"],
            expected_hardware_uid=uid,
            short_code=identity.short_code,
            logical_session_id=session["session_id"],
        )

        orchestrator.invalidate_calibration_on_restored_connect = restored
        async def relay(vitals_result):
            for listener in list(self.result_listeners):
                await listener(vitals_result)

        orchestrator.result_listeners.append(relay)
        async def baseline(*args):
            for listener in list(self.baseline_listeners):
                await listener(*args)
        orchestrator.baseline_listeners.append(baseline)
        kwargs = dict(identity=identity, device_handle=device_handle, orchestrator=orchestrator)
        if self.fixed25_diagnostic:
            from fixed25_diagnostic import Fixed25DiagnosticRunner
            kwargs["measurement"] = Fixed25DiagnosticRunner(identity=identity, orchestrator=orchestrator, schedule_policy=self.fixed25_schedule_policy)
        elif self.measurement_policy is not None:
            from measurement_policy import ScheduledCaptureRunner
            kwargs["measurement"] = ScheduledCaptureRunner(
                self.measurement_policy, identity=identity, orchestrator=orchestrator)
        await self.manager.start(**kwargs)
        self._by_session[session["session_id"]] = orchestrator
        self._session_uid[session["session_id"]] = uid

    async def end_session(self, session: dict) -> None:
        session_id = session["session_id"]
        uid = self._session_uid.pop(session_id, None)
        self._by_session.pop(session_id, None)
        if uid is not None:
            await self.manager.stop(uid)

    def for_session(self, session_id: str) -> EdgeOrchestrator | None:
        return self._by_session.get(session_id)

    def status_snapshot(self) -> dict:
        """Operational status only; no profiles or clinical values."""
        sessions = []
        policy = self.fixed25_schedule_policy or self.measurement_policy
        for session_id, orchestrator in self._by_session.items():
            path = self.manager.get(self._session_uid[session_id])
            measurement = path.measurement if path is not None else None
            schedule = getattr(measurement,"schedule",None)
            capture = getattr(measurement,"capture",None) if self.fixed25_diagnostic else None
            packets = getattr(capture,"packets",[])
            state = (
                "WARMUP" if capture is not None and getattr(capture,"warmup_count",500) < 500
                else str(capture.result) if capture is not None
                else "DATA_PENDING" if getattr(schedule,"active",False)
                else "QUEUED" if getattr(measurement,"pending",False) or getattr(schedule,"nurse_pending",False)
                else "IDLE" if measurement is not None else "UNAVAILABLE"
            )
            enabled = bool(getattr(schedule,"enabled",False) and not getattr(schedule,"stopped",False))
            sessions.append({
                "session_id":session_id,
                "battery":battery_snapshot(orchestrator, self.battery_validations, now=time.time()),
                "connection_status":"CONNECTING" if orchestrator.connection_status == "RECONNECTING" else orchestrator.connection_status,
                "acquisition_state":state,
                "received_sample_pairs":sum(p.sample_count for p in packets) if self.fixed25_diagnostic else None,
                "latest_reading_id":orchestrator.latest_result.reading_id if orchestrator.latest_result else None,
                "last_communication_at_utc":getattr(orchestrator,"last_communication_at_utc",None),
                "scheduled_interval_s":policy.scheduled_interval_s if policy else None,
                "schedule_enabled":enabled,
                "next_measurement_at_utc": time.time()+max(0,schedule.next_due-measurement.clock()) if enabled else None,
            })
        return {"schema_version":"1.1.0","served_at_utc":time.time(),"sessions":sessions}

    def request_capture(self, session_id: str) -> None:
        from measurement_policy import PolicyError
        uid = self._session_uid.get(session_id)
        path = self.manager.get(uid) if uid is not None else None
        if path is None or path.measurement is None:
            raise PolicyError("no active measurement session")
        path.measurement.request_nurse()
