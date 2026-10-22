"""FIXED-PROFILE-25 DIAGNOSTIC BP path, version 1.0.0. Not Stage-10 completion.

One operator-requested acquisition at a time on the existing connection:
profile 25 only, no exposure preflight, no no-contact reference check. Contact
is therefore NOT established independently; only the frozen integrity and SQI
gates screen the window. Each acquisition is the experimental warm-up START
(command 7) verified by the unchanged bench verifier (WarmupBenchCapture):
UID/nonce/session/config FINAL v2 binding, cumulative loss/uncertainty
rejection, and warm-up samples excluded from the 3000-pair measurement window.
No NEW_SESSION is issued here, SET_PROFILE only when a fresh STATUS is not
already 25, and there is no automatic retry. With the current warm-up firmware
(config cleared at clean closure, SET_PROFILE increments the session) a
baseline cannot share a session with a later acquisition: the second window
is reported RECALIBRATE/INVALIDATED rather than given fake continuity.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

from bench_capture_integrity import WarmupBenchCapture
from ble_identity import canonical_uid
from ble_uuids import CHAR_CONTROL_UUID, CHAR_FINAL_UUID
from protocol_decoder import ControlCommand, decode_status_packet, encode_control_packet

POLICY_ID = "FIXED-PROFILE-25-DIAGNOSTIC-v1"
PROFILE = 25
WARMUP_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "config" / "acquisition_integrity_bench_warmup_v1.json"


class Fixed25Error(RuntimeError):
    pass


def load_warmup_contract(path: Path = WARMUP_CONTRACT_PATH) -> dict:
    """Timeouts/commands come from Codex's frozen contract, never redefined here."""
    c = json.loads(path.read_text())
    if (c.get("policy_id") != 1 or c.get("profile") != PROFILE or c.get("start_command") != 7
            or c.get("start_arg0") != 1 or c.get("final_witness_version") != WarmupBenchCapture.witness_version
            or c.get("warmup_sample_pairs") != WarmupBenchCapture.warmup_pairs
            or c.get("warmup_deadline_s") != WarmupBenchCapture.warmup_deadline_s
            or c.get("measurement_sample_pairs") != 3000):
        raise Fixed25Error("warm-up contract does not match the installed verifier")
    return c


class Fixed25DiagnosticRunner:
    """Measurement-runner shape used by MultiBleManager (on_data/on_status/
    on_disconnected/request_nurse/run, controller assigned by the manager)."""
    POST_STOP_DRAIN_S = 0.3

    def __init__(self, *, identity, orchestrator, schedule_policy=None, clock=time.monotonic, nonce=lambda: secrets.randbits(64) or 1, evidence_directory=None):
        if not orchestrator.patient_id or not orchestrator.logical_session_id:
            raise Fixed25Error("explicit assigned patient/session required")
        if canonical_uid(orchestrator.expected_hardware_uid) != canonical_uid(identity.hardware_uid):
            raise Fixed25Error("assigned identity mismatch")
        self.contract = load_warmup_contract()
        self.identity, self.orchestrator, self.clock, self._nonce = identity, orchestrator, clock, nonce
        from measurement_policy import CaptureSchedule
        self.schedule = CaptureSchedule(schedule_policy, now=clock(), manual_first=True) if schedule_policy else None
        self.controller = None
        self.capture = None
        self._final_client = None
        self.pending = False
        self.wake = asyncio.Event()
        self.status = asyncio.Queue(maxsize=32)
        self.completed_attempts = 0
        self.failed_attempts = 0
        self.last_reason = None
        self.evidence_directory = Path(evidence_directory) if evidence_directory is not None else Path(__file__).resolve().parent.parent / "var" / "capture_attempts"
        self.last_attempt_record = None
        self.last_attempt_artifact = None
        self.last_evidence_error = None
        self.last_status_raw = None

    def _retain_attempt(self, capture):
        """Retain the original verifier evidence before clearing live capture state."""
        record = capture.record()
        record["notification_journal"] = capture.journal
        record["received_warmup_pairs"] = capture.warmup_count
        record["received_measurement_pairs"] = sum(p.sample_count for p in capture.packets)
        record["last_status_raw_hex"] = self.last_status_raw.hex() if self.last_status_raw is not None else None
        self.last_attempt_record = record
        self.last_evidence_error = None
        try:
            self.evidence_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.evidence_directory.chmod(0o700)
            path = self.evidence_directory / f"{time.time_ns()}-{capture.uid:016X}-{capture.nonce:016X}.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            self.last_attempt_artifact = path
        except OSError as exc:
            # Preserve verifier failure and in-memory evidence even if disk fails.
            self.last_evidence_error = type(exc).__name__

    # -- notification callbacks (all bytes go to the unchanged verifier)
    async def on_data(self, raw):
        if self.capture is not None:
            count = len(self.capture.packets)
            self.capture.receive("DATA", bytes(raw), self.clock())
            if len(self.capture.packets) > count:
                if getattr(self.capture, "measurement_start_utc", None) is None:
                    self.capture.measurement_start_utc = time.time()
                self.capture.measurement_end_utc = time.time()

    async def on_final(self, _sender, raw):
        if self.capture is not None:
            self.capture.receive("FINAL", bytes(raw), self.clock())

    async def on_status(self, raw):
        self.last_status_raw = bytes(raw)
        if self.capture is not None:
            self.capture.receive("STATUS", bytes(raw), self.clock())
        await self.orchestrator.handle_status_packet(raw)
        try:
            self.status.put_nowait(decode_status_packet(raw))
        except (asyncio.QueueFull, ValueError):
            pass

    async def on_disconnected(self):
        if self.capture is not None:
            self.capture.finish_unverified("DISCONNECTED")
        self._final_client = None
        await self.orchestrator.handle_disconnected()

    def request_nurse(self):
        from measurement_policy import PolicyError
        if self.pending or self.capture is not None or (self.schedule and self.schedule.active):
            raise PolicyError("a fixed-profile-25 acquisition is already queued or running")
        if self.schedule:
            self.schedule.request_nurse()
        self.pending = True
        self.wake.set()

    async def run(self, stop_event):
        from measurement_policy import State
        try:
            while not stop_event.is_set():
                self.wake.clear()
                request = self.schedule.take(now=self.clock()) if self.schedule else None
                should_capture = request is not None if self.schedule else self.pending
                if should_capture:
                    self.pending = False
                    if self.schedule:
                        self.schedule.transition(request[0], now=self.clock())
                    failed = False
                    try:
                        await self.capture_once()
                        self.completed_attempts += 1
                    except Exception as exc:
                        failed = True
                        self.failed_attempts += 1
                        self.last_reason = str(exc) or type(exc).__name__
                        await self.orchestrator.handle_capture_failure(self.last_reason)
                    finally:
                        if self.schedule:
                            self.schedule.finish(now=self.clock(),calibration_due=True,reason="CAPTURE_FAILED" if failed else None)
                    # Every scheduled obligation is consumed once, including
                    # failure. No immediate retry and no cadence erasure.
                    continue
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
        finally:
            if self.schedule:
                self.schedule.stop(now=self.clock())

    # -- one acquisition
    async def _write(self, command, **kwargs):
        client = self.controller.client if self.controller else None
        if client is None or self.controller._disconnect_event.is_set():
            raise Fixed25Error("DISCONNECTED")
        await asyncio.wait_for(client.write_gatt_char(CHAR_CONTROL_UUID, encode_control_packet(command=int(command), **kwargs)), timeout=1.5)

    async def _fresh_config(self):
        """Config from a STATUS requested now (TIME_SYNC fence), never a stale one."""
        while not self.status.empty():
            self.status.get_nowait()
        await self._write(ControlCommand.TIME_SYNC, arg_u64=time.time_ns() // 1000)
        deadline = self.clock() + 1.5
        while self.clock() < deadline:
            try:
                packet = await asyncio.wait_for(self.status.get(), timeout=max(0.01, deadline - self.clock()))
            except asyncio.TimeoutError:
                break
            if canonical_uid(packet.hardware_uid) != canonical_uid(self.identity.hardware_uid):
                raise Fixed25Error("STATUS_IDENTITY_MISMATCH")
            return packet.sensor_config_id
        raise Fixed25Error("STATUS_NOT_RECEIVED")

    async def _ensure_profile(self):
        if await self._fresh_config() == PROFILE:
            return  # already verified at 25: no reconfiguration, session unchanged
        # Current firmware clears the config after every clean closure, so a
        # later acquisition needs SET_PROFILE, which starts a NEW sensor
        # session; the calibration store then reports INVALIDATED. Not hidden.
        await self._write(ControlCommand.SET_PROFILE, arg0=PROFILE)
        if await self._fresh_config() != PROFILE:
            raise Fixed25Error("PROFILE_25_NOT_VERIFIED")

    async def capture_once(self):
        client = self.controller.client if self.controller else None
        if client is None:
            raise Fixed25Error("DISCONNECTED")
        if client is not self._final_client:
            await client.start_notify(CHAR_FINAL_UUID, self.on_final)
            self._final_client = client
        c = self.contract
        # Created before profile preparation so pre-START STATUS and failures are
        # journaled too; HOST_START_* rows separate pre-START from post-abort STATUS.
        capture = WarmupBenchCapture(uid=int(canonical_uid(self.identity.hardware_uid), 16), nonce=self._nonce())
        self.capture = capture
        started = False
        try:
            try:
                await self._ensure_profile()
            except Fixed25Error as exc:
                capture.finish_unverified("PRE_START_" + str(exc))
                raise
            capture.journal.append(dict(channel="HOST_START_REQUEST", host_monotonic_s=self.clock()))
            started = True
            await self._write(ControlCommand.WARMUP_BENCH_START, arg0=c["start_arg0"], arg_u64=capture.nonce)
            capture.journal.append(dict(channel="HOST_START_ACK", host_monotonic_s=self.clock()))
            start = self.clock()
            while not capture.terminal:
                now = self.clock()
                capture.tick(now)
                if self.controller._disconnect_event.is_set():
                    capture.finish_unverified("DISCONNECTED")
                elif now - start >= c["capture_timeout_s"]:
                    capture.finish_unverified("CAPTURE_TIMEOUT")
                elif capture.warmup_count < capture.warmup_pairs and now - start >= c["warmup_deadline_s"]:
                    capture.finish_unverified("WARMUP_DEADLINE")
                elif not (capture.packets or capture.warmup_packets) and now - start >= c["first_data_timeout_s"]:
                    capture.finish_unverified("NO_DATA_WITHIN_2_S")
                if not capture.terminal:
                    await asyncio.sleep(0.01)
        finally:
            if not capture.terminal:
                capture.finish_unverified("CAPTURE_INTERRUPTED")
            if started and not self.controller._disconnect_event.is_set():
                try:  # best-effort, as in the bench executor; never a new START
                    await self._write(ControlCommand.STOP)
                except Exception:
                    pass
                # Keep journaling (verifier marks AFTER_FINALIZATION) so the
                # post-abort STATUS/FINAL bytes are retained with the attempt.
                await asyncio.sleep(self.POST_STOP_DRAIN_S)
            self._retain_attempt(capture)
            self.capture = None
        if capture.result != "INTEGRITY_VERIFIED":
            raise Fixed25Error(f"{capture.result}:{capture.reason}")
        self.orchestrator.baseline_interval = (getattr(capture, "measurement_start_utc", None), getattr(capture, "measurement_end_utc", None))
        await self.orchestrator.handle_fixed25_verified_window(capture)
        return capture
