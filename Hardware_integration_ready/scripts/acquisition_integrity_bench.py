#!/usr/bin/env python3
"""One opt-in V3 fixture capture. Never runs inference or publishes vitals."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE / "receiver"))
sys.path.insert(0, str(BUNDLE / "scripts"))
from bench_capture_integrity import BenchCapture, WarmupBenchCapture
from ble_identity import decode_identity_value
from ble_uuids import CHAR_CONTROL_UUID, CHAR_DATA_UUID, CHAR_FINAL_UUID, CHAR_IDENTITY_UUID, CHAR_STATUS_UUID
from protocol_decoder import ControlCommand, encode_control_packet
from stage9_capture import _subscribe_and_commit_profile

CAPTURE_TIMEOUT_S = 45.0
FIRST_DATA_TIMEOUT_S = 2.0
WARMUP_CAPTURE_TIMEOUT_S = 53.0


async def execute_client(client, *, uid, short_code, output, disconnected,
                         nonce=None, clock=time.monotonic, pause=asyncio.sleep, warmup=False):
    """Actual executor seam: tests supply a BLE client, never synthetic fallback."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    capture = (WarmupBenchCapture if warmup else BenchCapture)(uid=int(uid, 16), nonce=nonce or secrets.randbits(64) or 1)
    gate = None
    started = False
    loop = asyncio.get_running_loop()
    def receive(channel, raw):
        capture.receive(channel, bytes(raw), clock())
    def notification(channel):
        return lambda _sender, raw: loop.call_soon_threadsafe(receive, channel, bytes(raw))
    class ObservedClient:
        def __getattr__(self, name):
            return getattr(client, name)
        async def start_notify(self, key, callback):
            if key == CHAR_STATUS_UUID:
                original = callback
                def callback(sender, raw):
                    loop.call_soon_threadsafe(receive, "STATUS", bytes(raw))
                    original(sender, raw)
            await client.start_notify(key, callback)
        async def write_gatt_char(self, key, raw, response=True):
            capture.journal.append(dict(channel="CONTROL", event="WRITE_REQUEST",
                raw_hex=bytes(raw).hex(), host_monotonic_s=clock()))
            await client.write_gatt_char(key, raw, response=response)
            capture.journal.append(dict(channel="CONTROL", event="WRITE_ACK",
                raw_hex=bytes(raw).hex(), host_monotonic_s=clock()))
    observed = ObservedClient()
    try:
        required = {CHAR_CONTROL_UUID, CHAR_DATA_UUID, CHAR_FINAL_UUID, CHAR_IDENTITY_UUID, CHAR_STATUS_UUID}
        if not required <= {s.lower() for s in client.get_service_characteristic_uuids()}:
            raise RuntimeError("V3 bench final-witness characteristic unavailable")
        identity = decode_identity_value(await client.read_gatt_char(CHAR_IDENTITY_UUID))
        if identity.hardware_uid != uid or identity.short_code != short_code:
            raise RuntimeError("GATT identity/code mismatch")
        await client.start_notify(CHAR_FINAL_UUID, notification("FINAL"))
        gate = await _subscribe_and_commit_profile(
            client=observed, disconnected=disconnected, on_data=notification("DATA"),
            expected_uid=uid, expected_config_id=25,
            status_observer=lambda status: None)
        # Subsequent STATUS is retained but is never an acquisition witness.
        # Replace the subscription callback through the client's existing API.
        await client.stop_notify(CHAR_STATUS_UUID)
        await client.start_notify(CHAR_STATUS_UUID, notification("STATUS"))
        if capture.terminal or disconnected.is_set():
            raise RuntimeError("pre-START DATA fault or disconnect")
        gate.claim_start()
        await observed.write_gatt_char(CHAR_CONTROL_UUID,
            encode_control_packet(command=int(ControlCommand.NEW_SESSION)), response=True)
        started = True
        start = clock()
        await observed.write_gatt_char(CHAR_CONTROL_UUID,
            encode_control_packet(command=int(ControlCommand.WARMUP_BENCH_START if warmup else ControlCommand.BENCH_START), arg0=1 if warmup else 0,
                                  arg_u64=capture.nonce), response=True)
        while not capture.terminal:
            now = clock()
            capture.tick(now)
            if disconnected.is_set():
                capture.finish_unverified("DISCONNECTED")
            elif now - start >= (WARMUP_CAPTURE_TIMEOUT_S if warmup else CAPTURE_TIMEOUT_S):
                capture.finish_unverified("CAPTURE_TIMEOUT")
            elif warmup and capture.warmup_count < capture.warmup_pairs and now-start >= capture.warmup_deadline_s:
                capture.finish_unverified("WARMUP_DEADLINE")
            elif not (capture.packets or (warmup and capture.warmup_packets)) and now - start >= FIRST_DATA_TIMEOUT_S:
                capture.finish_unverified("NO_DATA_WITHIN_2_S")
            if not capture.terminal:
                await pause(0.01)
    except Exception as exc:
        capture.finish_unverified(type(exc).__name__ + ": " + str(exc))
    finally:
        if gate:
            gate.close()
        if started and not disconnected.is_set():
            # Best-effort stop only; no new START, reconnect or capture retry.
            try:
                await observed.write_gatt_char(CHAR_CONTROL_UUID,
                    encode_control_packet(command=int(ControlCommand.STOP)), response=True)
            except Exception:
                pass
        await asyncio.sleep(0)  # flush queued notifications into retained journal
        (output / "capture.json").write_text(json.dumps(capture.record(), indent=2) + "\n")
        (output / "notifications.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in capture.journal))
        names = ["capture.json", "notifications.jsonl"]
    if warmup:
        record = capture.record()
        (output / "warmup.json").write_text(json.dumps(dict(
            schema_version="experimental-active-warmup-1.0.0",
            hardware_uid=record["hardware_uid"], acquisition_nonce=record["acquisition_nonce"],
            timestamp_semantics=record["timestamp_semantics"],
            final_witness=record["final_witness"],
            host_events=[row for row in capture.journal if row.get("phase") == "warmup"],
            **record["warmup"]), indent=2) + "\n")
        names.append("warmup.json")
    (output / "sha256.json").write_text(json.dumps({
            name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in names}, indent=2) + "\n")
    return capture


async def main_async(args):
    from bleak_adapter import BleakClientAdapter, scan_v2_adapters
    from live_ble_discovery import LiveBleDiscovery
    uid = args.uid.upper()
    if len(uid) != 16 or any(c not in "0123456789ABCDEF" for c in uid):
        raise ValueError("canonical 16-digit UID required")
    discovery = LiveBleDiscovery(freshness_s=45)
    await scan_v2_adapters(discovery, timeout_s=40)
    candidates = discovery.fresh_candidates(uid)
    if len(candidates) != 1:
        raise RuntimeError("exactly one fresh matching UID required; no retry")
    disconnected = asyncio.Event()
    client = BleakClientAdapter(candidates[0].connection_handle, disconnected.set)
    await client.connect()
    try:
        result = await execute_client(client, uid=uid, short_code=args.code,
                                      output=args.output, disconnected=disconnected, warmup=True)
        print(result.result, result.reason)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", required=True)
    parser.add_argument("--code", required=True, help="printed VN-NNN code")
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(main_async(parser.parse_args()))
