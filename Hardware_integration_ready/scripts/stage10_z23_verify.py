#!/usr/bin/env python3
"""Execute one bounded physical Stage 10 Z23 POC verification case."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BUNDLE_ROOT.parent
sys.path[:0] = [str(BUNDLE_ROOT / "receiver"), str(BUNDLE_ROOT / "scripts")]

from bleak_adapter import BleakClientAdapter, scan_v2_adapters
from ble_identity import canonical_uid, decode_identity_value
from ble_uuids import CHAR_CONTROL_UUID, CHAR_DATA_UUID, CHAR_IDENTITY_UUID, CHAR_STATUS_UUID
from live_ble_discovery import LiveBleDiscovery
from notification_archive import NotificationJournal, replay_journal
from protocol_decoder import ControlCommand, decode_status_packet, encode_control_packet
from stage10_z23_controller import (
    BoundedExposureController,
    ExposurePair,
    Stage10PacketStream,
    Stage10PolicyError,
    load_stage10_z23_policy,
    sha256_file,
)

UID = "A90AFDAF672D66F1"
SHORT_CODE = "VN-001"
CASES = {"SUCCESS", "NO_CONTACT", "INDEPENDENT_IR_ADJUSTMENT", "INDEPENDENT_RED_ADJUSTMENT"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def write_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def ingest_after_start(stream: Stage10PacketStream, raw: bytes, host_ns: int, start_ns: int):
    # STOP-flight packets remain in the journal but cannot enter the next session.
    return None if host_ns < start_ns else stream.ingest(raw, host_monotonic_ns=host_ns)


async def execute(case: str, output_dir: Path) -> int:
    if case not in CASES:
        raise ValueError("unknown frozen physical case")
    if output_dir.exists():
        raise RuntimeError("physical-case output already exists; retained attempts are immutable")
    output_dir.mkdir(parents=True, mode=0o700)
    journal = NotificationJournal(output_dir / "notifications")
    loaded = load_stage10_z23_policy()
    disconnected = asyncio.Event()
    data_queue: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue()
    status_queue: asyncio.Queue[tuple[object, int]] = asyncio.Queue()
    controls: list[dict] = []
    readbacks: list[dict] = []
    decisions: list[dict] = []
    client = None
    stream = None
    outcome = "ABSTAIN"
    reason = None
    preflight_elapsed_s = None
    measurement_window = None
    window_finalization_integrity_check_s = None
    controller = None
    started_utc = utc_now()

    def on_data(_sender, raw) -> None:
        now_ns = time.monotonic_ns()
        payload = bytes(raw)
        journal.append(payload, host_monotonic_ns=now_ns, host_utc=utc_now())
        data_queue.put_nowait((payload, now_ns))

    def on_status(_sender, raw) -> None:
        now_ns = time.monotonic_ns()
        try:
            status_queue.put_nowait((decode_status_packet(bytes(raw)), now_ns))
        except Exception:
            status_queue.put_nowait((None, now_ns))

    async def control(command: ControlCommand, *, arg0: int = 0) -> int:
        if disconnected.is_set():
            raise Stage10PolicyError("DISCONNECTION_OR_PERSISTENCE_FAILURE")
        sent_ns = time.monotonic_ns()
        await client.write_gatt_char(
            CHAR_CONTROL_UUID, encode_control_packet(command=int(command), arg0=arg0)
        )
        controls.append({"command": command.name, "arg0": arg0, "committed_monotonic_ns": sent_ns})
        return sent_ns

    async def set_and_verify(pair: ExposurePair) -> None:
        while not status_queue.empty():
            status_queue.get_nowait()
        config_id = loaded.pairs_to_config[pair]
        await control(ControlCommand.SET_PROFILE, arg0=config_id)
        fence = time.monotonic_ns()
        await client.write_gatt_char(
            CHAR_CONTROL_UUID,
            encode_control_packet(command=int(ControlCommand.TIME_SYNC), arg_u64=time.time_ns() // 1000),
        )
        controls.append({"command": "TIME_SYNC", "arg0": 0, "committed_monotonic_ns": fence})
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                status, received_ns = await asyncio.wait_for(status_queue.get(), deadline - time.monotonic())
            except asyncio.TimeoutError as exc:
                raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE") from exc
            if status is None or received_ns < fence:
                continue
            if canonical_uid(status.hardware_uid) != UID or status.fifo_overflow_count:
                raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE")
            if status.sensor_config_id != config_id:
                continue
            readbacks.append({
                "target": pair.to_dict(), "config_id": config_id,
                "status_sensor_config_id": status.sensor_config_id,
                "verified_monotonic_ns": received_ns,
                "register_readback_semantics": "firmware publishes this config only after exact RED/IR register readback",
            })
            return
        raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE")

    async def start_session() -> int:
        await control(ControlCommand.NEW_SESSION)
        return await control(ControlCommand.START)

    try:
        discovery = LiveBleDiscovery(freshness_s=15)
        await scan_v2_adapters(discovery, timeout_s=10)
        candidates = discovery.fresh_candidates(UID)
        if len(candidates) != 1:
            raise Stage10PolicyError("DISCONNECTION_OR_PERSISTENCE_FAILURE")
        client = BleakClientAdapter(candidates[0].connection_handle, disconnected.set)
        await asyncio.wait_for(client.connect(), 10)
        identity = decode_identity_value(await client.read_gatt_char(CHAR_IDENTITY_UUID))
        if canonical_uid(identity.hardware_uid) != UID or identity.short_code != SHORT_CODE:
            raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE")
        await client.start_notify(CHAR_STATUS_UUID, on_status)
        await client.start_notify(CHAR_DATA_UUID, on_data)

        controller = BoundedExposureController(loaded)
        transform = None
        if case == "INDEPENDENT_IR_ADJUSTMENT":
            transform = lambda red, ir: (red, [10000] * 100)
        elif case == "INDEPENDENT_RED_ADJUSTMENT":
            transform = lambda red, ir: ([10000] * 100, ir)
        stream = Stage10PacketStream(controller, hardware_uid=UID, evaluation_transform=transform)
        await set_and_verify(stream.current_pair)
        start_ns = await start_session()
        while not stream.measurement_complete:
            remaining = max(0.01, 5.0 - (time.monotonic() - controller.started)) if not stream.converged else 40.0
            try:
                raw, host_ns = await asyncio.wait_for(data_queue.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise Stage10PolicyError("PREFLIGHT_TIMEOUT" if not stream.converged else "DISCONNECTION_OR_PERSISTENCE_FAILURE") from exc
            decision = ingest_after_start(stream, raw, host_ns, start_ns)
            if decision is None:
                continue
            decisions.append({
                "status": decision.status, "pair": decision.pair.to_dict(),
                "next_pair": decision.next_pair.to_dict() if decision.next_pair else None,
                "evaluation": decision.evaluation.to_dict(),
                "safe_decision_input_injected": stream.evaluation_transform_used and len(decisions) == 0,
            })
            if decision.status == "ADJUST":
                await control(ControlCommand.STOP)
                await set_and_verify(decision.next_pair)
                stream.reset_for_adjustment(decision.next_pair)
                start_ns = await start_session()
            elif decision.status == "CONVERGED":
                preflight_elapsed_s = controller.assert_deadline()
        finalization_start_ns = time.monotonic_ns()
        measurement_window = stream.measurement_window()
        window_finalization_integrity_check_s = (time.monotonic_ns() - finalization_start_ns) / 1e9
        await control(ControlCommand.STOP)
        # Retain legitimate STOP-flight packets without extending the window.
        stop_deadline = time.monotonic() + 0.35
        while time.monotonic() < stop_deadline:
            try:
                raw, host_ns = await asyncio.wait_for(data_queue.get(), stop_deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            stream.ingest(raw, host_monotonic_ns=host_ns)
        outcome = "INTEGRITY_RETAINED"
    except Stage10PolicyError as exc:
        reason = str(exc)
        if controller is not None and stream is not None and preflight_elapsed_s is None and not stream.converged:
            preflight_elapsed_s = time.monotonic() - controller.started
        if client is not None:
            try:
                await control(ControlCommand.STOP)
            except Exception:
                pass
    except BaseException as exc:
        reason = "DISCONNECTION_OR_PERSISTENCE_FAILURE: " + str(exc)
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 3)
            except Exception:
                outcome = "ABSTAIN"
                reason = "DISCONNECTION_OR_PERSISTENCE_FAILURE"

    result = {
        "schema_version": "1.0.0", "record_type": "stage10_z23_poc_physical_case",
        "case": case, "scope": "Z23_ENGINEERING_POC_ONLY", "started_utc": started_utc,
        "ended_utc": utc_now(), "hardware_uid": UID, "short_code": SHORT_CODE,
        "policy_id": loaded.payload["policy_id"], "policy_sha256": loaded.policy_sha256,
        "authorization_path": "stage9_external_evidence/STAGE10_Z23_POC_BEGIN_AUTHORIZATION.json",
        "authorization_sha256": sha256_file(PROJECT_ROOT / "stage9_external_evidence/STAGE10_Z23_POC_BEGIN_AUTHORIZATION.json"),
        "outcome": outcome, "reason": reason, "preflight_elapsed_s": preflight_elapsed_s,
        "window_finalization_integrity_check_s": window_finalization_integrity_check_s,
        "deadline_s": 5.0, "controls": controls, "readbacks": readbacks, "decisions": decisions,
        "measurement": None if measurement_window is None else {
            "session_id": measurement_window.sensor_session_id,
            "config_id": measurement_window.sensor_config_id,
            "first_index": measurement_window.first_sample_index,
            "last_index": measurement_window.last_sample_index,
            "pairs": len(measurement_window.red),
            "device_first_to_last_sample_s": (measurement_window.last_sample_index - measurement_window.first_sample_index) / 100.0,
            "red": measurement_window.red, "ir": measurement_window.ir,
            "integrity": measurement_window.integrity.__dict__,
        },
        "notification_journal": str(journal.path.relative_to(output_dir)),
        "notification_count": journal.index,
        "failed_attempt_preserved": outcome == "ABSTAIN",
        "cardiac_quality_assessed": False,
    }
    write_file(output_dir / "result.json", canonical(result))
    replay_journal(journal.path)
    files = [output_dir / "result.json", journal.path]
    manifest = "".join(f"{sha256_file(path)}  {path.relative_to(output_dir)}\n" for path in files)
    write_file(output_dir / "manifest.sha256", manifest.encode())
    return 0 if outcome == "INTEGRITY_RETAINED" else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=sorted(CASES))
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    return asyncio.run(execute(args.case, args.output_dir))


if __name__ == "__main__":
    raise SystemExit(main())
