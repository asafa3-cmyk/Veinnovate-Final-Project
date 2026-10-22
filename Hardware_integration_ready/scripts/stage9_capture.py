#!/usr/bin/env python3
"""Run exactly the next PHI-free Stage 9 BLE capture and close its ledger."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

BUNDLE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BUNDLE_ROOT / "receiver"))

from ble_identity import canonical_uid, decode_identity_value
from ble_uuids import CHAR_CONTROL_UUID, CHAR_DATA_UUID, CHAR_IDENTITY_UUID, CHAR_STATUS_UUID
from bleak_adapter import BleakClientAdapter, scan_v2_adapters
from live_ble_discovery import LiveBleDiscovery
from protocol_decoder import ControlCommand, Flags, decode_data_packet, decode_status_packet, encode_control_packet
from qa_capture_recorder import CaptureIntegrityError, QaCaptureRecorder, load_and_verify_capture
from stage9_experiment import (
    Stage9PlanError,
    load_informational_baseline_disposition,
    load_plan,
    next_slot,
    read_ledger,
    require_metric_recording_contract_reviewed,
    sha256_file,
    write_ledger,
)
from stage9_transport_diagnostics import (
    TransportDiagnosticIntegrityError,
    TransportDiagnostics,
    load_and_verify_transport_diagnostic,
    validate_external_batch_root,
)


class Stage9CaptureError(RuntimeError):
    pass


PROFILE_ACK_TIMEOUT_S = 8.0
CAPTURE_TIMEOUT_S = 45.0


def _verify_informational_baselines(disposition: dict, batch_root: Path) -> int:
    for attempt in disposition["attempts"]:
        attempt_dir = batch_root / "diagnostics" / attempt["attempt_id"]
        diagnostic_path = attempt_dir / "diagnostic.json"
        data = diagnostic_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != attempt["diagnostic_sha256"]:
            raise Stage9CaptureError("informational baseline diagnostic hash mismatch")
        try:
            payload = load_and_verify_transport_diagnostic(attempt_dir)
        except TransportDiagnosticIntegrityError as exc:
            raise Stage9CaptureError(
                f"informational baseline diagnostic manifest mismatch: {exc}"
            ) from exc
        if (
            payload.get("attempt_id") != attempt["attempt_id"]
            or payload.get("experiment_id") != disposition["experiment_id"]
            or payload.get("device") != {
                "hardware_uid": disposition["hardware_uid"],
                "short_code": disposition["short_code"],
            }
            or payload.get("slot") != {"config_id": 16, "repetition": 1}
            or payload.get("handshake", {}).get("start_count") != 1
            or payload.get("data", {}).get("accepted_sample_pairs") != 3000
            or payload.get("data", {}).get("rejected_packets") != []
            or payload.get("firmware_reported_capture_completion") is not True
            or payload.get("error") != "QA validation failed: LOW_PSNR"
            or payload.get("capture_published") is not False
            or payload.get("ledger_entry_created") is not False
        ):
            raise Stage9CaptureError("informational baseline diagnostic content mismatch")
    return disposition["planned_slots_disposed"]


class _StatusAckGate:
    """Commit one fresh post-SET_PROFILE STATUS before permitting START."""

    def __init__(
        self,
        *,
        expected_uid: str,
        expected_config_id: int,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        status_observer: Callable[[object], None] | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._expected_uid = expected_uid
        self._expected_config_id = expected_config_id
        self._clock_ns = clock_ns
        self._status_observer = status_observer
        self._ack_committed = self._loop.create_future()
        self._subscribed = False
        self._write_committed_ns: int | None = None
        self._closed = False
        self._start_claimed = False
        self.rejected: list[str] = []

    @property
    def acknowledgment_future(self) -> asyncio.Future:
        return self._ack_committed

    @property
    def committed(self) -> bool:
        return self._ack_committed.done() and not self._ack_committed.cancelled() \
            and self._ack_committed.exception() is None

    def mark_subscribed(self) -> None:
        if self._closed:
            raise Stage9CaptureError("STATUS gate is closed")
        self._subscribed = True

    def begin_profile_write(self) -> None:
        if not self._subscribed:
            raise Stage9CaptureError("STATUS notifications were not subscribed before SET_PROFILE")
        if self._write_committed_ns is not None:
            raise Stage9CaptureError("SET_PROFILE acknowledgment gate was already armed")

    def mark_profile_write_committed(self) -> None:
        if self._closed:
            raise Stage9CaptureError("STATUS gate closed before SET_PROFILE completed")
        self._write_committed_ns = self._clock_ns()

    def on_status(self, _sender, raw) -> None:
        received_ns = self._clock_ns()
        payload = bytes(raw)
        self._loop.call_soon_threadsafe(self._process_status, payload, received_ns)

    def _fail(self, exc: BaseException) -> None:
        if not self._ack_committed.done():
            self._ack_committed.set_exception(exc)

    def _process_status(self, payload: bytes, received_ns: int) -> None:
        if self._closed:
            self.rejected.append("late")
            return
        if not self._subscribed:
            self.rejected.append("pre-subscription")
            return
        try:
            status = decode_status_packet(payload)
        except BaseException as exc:
            self.rejected.append("malformed")
            self._fail(Stage9CaptureError(f"malformed STATUS notification: {exc}"))
            return
        if self._status_observer is not None:
            self._status_observer(status)
        if canonical_uid(status.hardware_uid) != self._expected_uid:
            self.rejected.append("identity-mismatch")
            self._fail(Stage9CaptureError("STATUS identity mismatch"))
            return
        if status.protocol_version != 3 or status.software_overflow_count or status.acquisition_uncertainty:
            self.rejected.append("acquisition-continuity-uncertain")
            self._fail(Stage9CaptureError("STATUS acquisition continuity uncertain"))
            return
        if status.fifo_overflow_count != 0:
            self.rejected.append("fifo-overflow")
            self._fail(Stage9CaptureError("FIFO overflow was present before capture"))
            return
        if self._write_committed_ns is None or received_ns < self._write_committed_ns:
            self.rejected.append("stale")
            return
        if status.sensor_config_id != self._expected_config_id:
            self.rejected.append("config-mismatch")
            return
        if self._ack_committed.done():
            self.rejected.append("duplicate")
            return
        self._ack_committed.set_result(status)

    def claim_start(self) -> None:
        if self._closed or not self.committed:
            raise Stage9CaptureError("START blocked before verified STATUS commit")
        if self._start_claimed:
            raise Stage9CaptureError("duplicate START blocked")
        self._start_claimed = True

    def close(self) -> None:
        self._closed = True
        if not self._ack_committed.done():
            self._ack_committed.cancel()


async def _wait_gate_or_disconnect(
    *,
    gate: _StatusAckGate,
    future: asyncio.Future,
    disconnected: asyncio.Event,
    timeout_s: float,
    timeout_message: str,
) -> object:
    disconnect_task = asyncio.create_task(disconnected.wait())
    try:
        done, _ = await asyncio.wait(
            {future, disconnect_task}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        if disconnected.is_set():
            gate.close()
            raise Stage9CaptureError("BLE disconnected before verified STATUS commit")
        if future in done:
            return future.result()
        gate.close()
        raise Stage9CaptureError(timeout_message)
    finally:
        disconnect_task.cancel()
        await asyncio.gather(disconnect_task, return_exceptions=True)


async def _subscribe_and_commit_profile(
    *,
    client,
    disconnected: asyncio.Event,
    on_data,
    expected_uid: str,
    expected_config_id: int,
    acknowledgment_timeout_s: float = PROFILE_ACK_TIMEOUT_S,
    status_observer: Callable[[object], None] | None = None,
) -> _StatusAckGate:
    gate = _StatusAckGate(
        expected_uid=expected_uid,
        expected_config_id=expected_config_id,
        status_observer=status_observer,
    )
    try:
        await client.start_notify(CHAR_STATUS_UUID, gate.on_status)
        gate.mark_subscribed()
        await client.start_notify(CHAR_DATA_UUID, on_data)
        gate.begin_profile_write()
        await client.write_gatt_char(
            CHAR_CONTROL_UUID,
            encode_control_packet(command=int(ControlCommand.SET_PROFILE), arg0=expected_config_id),
        )
        # The freshness timeout starts only after the acknowledged GATT write
        # returns. Notifications received before this fence cannot satisfy it.
        gate.mark_profile_write_committed()
        # Protocol V2 does not promise an unsolicited notification immediately
        # after subscription. TIME_SYNC is the explicit, side-effect-free
        # control request whose firmware response is a current STATUS packet.
        await client.write_gatt_char(
            CHAR_CONTROL_UUID,
            encode_control_packet(
                command=int(ControlCommand.TIME_SYNC),
                arg_u64=time.time_ns() // 1_000,
            ),
        )
        await _wait_gate_or_disconnect(
            gate=gate,
            future=gate.acknowledgment_future,
            disconnected=disconnected,
            timeout_s=acknowledgment_timeout_s,
            timeout_message=f"config {expected_config_id} received no verified fresh STATUS response",
        )
        return gate
    except BaseException:
        gate.close()
        raise


async def _start_capture_once(
    *, client, gate: _StatusAckGate, disconnected: asyncio.Event, announce, on_start_sent=lambda: None
) -> None:
    if disconnected.is_set():
        gate.close()
        raise Stage9CaptureError("BLE disconnected before START")
    gate.claim_start()
    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=int(ControlCommand.NEW_SESSION))
    )
    if disconnected.is_set():
        gate.close()
        raise Stage9CaptureError("BLE disconnected before START")
    announce()
    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=int(ControlCommand.START))
    )
    on_start_sent()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


async def capture_next(
    *,
    plan_path: Path,
    batch_root: Path,
    firmware_package: Path,
    informational_baseline_disposition: Path | None = None,
) -> dict:
    plan = load_plan(plan_path)
    # Contract, plan identity/hash, exact attempt count, configs, ADC range, and
    # immutable zone metadata are all verified before BLE discovery.
    require_metric_recording_contract_reviewed(plan_path=plan_path, plan=plan)
    exposure_only = plan.get("batch_type") == "EXPOSURE_CHARACTERIZATION_ONLY"
    skipped_slots = 0
    disposition_record = None
    if informational_baseline_disposition is not None:
        expected_disposition = BUNDLE_ROOT / "config" / "stage9_config16_informational_baseline.json"
        if informational_baseline_disposition.resolve() != expected_disposition.resolve():
            raise Stage9CaptureError("only the committed config-16 baseline disposition is allowed")
        disposition = load_informational_baseline_disposition(
            informational_baseline_disposition, plan_path=plan_path
        )
        skipped_slots = _verify_informational_baselines(disposition, batch_root)
        disposition_record = {
            "path": str(informational_baseline_disposition.resolve().relative_to(BUNDLE_ROOT)),
            "sha256": sha256_file(informational_baseline_disposition),
            "skipped_slots": skipped_slots,
            "disposition": disposition["disposition"],
        }
    ledger = read_ledger(batch_root)
    plan_sha = sha256_file(plan_path)
    firmware_sha = sha256_file(firmware_package)
    if ledger is None:
        ledger = {
            "schema_version": "2.2.0" if exposure_only else "1.0.0",
            "experiment_id": plan["experiment_id"],
            "hardware_uid": plan["hardware_uid"],
            "short_code": plan["short_code"],
            "plan_sha256": plan_sha,
            "firmware_package_sha256": firmware_sha,
            "informational_baseline_disposition": disposition_record,
            ("attempts" if exposure_only else "captures"): [],
        }
    elif (
        ledger.get("experiment_id") != plan["experiment_id"]
        or ledger.get("hardware_uid") != plan["hardware_uid"]
        or ledger.get("short_code") != plan["short_code"]
        or ledger.get("plan_sha256") != plan_sha
        or ledger.get("firmware_package_sha256") != firmware_sha
        or ledger.get("informational_baseline_disposition") != disposition_record
    ):
        raise Stage9CaptureError("plan, identity, or firmware artifact differs from the open batch")
    slot = next_slot(plan, ledger, skipped_slots=skipped_slots)
    if slot is None:
        raise Stage9CaptureError("the authorized Stage 9 batch is already complete")
    validate_external_batch_root(batch_root)
    diagnostics = TransportDiagnostics(
        experiment_id=plan["experiment_id"],
        hardware_uid=plan["hardware_uid"],
        short_code=plan["short_code"],
        config_id=slot.config_id,
        repetition=slot.repetition,
        zone=slot.zone,
        attempt_ordinal=slot.attempt_ordinal,
    )
    disconnected = asyncio.Event()
    client = None
    recorder = QaCaptureRecorder(
        hardware_uid=plan["hardware_uid"],
        short_code=plan["short_code"],
        sensor_config_id=slot.config_id,
        register_settings=slot.settings,
    )
    capture_done = asyncio.Event()
    callback_error: list[BaseException] = []
    sample_count = 0
    status_gate: _StatusAckGate | None = None
    failure: BaseException | None = None
    unexpected_disconnect = False
    artifact = None
    record = None

    def rejection_code(exc: BaseException) -> str:
        message = str(exc).lower()
        if "identity" in message or "uid" in message:
            return "IDENTITY_MISMATCH"
        if "config" in message or "profile" in message:
            return "CONFIG_MISMATCH"
        if "timestamp" in message or "order" in message or "duplicate" in message:
            return "ORDER_OR_TIMESTAMP_INVALID"
        if "sample rate" in message:
            return "SAMPLE_RATE_MISMATCH"
        return "CAPTURE_INTEGRITY_REJECTED"

    def on_data(_sender, raw) -> None:
        nonlocal sample_count
        host_monotonic_ns = time.monotonic_ns()
        host_utc = _utc_now()
        notification_index = diagnostics.data_notification(
            host_monotonic_ns=host_monotonic_ns, host_utc=host_utc, raw=bytes(raw)
        )
        if callback_error:
            diagnostics.rejected(notification_index, "AFTER_TERMINAL_PACKET_ERROR")
            return
        try:
            try:
                packet = decode_data_packet(bytes(raw))
            except BaseException:
                diagnostics.rejected(notification_index, "MALFORMED_DATA")
                raise
            diagnostics.decoded(packet)
            if packet.protocol_version != 3 or packet.software_overflow_count or packet.acquisition_uncertainty:
                diagnostics.rejected(notification_index, "ACQUISITION_CONTINUITY_UNCERTAIN")
                raise Stage9CaptureError("acquisition continuity uncertain")
            if packet.fifo_overflow_count != 0 or packet.flags & int(Flags.FIFO_OVERFLOW_SINCE_LAST):
                diagnostics.rejected(notification_index, "FIFO_OVERFLOW")
                raise Stage9CaptureError("FIFO overflow detected; capture stopped")
            if any(value >= 262143 for value in (*packet.red, *packet.ir)):
                diagnostics.rejected(notification_index, "ADC_SATURATION")
                raise Stage9CaptureError("ADC saturation detected; capture stopped")
            if sample_count + packet.sample_count > 3000:
                diagnostics.rejected(notification_index, "SAMPLE_WINDOW_OVERRUN")
                raise Stage9CaptureError("firmware emitted data beyond the exact 3000-sample window")
            try:
                recorder.ingest(
                    bytes(raw), host_monotonic_ns=host_monotonic_ns, host_utc=host_utc
                )
            except BaseException as exc:
                diagnostics.rejected(notification_index, rejection_code(exc))
                raise
            sample_count += packet.sample_count
            diagnostics.accepted(packet.sample_count)
            if sample_count == 3000:
                capture_done.set()
        except BaseException as exc:
            callback_error.append(exc)
            capture_done.set()

    try:
        discovery = LiveBleDiscovery(freshness_s=15.0)
        await scan_v2_adapters(discovery, timeout_s=10.0)
        candidates = discovery.fresh_candidates(plan["hardware_uid"])
        if len(candidates) != 1 or len(discovery.all_fresh()) != 1:
            raise Stage9CaptureError("expected exactly one fresh V2 source for VN-001")
        client = BleakClientAdapter(candidates[0].connection_handle, disconnected.set)
        await client.connect()
        diagnostics.connected()
        required = {CHAR_CONTROL_UUID, CHAR_DATA_UUID, CHAR_IDENTITY_UUID, CHAR_STATUS_UUID}
        available = {value.lower() for value in client.get_service_characteristic_uuids()}
        if not {value.lower() for value in required}.issubset(available):
            raise Stage9CaptureError("connected device lacks the complete Protocol V2 GATT layout")
        identity = decode_identity_value(await client.read_gatt_char(CHAR_IDENTITY_UUID))
        if identity.hardware_uid != plan["hardware_uid"] or identity.short_code != plan["short_code"]:
            raise Stage9CaptureError("advertising/GATT/planned identity mismatch")
        status_gate = await _subscribe_and_commit_profile(
            client=client,
            disconnected=disconnected,
            on_data=on_data,
            expected_uid=plan["hardware_uid"],
            expected_config_id=slot.config_id,
            acknowledgment_timeout_s=PROFILE_ACK_TIMEOUT_S,
            status_observer=diagnostics.observe_status,
        )
        diagnostics.status_committed()

        def announce_start() -> None:
            print(json.dumps({
                "event": "CAPTURE_START",
                "experiment_id": plan["experiment_id"],
                "config_id": slot.config_id,
                "repetition": slot.repetition,
                "zone": slot.zone,
                "attempt_ordinal": slot.attempt_ordinal,
                "start_utc": _utc_now(),
            }, sort_keys=True), flush=True)

        await _start_capture_once(
            client=client,
            gate=status_gate,
            disconnected=disconnected,
            announce=announce_start,
            on_start_sent=diagnostics.start_sent,
        )
        wait_capture = asyncio.create_task(capture_done.wait())
        wait_disconnect = asyncio.create_task(disconnected.wait())
        done, pending = await asyncio.wait(
            {wait_capture, wait_disconnect}, timeout=CAPTURE_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if not done:
            raise Stage9CaptureError(
                f"capture exceeded the {CAPTURE_TIMEOUT_S:g}-second transport deadline"
            )
        if wait_disconnect in done and disconnected.is_set() and sample_count < 3000:
            raise Stage9CaptureError("BLE disconnected before a complete window")
        if callback_error:
            raise callback_error[0]
        if sample_count != 3000:
            raise Stage9CaptureError(f"capture ended at {sample_count}/3000 samples")
        await client.write_gatt_char(
            CHAR_CONTROL_UUID, encode_control_packet(command=int(ControlCommand.STOP))
        )
        if exposure_only:
            # Stage 9B v2.2 records the complete window without applying any
            # numeric v1 or v2 quality accept/reject threshold. The call still
            # enforces transport, identity, configuration, timing, overflow,
            # clipping (above), and exact-window integrity fail closed.
            recorder.verify_complete_window()
        else:
            artifact = recorder.persist(batch_root / "captures")
            diagnostics.published()
            record = load_and_verify_capture(artifact.path)
            if record["register_settings"] != slot.settings.to_dict():
                raise Stage9CaptureError("persisted register provenance mismatch")
    except BaseException as exc:
        failure = exc
    finally:
        if status_gate is not None:
            status_gate.close()
        if client is not None:
            try:
                await client.write_gatt_char(
                    CHAR_CONTROL_UUID, encode_control_packet(command=int(ControlCommand.STOP))
                )
            except Exception:
                pass
            # Snapshot link loss before our own clean teardown. Bleak invokes
            # the disconnect callback for client.disconnect() too; consulting
            # the event after that call misclassifies a full-window QA failure
            # as an unexpected transport disconnect.
            unexpected_disconnect = disconnected.is_set()
            try:
                await client.disconnect()
                diagnostics.payload["disconnect_state"] = (
                    "unexpected_disconnect" if unexpected_disconnect else "disconnected"
                )
            except Exception:
                diagnostics.disconnected(clean=False)

    if failure is not None:
        data = diagnostics.payload["data"]
        rejected = data["rejected_packets"]
        if rejected:
            failure_class = rejected[0]["reason_code"]
            state = "PACKET_REJECTED"
        elif not diagnostics.payload["handshake"]["start_sent"]:
            failure_class = "PRE_START_FAILURE"
            state = "FAILED_BEFORE_START"
        elif unexpected_disconnect:
            failure_class = "DISCONNECTED_PARTIAL" if sample_count else "DISCONNECTED_NO_DATA"
            state = "DISCONNECTED"
        elif sample_count == 0:
            failure_class = "DATA_ABSENT"
            state = "TRANSPORT_TIMEOUT"
        elif sample_count < 3000:
            diagnostics.set_average_pair_rate(elapsed_s=CAPTURE_TIMEOUT_S)
            observed_rate = diagnostics.payload["data"]["observed_average_pair_rate_hz"]
            failure_class = (
                "DATA_TOO_SLOW" if observed_rate is not None and observed_rate < 99.0
                else "DATA_PARTIAL_TIMEOUT"
            )
            state = "TRANSPORT_TIMEOUT"
        elif isinstance(failure, CaptureIntegrityError):
            failure_class = "CAPTURE_INTEGRITY_REJECTED"
            state = "CAPTURE_REJECTED"
        else:
            failure_class = "FULLY_RECEIVED_NOT_FINALIZED"
            state = "FINALIZATION_FAILED"
        # Purely additive visibility into a QA rejection's actual numbers
        # (e.g. LOW_PSNR); never populated when no QA validation ran at all
        # (a transport-level failure), and never a substitute for a
        # published Stage 7 capture record.
        if isinstance(failure, CaptureIntegrityError):
            quality_metrics = getattr(failure, "quality_metrics", None)
            if quality_metrics is not None:
                diagnostics.record_quality_metrics(quality_metrics)
        diagnostics.finish(state=state, failure_class=failure_class, error=str(failure))
        try:
            diagnostic_path = diagnostics.persist(batch_root)
        except BaseException as diagnostic_error:
            raise Stage9CaptureError(
                f"{failure}; atomic transport diagnostic persistence failed: {diagnostic_error}"
            ) from failure
        if exposure_only:
            verified = load_and_verify_transport_diagnostic(diagnostic_path)
            ledger["attempts"].append({
                "attempt_ordinal": slot.attempt_ordinal,
                "zone": slot.zone,
                "config_id": slot.config_id,
                "repetition": slot.repetition,
                "completion_state": verified["completion_state"],
                "failure_class": verified["failure_class"],
                "raw_sample_pairs_retained": verified["data"]["raw_sample_pairs_retained"],
                "diagnostic_path": str(diagnostic_path.relative_to(batch_root)),
                "manifest_sha256": sha256_file(diagnostic_path / "manifest.sha256"),
            })
            write_ledger(batch_root, ledger)
        raise Stage9CaptureError(f"{failure}; diagnostic={diagnostic_path}") from failure

    if exposure_only:
        diagnostics.finish(state="EXPOSURE_ATTEMPT_RETAINED")
        diagnostic_path = diagnostics.persist(batch_root)
        verified = load_and_verify_transport_diagnostic(diagnostic_path)
        ledger["attempts"].append({
            "attempt_ordinal": slot.attempt_ordinal,
            "zone": slot.zone,
            "config_id": slot.config_id,
            "repetition": slot.repetition,
            "completion_state": verified["completion_state"],
            "failure_class": None,
            "raw_sample_pairs_retained": verified["data"]["raw_sample_pairs_retained"],
            "diagnostic_path": str(diagnostic_path.relative_to(batch_root)),
            "manifest_sha256": sha256_file(diagnostic_path / "manifest.sha256"),
        })
        write_ledger(batch_root, ledger)
        following = next_slot(plan, ledger)
        return {
            "event": "EXPOSURE_ATTEMPT_RETAINED",
            "attempt_ordinal": slot.attempt_ordinal,
            "zone": slot.zone,
            "config_id": slot.config_id,
            "repetition": slot.repetition,
            "transport_diagnostic": str(diagnostic_path),
            "batch_completed": following is None,
            "next": None if following is None else {
                "attempt_ordinal": following.attempt_ordinal,
                "zone": following.zone,
                "config_id": following.config_id,
                "repetition": following.repetition,
            },
        }

    assert artifact is not None and record is not None
    ledger["captures"].append({
        "config_id": slot.config_id,
        "repetition": slot.repetition,
        "capture_id": artifact.capture_id,
        "capture_path": str(artifact.path.relative_to(batch_root)),
        "manifest_sha256": artifact.manifest_sha256,
        "capture_start_utc": record["timing"]["capture_start_utc"],
        "capture_end_utc": record["timing"]["capture_end_utc"],
        "register_settings": record["register_settings"],
        "quality_metrics": record["quality_metrics"],
    })
    write_ledger(batch_root, ledger)
    diagnostics.ledger_created()
    diagnostics.finish(state="CAPTURE_COMMITTED")
    diagnostic_path = diagnostics.persist(batch_root)
    following = next_slot(plan, ledger, skipped_slots=skipped_slots)
    return {
        "event": "CAPTURE_COMMITTED",
        "config_id": slot.config_id,
        "repetition": slot.repetition,
        "capture_id": artifact.capture_id,
        "manifest_sha256": artifact.manifest_sha256,
        "transport_diagnostic": str(diagnostic_path),
        "batch_completed": following is None,
        "next": None if following is None else {"config_id": following.config_id, "repetition": following.repetition},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Record exactly the next authorized Stage 9 slot")
    parser.add_argument(
        "--plan",
        type=Path,
        default=BUNDLE_ROOT / "config" / "stage9b_exposure_batch_v2_2.json",
    )
    parser.add_argument("--batch-root", required=True, type=Path)
    parser.add_argument("--firmware-package", required=True, type=Path)
    parser.add_argument("--informational-baseline-disposition", type=Path)
    parser.add_argument("--operator-ready", required=True)
    args = parser.parse_args()
    try:
        plan = load_plan(args.plan)
        if args.operator_ready != plan["experiment_id"]:
            raise Stage9CaptureError("operator-ready token does not match the frozen experiment")
        result = asyncio.run(capture_next(
            plan_path=args.plan.resolve(),
            batch_root=args.batch_root.resolve(),
            firmware_package=args.firmware_package.resolve(),
            informational_baseline_disposition=(
                None
                if args.informational_baseline_disposition is None
                else args.informational_baseline_disposition.resolve()
            ),
        ))
    except (Stage9CaptureError, Stage9PlanError, CaptureIntegrityError, OSError, ValueError) as exc:
        print(f"capture rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
