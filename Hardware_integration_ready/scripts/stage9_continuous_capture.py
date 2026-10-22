#!/usr/bin/env python3
"""Continuous-stream experiment: stabilize (live preflight guidance), then
collect N consecutive fixed-length windows from a single BLE stream with no
restart (no STOP/START, no NEW_SESSION) between windows.

Astra's design for this run: single point (fingertip, to validate the
measurement chain -- not a location claim), config 17 (chosen for its best
average in the original 18-attempt series, not because it's proven), fixture
= hand/cable support + light steady pressure + a light-sealing cover around
the sensor that does not block the optical window (external light is a
documented MAX30102 noise source). No filter/threshold changes here or
anywhere in this script to obtain a passing score -- this only records and
reports.

Reports red_cardiac_snr_db, ir_cardiac_snr_db, and periodicity_index
SEPARATELY per window (never averaged) -- averaging would hide whether a
problem is startup-only (window 1 different from 2/3) or ongoing drift
(all three windows similarly poor).

Requires the config_id being used to already have a real, on-device-
calibrated CONTACT_CALIBRATION entry in receiver/preflight_stability.py
(see scripts/stage9_calibrate_contact.py) -- otherwise preflight ABSTAINs
unconditionally with reason NO_CONTACT_CALIBRATION, by design (no silent
fallback to a different config).

Also reports the placeholder_exclusion diagnostic for window 1 (the true
session start) as a live check of the permanent firmware acquisition fix:
excluded_count should read 0 once fixed firmware is flashed (see
firmware/veinnet_sensor_v1/veinnet_sensor_v1.ino's
g_stage9DropLeadingPlaceholder) -- a nonzero count here means the device is
still running pre-fix firmware.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BUNDLE_ROOT / "receiver"))

from bleak import BleakClient, BleakScanner  # noqa: E402

from capture_quality_metrics import cardiac_channel_metrics  # noqa: E402
from extended_quality_metrics import periodicity_index  # noqa: E402
from placeholder_exclusion import build_corrected_view  # noqa: E402
from preflight_stability import DEFAULT_MIN_SECONDS, evaluate_preflight, write_abstain_log  # noqa: E402
from notification_archive import NotificationJournal
from protocol_decoder import ControlCommand, decode_data_packet, encode_control_packet  # noqa: E402
from ble_uuids import CHAR_CONTROL_UUID, CHAR_DATA_UUID  # noqa: E402
from window_assembler import EFFECTIVE_RATE_HZ  # noqa: E402

OUT_DIR = BUNDLE_ROOT / "var" / "stage9-evidence" / "continuous-stream"
ABSTAIN_DIR = OUT_DIR / "abstain"

WINDOW_SAMPLES = 3000
PREFLIGHT_MIN_SAMPLES = round(DEFAULT_MIN_SECONDS * EFFECTIVE_RATE_HZ)
MAX_PREFLIGHT_RETRIES = 6
SETUP_COUNTDOWN_S = 5
RETRY_PAUSE_S = 2.0
CAPTURE_TIMEOUT_S = 45.0
STREAM_TIMEOUT_S = 150.0  # generous ceiling for N*30s windows on one stream


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _run_one_preflight_pass(client, config_id: int):
    red: list[int] = []
    ir: list[int] = []
    done = asyncio.Event()
    verdict_holder: list[str] = []
    result_holder: list[object] = []

    journal = NotificationJournal(OUT_DIR / "notification-journals")

    def on_data(_handle, raw):
        journal.append(bytes(raw))
        try:
            pkt = decode_data_packet(bytes(raw))
        except Exception as exc:
            print(f"    !! decode error: {exc}")
            return
        red.extend(pkt.red)
        ir.extend(pkt.ir)
        if not verdict_holder and len(red) >= PREFLIGHT_MIN_SAMPLES:
            result = evaluate_preflight(red, ir, config_id=config_id)
            if result.status in ("PASS", "ABSTAIN"):
                verdict_holder.append(result.status)
                result_holder.append(result)
                done.set()

    await client.start_notify(CHAR_DATA_UUID, on_data)
    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.NEW_SESSION), response=True
    )
    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.START), response=True
    )
    try:
        await asyncio.wait_for(done.wait(), timeout=CAPTURE_TIMEOUT_S)
    except asyncio.TimeoutError:
        result = evaluate_preflight(red, ir, config_id=config_id)
        verdict_holder.append(result.status)
        result_holder.append(result)

    verdict = verdict_holder[0]
    result = result_holder[0]
    if verdict == "ABSTAIN":
        await client.write_gatt_char(
            CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.STOP), response=True
        )
        await client.stop_notify(CHAR_DATA_UUID)
    return verdict, red, ir, result, on_data


async def _collect_n_windows(client, red: list[int], ir: list[int], on_data, n_windows: int) -> None:
    """Already streaming and PASSed preflight; keep collecting on the SAME
    stream (no power-cycle, no NEW_SESSION) until n_windows * WINDOW_SAMPLES
    total samples are in hand."""
    target = n_windows * WINDOW_SAMPLES
    deadline = time.monotonic() + STREAM_TIMEOUT_S
    while len(red) < target:
        if time.monotonic() > deadline:
            raise RuntimeError(f"continuous stream stalled at {len(red)}/{target} samples")
        await asyncio.sleep(0.05)
    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.STOP), response=True
    )
    await client.stop_notify(CHAR_DATA_UUID)


def _window_metrics(red_window: list[int], ir_window: list[int], window_index: int, fresh_session: bool) -> dict:
    red_cardiac = cardiac_channel_metrics(red_window, EFFECTIVE_RATE_HZ)
    ir_cardiac = cardiac_channel_metrics(ir_window, EFFECTIVE_RATE_HZ)
    red_periodicity = periodicity_index(red_window, EFFECTIVE_RATE_HZ)
    ir_periodicity = periodicity_index(ir_window, EFFECTIVE_RATE_HZ)

    corrected = build_corrected_view(red_window, ir_window, fresh_session=fresh_session)

    return {
        "window_index": window_index,
        "fresh_session": fresh_session,
        "samples": len(red_window),
        "red_cardiac_snr_db": red_cardiac.cardiac_snr_db,
        "ir_cardiac_snr_db": ir_cardiac.cardiac_snr_db,
        "red_periodicity_index": red_periodicity,
        "ir_periodicity_index": ir_periodicity,
        "red_cardiac_metrics": red_cardiac.to_dict(),
        "ir_cardiac_metrics": ir_cardiac.to_dict(),
        "placeholder_exclusion_check": corrected.to_dict(),
    }


async def run_experiment(client, *, config_id: int, zone: str, fixture_condition: str, n_windows: int) -> dict:
    print(f"\n=== CONTINUOUS-STREAM EXPERIMENT -- zone={zone} config={config_id} "
          f"fixture_condition={fixture_condition} windows={n_windows} ===")
    print(f"  Set up: {fixture_condition}. Hold steady through the whole run.")
    for s in range(SETUP_COUNTDOWN_S, 0, -1):
        print(f"    starting preflight in {s}...")
        await asyncio.sleep(1.0)

    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.SET_PROFILE, arg0=config_id), response=True
    )
    await asyncio.sleep(0.3)

    abstain_log_paths: list[str] = []
    abstain_reasons: list[str] = []
    result = None
    red: list[int] = []
    ir: list[int] = []
    on_data = None
    retry = 0

    for retry in range(1, MAX_PREFLIGHT_RETRIES + 1):
        verdict, red, ir, result, on_data = await _run_one_preflight_pass(client, config_id)
        if verdict == "PASS":
            print(f"  GOOD -- preflight passed on try {retry}/{MAX_PREFLIGHT_RETRIES}, "
                  f"starting continuous {n_windows}x30s-window capture now (no power-cycle).")
            break
        print(f"  not stable yet ({result.reason}) -- hold position. try {retry}/{MAX_PREFLIGHT_RETRIES}")
        for b in result.blocks:
            print(f"    block {b.index}: red_dc={b.red_dc:.1f} ir_dc={b.ir_dc:.1f}")
        abstain_path = write_abstain_log(
            ABSTAIN_DIR,
            hardware_uid="A90AFDAF672D66F1",
            config_id=config_id,
            result=result,
            red=red,
            ir=ir,
            elapsed_s=len(red) / EFFECTIVE_RATE_HZ,
        )
        abstain_log_paths.append(str(abstain_path))
        abstain_reasons.append(result.reason)
        if retry < MAX_PREFLIGHT_RETRIES:
            await asyncio.sleep(RETRY_PAUSE_S)
    else:
        return {
            "outcome": "ABSTAIN_EXHAUSTED",
            "abstain_count": len(abstain_reasons),
            "abstain_reasons": abstain_reasons,
            "abstain_log_paths": abstain_log_paths,
        }

    await _collect_n_windows(client, red, ir, on_data, n_windows)

    windows = []
    for i in range(n_windows):
        start = i * WINDOW_SAMPLES
        end = start + WINDOW_SAMPLES
        red_window = red[start:end]
        ir_window = ir[start:end]
        # Only window 0 coincides with the real NEW_SESSION+START handshake;
        # windows 1..N-1 are continuations of the same stream (no restart),
        # so provenance says no confirmed placeholder in them.
        fresh_session = (i == 0)
        w = _window_metrics(red_window, ir_window, i, fresh_session)
        w["raw_red"] = red_window
        w["raw_ir"] = ir_window
        windows.append(w)
        print(f"  window {i}: red_snr={w['red_cardiac_snr_db']:.2f}dB ir_snr={w['ir_cardiac_snr_db']:.2f}dB "
              f"red_periodicity={w['red_periodicity_index']:.3f} ir_periodicity={w['ir_periodicity_index']:.3f} "
              f"placeholder_excluded={w['placeholder_exclusion_check']['excluded_count']}")

    return {
        "outcome": "CAPTURED",
        "zone": zone,
        "config_id": config_id,
        "fixture_condition": fixture_condition,
        "preflight_pass_try": retry,
        "abstain_count": len(abstain_reasons),
        "abstain_reasons": abstain_reasons,
        "abstain_log_paths": abstain_log_paths,
        "windows": windows,
        "captured_utc": _utc_now(),
    }


def print_report(record: dict) -> None:
    print("\n\n========== CONTINUOUS-STREAM EXPERIMENT REPORT ==========")
    if record["outcome"] != "CAPTURED":
        print(f"outcome: {record['outcome']} reasons={record.get('abstain_reasons')}")
        return
    print(f"zone={record['zone']} config_id={record['config_id']} "
          f"fixture_condition={record['fixture_condition']}")
    for w in record["windows"]:
        print(f"\n-- window {w['window_index']} (fresh_session={w['fresh_session']}) --")
        print(f"  red_cardiac_snr_db = {w['red_cardiac_snr_db']:.2f} dB")
        print(f"  ir_cardiac_snr_db  = {w['ir_cardiac_snr_db']:.2f} dB")
        print(f"  red_periodicity_index = {w['red_periodicity_index']:.4f}")
        print(f"  ir_periodicity_index  = {w['ir_periodicity_index']:.4f}")
        pe = w["placeholder_exclusion_check"]
        print(f"  placeholder_exclusion_check: excluded_count={pe['excluded_count']} "
              f"valid_sample_count={pe['valid_sample_count']} provenance={pe['provenance']}")
    print(f"\nTotal ABSTAIN events (including retries before the stream started): "
          f"{record['abstain_count']}")
    print(f"ABSTAIN reasons: {record['abstain_reasons']}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-id", type=int, default=17)
    parser.add_argument("--zone", default="FINGERTIP")
    parser.add_argument("--fixture-condition", default="fixture_light_sealed_single_point")
    parser.add_argument("--n-windows", type=int, default=3)
    args = parser.parse_args()

    print("Scanning for VeinNetSensorV2 ...")
    device = await BleakScanner.find_device_by_filter(lambda d, adv: d.name == "VeinNetSensorV2", timeout=15.0)
    if device is None:
        print("device not found")
        return 1
    print(f"Found device: {device.address}")

    async with BleakClient(device) as client:
        record = await run_experiment(
            client,
            config_id=args.config_id,
            zone=args.zone,
            fixture_condition=args.fixture_condition,
            n_windows=args.n_windows,
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"continuous_{args.zone.lower()}_config{args.config_id}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"\nFull record saved to {out_path}")

    print_report(record)
    return 0 if record["outcome"] == "CAPTURED" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
