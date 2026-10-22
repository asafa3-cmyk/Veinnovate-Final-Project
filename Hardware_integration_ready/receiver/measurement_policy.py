"""Stage 16 Alternative 2: per-session scheduling and bounded Z23 capture.

Policy events contain timing/reason only, never patient data or measurements.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ble_identity import canonical_uid
from ble_uuids import CHAR_CONTROL_UUID
from calibration_store import CalibrationStatus
from protocol_decoder import ControlCommand, decode_status_packet, encode_control_packet
from stage10_z23_controller import (
    BoundedExposureController, Stage10PacketStream, Stage10PolicyError,
    deliver_verified_window, load_stage10_z23_policy,
)

POLICY_ID = "STAGE16-ALTERNATIVE2-v1"


class PolicyError(ValueError):
    pass


class State(str, Enum):
    OFF = "OFF"
    BOOT = "BOOT"
    ADVERTISING = "ADVERTISING"
    PAIRED = "PAIRED"
    IDLE = "IDLE"
    SCHEDULED_CAPTURE = "SCHEDULED_CAPTURE"
    NURSE_FORCED_CAPTURE = "NURSE_FORCED_CAPTURE"
    CALIBRATION_DUE = "CALIBRATION_DUE"
    TRANSMIT = "TRANSMIT"
    FAILURE = "FAILURE"


TRANSITIONS = {
    State.OFF: {State.BOOT},
    State.BOOT: {State.ADVERTISING, State.FAILURE},
    State.ADVERTISING: {State.PAIRED, State.FAILURE},
    State.PAIRED: {State.IDLE, State.CALIBRATION_DUE, State.SCHEDULED_CAPTURE, State.NURSE_FORCED_CAPTURE, State.FAILURE},
    State.IDLE: {State.CALIBRATION_DUE, State.SCHEDULED_CAPTURE, State.NURSE_FORCED_CAPTURE, State.FAILURE},
    State.CALIBRATION_DUE: {State.IDLE, State.SCHEDULED_CAPTURE, State.NURSE_FORCED_CAPTURE, State.FAILURE},
    State.SCHEDULED_CAPTURE: {State.TRANSMIT, State.IDLE, State.CALIBRATION_DUE, State.FAILURE},
    State.NURSE_FORCED_CAPTURE: {State.TRANSMIT, State.IDLE, State.CALIBRATION_DUE, State.FAILURE},
    State.TRANSMIT: {State.IDLE, State.CALIBRATION_DUE, State.FAILURE},
    State.FAILURE: {State.PAIRED, State.IDLE, State.CALIBRATION_DUE, State.SCHEDULED_CAPTURE, State.NURSE_FORCED_CAPTURE},
}


@dataclass(frozen=True)
class MeasurementPolicy:
    scheduled_interval_s: float
    capture_deadline_s: float
    policy_id: str = POLICY_ID
    schema_version: str = "1.0.0"

    def __post_init__(self):
        if self.policy_id != POLICY_ID or self.schema_version != "1.0.0":
            raise PolicyError("unsupported measurement policy")
        for value in (self.scheduled_interval_s, self.capture_deadline_s):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise PolicyError("finite numeric timing is required")
        if self.capture_deadline_s < 35 or self.scheduled_interval_s <= self.capture_deadline_s:
            raise PolicyError("capture deadline must cover 5 s preflight plus 30 s acquisition; interval must exceed deadline")

    @classmethod
    def load(cls, path: Path):
        try:
            value = json.loads(path.read_text())
            if set(value) != {"policy_id", "schema_version", "scheduled_interval_s", "capture_deadline_s"}:
                raise PolicyError("measurement policy keys mismatch")
            return cls(**value)
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise PolicyError("missing or invalid measurement policy configuration") from exc


class CaptureSchedule:
    """Fixed cadence: forced requests never erase or move a scheduled slot."""
    def __init__(self, policy: MeasurementPolicy, *, now: float, manual_first: bool = False):
        self.policy = policy
        self.next_due = now + policy.scheduled_interval_s
        self.enabled = not manual_first
        self.nurse_pending = False
        self.active = False
        self.stopped = False
        self.state = State.OFF
        # ponytail: last 128 operational events only; durable audit belongs to deployment storage.
        self.events = deque(maxlen=128)
        self.transition(State.BOOT, now=now)
        self.transition(State.ADVERTISING, now=now)
        self.transition(State.PAIRED, now=now)

    def transition(self, state: State, *, now: float, reason: str | None = None):
        if state == self.state:
            return
        if state != State.OFF and state not in TRANSITIONS[self.state]:
            raise PolicyError(f"invalid transition {self.state.value} -> {state.value}")
        self.state = state
        self.events.append({"state": state.value, "monotonic_s": now, "reason": reason})

    def request_nurse(self):
        if self.stopped or self.nurse_pending:
            raise PolicyError("session stopped or nurse request already queued")
        self.nurse_pending = True

    def take(self, *, now: float):
        if self.active or self.stopped:
            return None
        # Scheduled obligations take priority when already due. A queued forced
        # request follows; neither can satisfy or replace the other.
        if self.enabled and now >= self.next_due:
            due = self.next_due
            self.next_due += self.policy.scheduled_interval_s
            self.active = True
            return State.SCHEDULED_CAPTURE, due
        if self.nurse_pending:
            self.nurse_pending = False
            self.active = True
            if not self.enabled:
                self.enabled = True
                self.next_due = now + self.policy.scheduled_interval_s
            return State.NURSE_FORCED_CAPTURE, now
        return None

    def finish(self, *, now: float, calibration_due: bool, reason: str | None = None):
        self.active = False
        self.transition(State.FAILURE if reason else State.CALIBRATION_DUE if calibration_due else State.IDLE,
                        now=now, reason=reason)

    def stop(self, *, now: float):
        self.stopped = True
        self.active = False
        self.nurse_pending = False
        self.transition(State.OFF, now=now, reason="SESSION_ENDED_NOT_PHYSICAL_POWER_PROOF")


class ScheduledCaptureRunner:
    """Runs only under an already committed, identity-verified assignment."""
    def __init__(self, policy, *, identity, orchestrator, loaded_exposure=None, clock=time.monotonic):
        self.clock = clock
        self.identity = identity
        self.orchestrator = orchestrator
        uid = canonical_uid(identity.hardware_uid)
        try:
            self.exposure = loaded_exposure or load_stage10_z23_policy(hardware_uid=uid)
        except Stage10PolicyError as exc:
            raise PolicyError(f"Z23 exposure calibration is unavailable for this adapter: {exc}") from exc
        # Same-pair optical references are device-specific: the loaded registry
        # must belong to exactly this verified UID and printed short code.
        if canonical_uid(self.exposure.hardware_uid) != uid or self.exposure.short_code != identity.short_code:
            raise PolicyError("Z23 exposure calibration is unavailable for this adapter")
        if not orchestrator.patient_id or not orchestrator.logical_session_id:
            raise PolicyError("explicit assigned patient/session required")
        if canonical_uid(orchestrator.expected_hardware_uid) != canonical_uid(identity.hardware_uid):
            raise PolicyError("assigned identity mismatch")
        self.schedule = CaptureSchedule(policy, now=clock(), manual_first=True)
        self.controller = None
        self.data = asyncio.Queue(maxsize=512)
        self.status = asyncio.Queue(maxsize=32)
        self.wake = asyncio.Event()
        self.accepting = False
        self.queue_error = False
        self.completed_attempts = 0
        self.failed_attempts = 0
        self.effective_response_ceiling_s = policy.capture_deadline_s
        self.last_response_s = None
        self.last_downstream_s = None
        self.response_overruns = 0
        self.scheduled_lateness_s = 0

    async def on_data(self, raw):
        if self.accepting:
            try:
                self.data.put_nowait((bytes(raw), time.monotonic_ns()))
            except asyncio.QueueFull:
                self.queue_error = True

    async def on_status(self, raw):
        await self.orchestrator.handle_status_packet(raw)
        if self.accepting:
            try:
                self.status.put_nowait((decode_status_packet(raw), time.monotonic_ns()))
            except (asyncio.QueueFull, ValueError):
                self.queue_error = True

    def request_nurse(self):
        self.schedule.request_nurse()
        self.wake.set()

    async def on_disconnected(self):
        if self.schedule.stopped:
            return
        await self.orchestrator.handle_disconnected()
        self.schedule.transition(State.FAILURE, now=self.clock(), reason="DEVICE_DISCONNECTED")
        self.wake.set()

    def calibration_due(self):
        o = self.orchestrator
        status = o.calibration_store.status(
            o.patient_id, o.calibration_device_key(),
            current_sensor_session_id=o._current_session_id,
            current_sensor_config_id=o.last_status.sensor_config_id if o.last_status else None,
        )
        return status not in (CalibrationStatus.VALID, CalibrationStatus.WARNING)

    async def _control(self, command, **kwargs):
        if self.controller.client is None or self.controller._disconnect_event.is_set():
            raise Stage10PolicyError("DISCONNECTION_OR_PERSISTENCE_FAILURE")
        await asyncio.wait_for(self.controller.client.write_gatt_char(
            CHAR_CONTROL_UUID, encode_control_packet(command=int(command), **kwargs)), timeout=1.5)

    async def _verify_pair(self, pair):
        config = self.exposure.pairs_to_config[pair]
        # Reapplying even the same profile increments the firmware session.
        # Avoid that when exact STATUS already verifies the required pair;
        # changed profiles still invalidate calibration through the existing gate.
        current = self.orchestrator.last_status
        if current is None or current.sensor_config_id != config:
            await self._control(ControlCommand.SET_PROFILE, arg0=config)
        while not self.status.empty():
            self.status.get_nowait()
        fence = time.monotonic_ns()
        await self._control(ControlCommand.TIME_SYNC, arg_u64=time.time_ns() // 1000)
        deadline = self.clock() + 1.5
        while True:
            packet, host_ns = await self._next(self.status, deadline)
            if host_ns < fence:
                continue
            if canonical_uid(packet.hardware_uid) != canonical_uid(self.identity.hardware_uid) or packet.fifo_overflow_count:
                raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE")
            if packet.sensor_config_id == config:
                return

    async def _next(self, queue, deadline):
        while self.clock() < deadline:
            if self.controller.client is None or self.controller._disconnect_event.is_set():
                raise Stage10PolicyError("DISCONNECTION_OR_PERSISTENCE_FAILURE")
            if self.queue_error:
                raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE")
            try:
                return await asyncio.wait_for(queue.get(), min(.25, deadline-self.clock()))
            except asyncio.TimeoutError:
                pass
        raise asyncio.TimeoutError("PREFLIGHT_OR_CAPTURE_TIMEOUT")

    async def _acquire(self):
        while not self.data.empty(): self.data.get_nowait()
        while not self.status.empty(): self.status.get_nowait()
        self.queue_error = False
        self.accepting = True
        exposure = BoundedExposureController(self.exposure)
        stream = Stage10PacketStream(exposure, hardware_uid=canonical_uid(self.identity.hardware_uid))
        try:
            await self._verify_pair(stream.current_pair)
            # START resets the existing experimental firmware FIFO/index. Do not
            # issue NEW_SESSION merely because another scheduled slot is due.
            start_ns = time.monotonic_ns()
            await self._control(ControlCommand.START)
            while not stream.measurement_complete:
                if self.queue_error:
                    raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE")
                remaining = 5 - (self.clock() - exposure.started) if not stream.converged else 30 + 5
                if remaining <= 0: raise Stage10PolicyError("PREFLIGHT_TIMEOUT")
                raw, host_ns = await self._next(self.data, self.clock() + remaining)
                if host_ns < start_ns: continue
                decision = stream.ingest(raw, host_monotonic_ns=host_ns)
                if decision and decision.status == "ADJUST":
                    await self._control(ControlCommand.STOP)
                    await self._verify_pair(decision.next_pair)
                    stream.reset_for_adjustment(decision.next_pair)
                    start_ns = time.monotonic_ns()
                    await self._control(ControlCommand.START)
                elif decision and decision.status == "CONVERGED":
                    exposure.assert_deadline()
                    self.schedule.transition(State.TRANSMIT, now=self.clock(), reason="BLE_STREAMS_DURING_CAPTURE")
            return stream
        finally:
            self.accepting = False
            try:
                await self._control(ControlCommand.STOP)
            except Exception as exc:
                # Fail closed if STOP cannot be delivered; reconnect alone does
                # not restart capture because controller auto_start is disabled.
                if self.controller.client is not None:
                    client = self.controller.client
                    self.controller.client = None
                    self.controller._disconnect_event.set()
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=3)
                    except Exception:
                        pass  # link is unusable; no further control/result on it
                raise Stage10PolicyError("DISCONNECTION_OR_PERSISTENCE_FAILURE") from exc

    async def capture(self):
        started = self.clock()
        # Preflight and individual missing-DATA waits are bounded by _acquire.
        # The response target is never an overall cancellation deadline, even
        # if transfer of the required complete window takes longer than it.
        stream = await self._acquire()
        downstream = self.clock()
        await deliver_verified_window(stream, self.orchestrator)
        self.last_downstream_s = self.clock() - downstream
        self.last_response_s = self.clock() - started + self.scheduled_lateness_s
        if self.last_response_s > self.effective_response_ceiling_s or self.last_downstream_s > 5:
            self.response_overruns += 1
            self.effective_response_ceiling_s = max(
                self.effective_response_ceiling_s, self.last_response_s, 35 + self.last_downstream_s)
            self.schedule.events.append({"state": self.schedule.state.value,
                "monotonic_s": self.clock(), "reason": "RESPONSE_CEILING_RAISED",
                "response_ceiling_s": self.effective_response_ceiling_s})

    async def run(self, stop_event):
        try:
            while not stop_event.is_set():
                request = self.schedule.take(now=self.clock())
                if request:
                    source, due = request
                    self.schedule.transition(source, now=self.clock())
                    self.scheduled_lateness_s = max(0, self.clock() - due)
                    if self.scheduled_lateness_s > self.schedule.policy.capture_deadline_s:
                        self.schedule.events.append({"state": source.value, "monotonic_s": self.clock(),
                            "reason": "SCHEDULE_LATE_CAPTURE_STILL_REQUIRED"})
                    try:
                        await self.capture()
                        self.completed_attempts += 1
                        self.schedule.finish(now=self.clock(), calibration_due=self.calibration_due())
                    except Exception as exc:
                        self.failed_attempts += 1
                        reason = str(exc) if isinstance(exc, (PolicyError, Stage10PolicyError)) else "CAPTURE_FAILED"
                        self.schedule.finish(now=self.clock(), calibration_due=True, reason=reason)
                        await self.orchestrator.handle_capture_failure(reason)
                    continue
                if not self.schedule.active and self.schedule.state != State.FAILURE:
                    if self.orchestrator.connection_status == "CONNECTED":
                        self.schedule.transition(State.CALIBRATION_DUE if self.calibration_due() else State.IDLE, now=self.clock())
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=(min(1, max(.01, self.schedule.next_due-self.clock())) if self.schedule.enabled else 1))
                except asyncio.TimeoutError:
                    pass
        finally:
            self.schedule.stop(now=self.clock())


def simulate_energy(*, idle_current_ma, active_current_ma, active_duration_s, interval_s,
                    battery_v=4.01, capacity_mah=220):
    """Illustrative budget; active input is explicit, never a production fallback."""
    values = [idle_current_ma, active_current_ma, active_duration_s, interval_s, battery_v, capacity_mah]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values) or active_duration_s > interval_s:
        raise PolicyError("invalid simulation inputs")
    average_ma = (idle_current_ma*(interval_s-active_duration_s)+active_current_ma*active_duration_s)/interval_s
    return {"evidence_class": "THEORETICAL_NOT_MEASURED", "average_current_ma": average_ma,
            "average_power_mw": battery_v*average_ma,
            "capture_energy_mj": battery_v*active_current_ma*active_duration_s,
            "ideal_runtime_h": capacity_mah/average_ma}
