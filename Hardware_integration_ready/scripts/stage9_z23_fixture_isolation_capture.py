#!/usr/bin/env python3
"""Stage 9 Z23-only decision experiment: plain contact vs. fixture+optical
isolation, config 16, 4 repetitions per condition (8 captures total).

This is a small engineering decision experiment, not a formal Stage 9B
exposure batch -- it deliberately does not go through the ledger/manifest/
contract apparatus in stage9_experiment.py / stage9_capture.py (that system
is hash-bound to STAGE9B-VN001-EXPOSURE-001 and its own contract). ADC range,
LED current, and filters are never touched here; only the physical contact
setup (fixture_condition) changes between attempts, driven by
config/stage9_z23_fixture_isolation_batch.json.

Live preflight guidance loop, per attempt:
  1. Print a countdown so the operator can set up the attempt's
     fixture_condition.
  2. START streaming. Buffer samples. As soon as enough samples exist for a
     preflight verdict (~1.2s), call evaluate_preflight() on the buffer so
     far.
       - ABSTAIN: print the reason and the block DC values, STOP, wait, and
         retry (new START) up to MAX_PREFLIGHT_RETRIES -- consistent with the
         priority-2 design (bounded rule, no silent skip).
       - PASS: print "GOOD -- starting capture now" and keep streaming
         WITHOUT stopping/restarting -- the preflight samples are the prefix
         of the 3000-sample measurement window, not a discarded capture.
  3. Once 3000 total samples are collected after a PASS, STOP and compute
     cardiac_channel_metrics (red/ir) + extended_quality_metrics on the full
     window.
  4. Persist one JSON record per attempt (fixture_condition, preflight
     result, quality metrics) and print a final report grouped by condition.
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
from extended_quality_metrics import extended_quality_metrics, periodicity_index  # noqa: E402
from placeholder_exclusion import build_corrected_view  # noqa: E402
from preflight_stability import (  # noqa: E402
    DEFAULT_MIN_SECONDS,
    evaluate_preflight,
    write_abstain_log,
)
from notification_archive import NotificationJournal
from protocol_decoder import ControlCommand, decode_data_packet, encode_control_packet  # noqa: E402
from ble_uuids import CHAR_CONTROL_UUID, CHAR_DATA_UUID  # noqa: E402
from window_assembler import EFFECTIVE_RATE_HZ  # noqa: E402

PLAN_PATH = BUNDLE_ROOT / "config" / "stage9_z23_fixture_isolation_batch.json"
OUT_DIR = BUNDLE_ROOT / "var" / "stage9-evidence" / "z23-fixture-isolation"
ABSTAIN_DIR = OUT_DIR / "abstain"

WINDOW_SAMPLES = 3000
PREFLIGHT_MIN_SAMPLES = round(DEFAULT_MIN_SECONDS * EFFECTIVE_RATE_HZ)  # ~100
MAX_PREFLIGHT_RETRIES = 6
SETUP_COUNTDOWN_S = 5
RETRY_PAUSE_S = 2.0
CAPTURE_TIMEOUT_S = 45.0


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _run_one_preflight_pass(client, config_id: int) -> tuple[str, list[int], list[int], object]:
    """START, stream until a preflight verdict is reached or CAPTURE_TIMEOUT_S
    elapses, STOP if ABSTAIN. Returns (verdict, red_so_far, ir_so_far,
    PreflightResult). On PASS the caller keeps streaming (does not STOP)."""
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
            if result.status == "PASS" or result.status == "ABSTAIN":
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


async def _finish_window_after_pass(client, red: list[int], ir: list[int], on_data) -> None:
    """Already streaming and PASSed preflight; keep collecting on the same
    stream (no power-cycle) until the window reaches WINDOW_SAMPLES."""
    deadline = time.monotonic() + CAPTURE_TIMEOUT_S
    while len(red) < WINDOW_SAMPLES:
        if time.monotonic() > deadline:
            raise RuntimeError(f"capture stalled at {len(red)}/{WINDOW_SAMPLES} samples")
        await asyncio.sleep(0.05)
    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.STOP), response=True
    )
    await client.stop_notify(CHAR_DATA_UUID)


async def run_attempt(client, attempt: dict, total_attempts: int) -> dict:
    config_id = attempt["config_id"]
    condition = attempt["fixture_condition"]
    ordinal = attempt["attempt_ordinal"]

    print(f"\n=== ATTEMPT {ordinal}/{total_attempts} -- zone {attempt['zone']} config {config_id} "
          f"condition={condition} rep={attempt['repetition']} ===")
    print(f"  Set up condition '{condition}' now.")
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

    for retry in range(1, MAX_PREFLIGHT_RETRIES + 1):
        verdict, red, ir, result, on_data = await _run_one_preflight_pass(client, config_id)
        if verdict == "PASS":
            print(f"  GOOD -- preflight passed on try {retry}/{MAX_PREFLIGHT_RETRIES}, "
                  f"starting capture now (no power-cycle).")
            break
        print(f"  not stable yet ({result.reason}) -- hold position. try {retry}/{MAX_PREFLIGHT_RETRIES}")
        for b in result.blocks:
            print(f"    block {b.index}: red_dc={b.red_dc:.1f} ir_dc={b.ir_dc:.1f}")
        abstain_path = write_abstain_log(
            ABSTAIN_DIR,
            hardware_uid=attempt.get("hardware_uid", "unknown"),
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
            "attempt_ordinal": ordinal,
            "zone": attempt["zone"],
            "config_id": config_id,
            "fixture_condition": condition,
            "repetition": attempt["repetition"],
            "outcome": "ABSTAIN_EXHAUSTED",
            "abstain_count": len(abstain_reasons),
            "abstain_reasons": abstain_reasons,
            "abstain_log_paths": abstain_log_paths,
            "captured_utc": _utc_now(),
        }

    await _finish_window_after_pass(client, red, ir, on_data)
    red = red[:WINDOW_SAMPLES]
    ir = ir[:WINDOW_SAMPLES]

    red_cardiac = cardiac_channel_metrics(red, EFFECTIVE_RATE_HZ)
    ir_cardiac = cardiac_channel_metrics(ir, EFFECTIVE_RATE_HZ)
    extended = extended_quality_metrics(red, ir, EFFECTIVE_RATE_HZ)

    # DIAGNOSTIC ONLY -- does not replace or feed into the raw/frozen metrics
    # above. Every attempt in this script is provably a fresh session: the
    # winning _run_one_preflight_pass() call issued NEW_SESSION+START at its
    # start, and the window continues on that same stream with no further
    # power-cycle (see priority-2's "pass -> start real window immediately,
    # no power-cycle" design), so fresh_session=True here is explicit
    # protocol-level provenance, not a guess. See receiver/placeholder_exclusion.py.
    corrected = build_corrected_view(red, ir, fresh_session=True)
    diagnostic = {
        "excluded_count": corrected.excluded_count,
        "valid_sample_count": corrected.valid_sample_count,
        "metric_version": corrected.metric_version,
        "provenance": corrected.provenance,
        "red_periodicity_index": periodicity_index(corrected.red, EFFECTIVE_RATE_HZ),
        "ir_periodicity_index": periodicity_index(corrected.ir, EFFECTIVE_RATE_HZ),
        "red_cardiac_snr_db": cardiac_channel_metrics(corrected.red, EFFECTIVE_RATE_HZ).cardiac_snr_db,
        "ir_cardiac_snr_db": cardiac_channel_metrics(corrected.ir, EFFECTIVE_RATE_HZ).cardiac_snr_db,
        "caveat": (
            "diagnostic-only view with the one confirmed firmware placeholder "
            "sample excluded by session provenance; NOT a validated heart "
            "rate and NOT evidence that cardiac_snr_db is now positive -- "
            "the raw/frozen fields above are the ones of record."
        ),
    }

    record = {
        "attempt_ordinal": ordinal,
        "zone": attempt["zone"],
        "config_id": config_id,
        "fixture_condition": condition,
        "repetition": attempt["repetition"],
        "outcome": "CAPTURED",
        "samples_evaluated": len(red),
        "preflight_pass_try": retry,
        "abstain_count": len(abstain_reasons),
        "abstain_reasons": abstain_reasons,
        "abstain_log_paths": abstain_log_paths,
        "red_cardiac_snr_db": red_cardiac.cardiac_snr_db,
        "ir_cardiac_snr_db": ir_cardiac.cardiac_snr_db,
        "red_cardiac_metrics": red_cardiac.to_dict(),
        "ir_cardiac_metrics": ir_cardiac.to_dict(),
        "extended_quality_metrics": extended,
        "diagnostic_placeholder_excluded": diagnostic,
        "captured_utc": _utc_now(),
        "raw_red": red,
        "raw_ir": ir,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"attempt{ordinal:02d}_{condition}_rep{attempt['repetition']}.json"
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"  red cardiac_snr_db={red_cardiac.cardiac_snr_db:.2f} "
          f"ir cardiac_snr_db={ir_cardiac.cardiac_snr_db:.2f}")
    print(f"  saved to {out_path}")
    return record


def print_report(records: list[dict]) -> None:
    print("\n\n========== Z23 FIXTURE-ISOLATION DECISION EXPERIMENT REPORT ==========")
    by_condition: dict[str, list[dict]] = {}
    total_abstain = 0
    abstain_reason_counts: dict[str, int] = {}
    for r in records:
        by_condition.setdefault(r["fixture_condition"], []).append(r)
        total_abstain += r.get("abstain_count", 0)
        for reason in r.get("abstain_reasons", []):
            abstain_reason_counts[reason] = abstain_reason_counts.get(reason, 0) + 1

    for condition in sorted(by_condition):
        print(f"\n-- condition: {condition} --")
        for r in by_condition[condition]:
            if r["outcome"] != "CAPTURED":
                print(f"  attempt {r['attempt_ordinal']} rep{r['repetition']}: {r['outcome']} "
                      f"(reasons={r['abstain_reasons']})")
                continue
            print(f"  attempt {r['attempt_ordinal']} rep{r['repetition']}: "
                  f"red_snr={r['red_cardiac_snr_db']:.2f}dB ir_snr={r['ir_cardiac_snr_db']:.2f}dB "
                  f"red_periodicity={r['extended_quality_metrics']['red']['periodicity_index']:.3f} "
                  f"ir_periodicity={r['extended_quality_metrics']['ir']['periodicity_index']:.3f} "
                  f"worst_cross_corr={r['extended_quality_metrics']['cross_channel']['worst']:.3f}")

    print(f"\nTotal ABSTAIN events (including retries within captured attempts): {total_abstain}")
    print(f"ABSTAIN reasons: {abstain_reason_counts}")


async def main() -> int:
    global OUT_DIR, ABSTAIN_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=PLAN_PATH)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()
    if args.out_dir is not None:
        OUT_DIR = args.out_dir
        ABSTAIN_DIR = OUT_DIR / "abstain"

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    hardware_uid = plan["hardware_uid"]
    for attempt in plan["attempts"]:
        attempt["hardware_uid"] = hardware_uid

    print(f"Scanning for hardware_uid={hardware_uid} ...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, adv: d.name == "VeinNetSensorV2", timeout=15.0
    )
    if device is None:
        print("device not found")
        return 1
    print(f"Found device: {device.address}")

    records: list[dict] = []
    total_attempts = len(plan["attempts"])
    async with BleakClient(device) as client:
        for attempt in plan["attempts"]:
            record = await run_attempt(client, attempt, total_attempts)
            records.append(record)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"\nFull record set saved to {summary_path}")

    print_report(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
