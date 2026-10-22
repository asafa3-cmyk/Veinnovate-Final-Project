#!/usr/bin/env python3
"""Diagnostic fingertip acquisition: ONE uninterrupted BLE stream (no
STOP/START, no config change, no NEW_SESSION after the initial one) -- 10s
pre-roll, then three consecutive 30-second (3000-pair) measurement windows.
Config 17, single fixed fingertip location.

This is diagnostic acquisition only, NOT an approval of production
readiness, and NOT gated by receiver/preflight_stability.py's PASS/ABSTAIN
logic -- the pre-roll here is a fixed 10s experimental settling interval,
not a validated contact/stability guarantee, and this script never aborts
or reselects a window based on what it sees. Every packet received
(pre-roll and all three windows) is saved verbatim, including the known
single-sample startup transient already traced in the LED-on/LED-off
diagnostic -- nothing is dropped, filtered, or re-picked.

Reports, per window, using the UNCHANGED/frozen cardiac_channel_metrics()
from capture_quality_metrics.py (not the extended/diagnostic-corrected
view): red/ir cardiac_snr_db, continuity (first_sample_index contiguity,
duplicate/gap detection), FIFO-overflow and ambient-light-overflow flag
counts, and a plain outlier scan (any sample far from its local neighbors)
reported as raw fact, not used to reject or reselect anything. If a window
does not contain exactly 3000 consecutive real pairs, that is reported as a
failure for that window -- boundaries are never shifted to find a cleaner
one.
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

from capture_quality_metrics import (  # noqa: E402
    CARDIAC_BAND_HIGH_HZ,
    CARDIAC_BAND_LOW_HZ,
    _linear_detrend,
    cardiac_band_correlation,
    cardiac_channel_metrics,
)
from extended_quality_metrics import cross_channel_sub_block_correlation, periodicity_index  # noqa: E402
from notification_archive import NotificationJournal
from protocol_decoder import ControlCommand, Flags, decode_data_packet, encode_control_packet  # noqa: E402
from ble_uuids import CHAR_CONTROL_UUID, CHAR_DATA_UUID  # noqa: E402
from window_assembler import EFFECTIVE_RATE_HZ  # noqa: E402

OUT_DIR = BUNDLE_ROOT / "var" / "stage9-evidence" / "diagnostic-fingertip-continuous"
CONFIG_ID = 17
PRE_ROLL_SECONDS = 10
PRE_ROLL_SAMPLES = PRE_ROLL_SECONDS * EFFECTIVE_RATE_HZ  # 1000, nominal -- see module docstring
WINDOW_SAMPLES = 3000
N_WINDOWS = 3
TOTAL_TARGET_SAMPLES = PRE_ROLL_SAMPLES + N_WINDOWS * WINDOW_SAMPLES  # 10000
SETUP_COUNTDOWN_S = 5
STREAM_TIMEOUT_S = 180.0
OUTLIER_RATIO = 5.0  # purely descriptive: flag a sample >5x or <1/5x its local median; not a gate


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


def _check_continuity(packets: list[dict]) -> dict:
    gaps = []
    duplicates = []
    expected_next = None
    for p in packets:
        idx = p["first_sample_index"]
        if expected_next is not None and idx != expected_next:
            if idx < expected_next:
                duplicates.append({"expected": expected_next, "got": idx})
            else:
                gaps.append({"expected": expected_next, "got": idx, "missing": idx - expected_next})
        expected_next = idx + p["sample_count"]
    return {"gaps": gaps, "duplicates": duplicates, "contiguous": not gaps and not duplicates}


def _find_outliers(values: list[int], *, window: int = 5) -> list[dict]:
    outliers = []
    n = len(values)
    for i in range(n):
        lo = max(0, i - window)
        hi = min(n, i + window + 1)
        neighborhood = [values[j] for j in range(lo, hi) if j != i]
        if not neighborhood:
            continue
        local_median = sorted(neighborhood)[len(neighborhood) // 2]
        if local_median <= 0:
            continue
        ratio = values[i] / local_median
        if ratio >= OUTLIER_RATIO or ratio <= 1.0 / OUTLIER_RATIO:
            outliers.append({"index": i, "value": values[i], "local_median": local_median, "ratio": ratio})
    return outliers


def _non_boundary_acf_peak(samples: list[int], effective_sample_rate_hz: int) -> dict:
    """Same lag range and normalization periodicity_index() already uses
    (frozen, no new filter) -- additionally reports whether the best value
    found is a genuine LOCAL maximum (a real bump, not just the highest
    point because it sits at the edge of the searched lag range) and its
    lag/frequency, since a boundary-pinned max is not evidence of a real
    periodic component (this is exactly the distinction the earlier
    finger-control investigation needed)."""
    if not samples or effective_sample_rate_hz <= 0:
        return {"local_peak_found": False}
    values = _linear_detrend([float(v) for v in samples])
    n = len(values)
    zero_lag_energy = sum(v * v for v in values)
    if zero_lag_energy == 0.0:
        return {"local_peak_found": False}
    min_lag = max(1, round(effective_sample_rate_hz / CARDIAC_BAND_HIGH_HZ))
    max_lag = min(n - 1, round(effective_sample_rate_hz / CARDIAC_BAND_LOW_HZ))
    if min_lag > max_lag:
        return {"local_peak_found": False}
    corr = []
    for lag in range(min_lag, max_lag + 1):
        cross = sum(values[i] * values[i + lag] for i in range(n - lag))
        corr.append(cross / zero_lag_energy)
    local_max_idx = [i for i in range(1, len(corr) - 1) if corr[i] > corr[i - 1] and corr[i] > corr[i + 1]]
    if not local_max_idx:
        return {"local_peak_found": False, "global_max_lag_s": (min_lag) / effective_sample_rate_hz,
                "global_max_value": corr[0] if corr else None}
    best = max(local_max_idx, key=lambda i: corr[i])
    lag = min_lag + best
    return {
        "local_peak_found": True,
        "lag_samples": lag,
        "lag_s": lag / effective_sample_rate_hz,
        "freq_hz": effective_sample_rate_hz / lag,
        "bpm": 60 * effective_sample_rate_hz / lag,
        "value": corr[best],
    }


def _window_report(label: str, packets: list[dict]) -> dict:
    red = [v for p in packets for v in p["red"]]
    ir = [v for p in packets for v in p["ir"]]
    total_samples = len(red)
    continuity = _check_continuity(packets)
    fifo_overflow_flag_count = sum(1 for p in packets if p["fifo_overflow_flag"])
    ambient_light_overflow_flag_count = sum(1 for p in packets if p["ambient_light_overflow_flag"])
    max_fifo_overflow_count = max((p["fifo_overflow_count"] for p in packets), default=0)
    config_readbacks = sorted({p["sensor_config_id_readback"] for p in packets})

    report = {
        "label": label,
        "total_samples": total_samples,
        "expected_samples": WINDOW_SAMPLES if label != "pre_roll" else None,
        "complete_window": (total_samples == WINDOW_SAMPLES) if label != "pre_roll" else None,
        "config_id_readback": config_readbacks,
        "continuity": continuity,
        "fifo_overflow_flag_count": fifo_overflow_flag_count,
        "ambient_light_overflow_flag_count": ambient_light_overflow_flag_count,
        "max_fifo_overflow_count": max_fifo_overflow_count,
    }
    if red:
        report["red_min"] = min(red)
        report["red_max"] = max(red)
        report["red_mean"] = sum(red) / len(red)
        report["ir_min"] = min(ir)
        report["ir_max"] = max(ir)
        report["ir_mean"] = sum(ir) / len(ir)
        report["red_outliers"] = _find_outliers(red)
        report["ir_outliers"] = _find_outliers(ir)
    if label != "pre_roll" and total_samples == WINDOW_SAMPLES:
        red_cardiac = cardiac_channel_metrics(red, EFFECTIVE_RATE_HZ)
        ir_cardiac = cardiac_channel_metrics(ir, EFFECTIVE_RATE_HZ)
        report["red_cardiac_snr_db"] = red_cardiac.cardiac_snr_db
        report["ir_cardiac_snr_db"] = ir_cardiac.cardiac_snr_db
        report["red_cardiac_metrics"] = red_cardiac.to_dict()
        report["ir_cardiac_metrics"] = ir_cardiac.to_dict()
        # Non-boundary ACF peak: same frozen lag range/normalization as
        # periodicity_index() below, additionally reporting whether the
        # best value is a genuine local bump vs. just the search range's
        # edge (a boundary-pinned max is not evidence of real periodicity).
        report["red_periodicity_index"] = periodicity_index(red, EFFECTIVE_RATE_HZ)
        report["ir_periodicity_index"] = periodicity_index(ir, EFFECTIVE_RATE_HZ)
        report["red_non_boundary_acf_peak"] = _non_boundary_acf_peak(red, EFFECTIVE_RATE_HZ)
        report["ir_non_boundary_acf_peak"] = _non_boundary_acf_peak(ir, EFFECTIVE_RATE_HZ)
        # Channel agreement: the same frozen cardiac_band_correlation() used
        # by every prior report in this investigation, plus its full
        # per-sub-block breakdown.
        xcorr = cross_channel_sub_block_correlation(red, ir, EFFECTIVE_RATE_HZ)
        report["worst_cardiac_corr"] = cardiac_band_correlation(red, ir, EFFECTIVE_RATE_HZ)
        report["cross_channel_sub_block_correlations"] = xcorr.sub_block_correlations
    else:
        report["red_cardiac_snr_db"] = None
        report["ir_cardiac_snr_db"] = None
        report["red_periodicity_index"] = None
        report["ir_periodicity_index"] = None
        report["red_non_boundary_acf_peak"] = None
        report["ir_non_boundary_acf_peak"] = None
        report["worst_cardiac_corr"] = None
        report["cross_channel_sub_block_correlations"] = None
    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()

    print("Scanning for VeinNetSensorV2 ...")
    device = await BleakScanner.find_device_by_filter(lambda d, adv: d.name == "VeinNetSensorV2", timeout=15.0)
    if device is None:
        print("device not found")
        return 1
    print(f"Found device: {device.address}")

    print(f"\nPlace and hold fingertip on the sensor now (config {CONFIG_ID}). "
          f"One uninterrupted stream: {PRE_ROLL_SECONDS}s pre-roll then 3x30s windows, no restart.")
    for s in range(SETUP_COUNTDOWN_S, 0, -1):
        print(f"  starting in {s}...")
        await asyncio.sleep(1.0)

    packets: list[dict] = []

    journal = NotificationJournal(OUT_DIR / "notification-journals")

    def on_data(_h, raw):
        journal.append(bytes(raw))
        host_monotonic_ns = time.monotonic_ns()
        host_utc = _utc_now()
        try:
            pkt = decode_data_packet(bytes(raw))
        except Exception as exc:
            print(f"  !! decode error: {exc}")
            return
        packets.append({
            "host_monotonic_ns": host_monotonic_ns,
            "host_utc": host_utc,
            "device_monotonic_timestamp_us": pkt.device_monotonic_timestamp_us,
            "sensor_config_id_readback": pkt.sensor_config_id,
            "first_sample_index": pkt.first_sample_index,
            "sample_count": pkt.sample_count,
            "fifo_overflow_count": pkt.fifo_overflow_count,
            "flags_raw": pkt.flags,
            "fifo_overflow_flag": bool(pkt.flags & Flags.FIFO_OVERFLOW_SINCE_LAST),
            "ambient_light_overflow_flag": bool(pkt.flags & Flags.AMBIENT_LIGHT_OVERFLOW_SINCE_LAST),
            "red": pkt.red,
            "ir": pkt.ir,
        })

    async with BleakClient(device) as client:
        await client.start_notify(CHAR_DATA_UUID, on_data)
        await client.write_gatt_char(
            CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.SET_PROFILE, arg0=CONFIG_ID), response=True
        )
        await asyncio.sleep(0.3)
        await client.write_gatt_char(
            CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.NEW_SESSION), response=True
        )
        print("STREAM_START (single continuous stream, no further STOP/START/config-change until the end)")
        await client.write_gatt_char(CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.START), response=True)

        deadline = time.monotonic() + STREAM_TIMEOUT_S
        total_samples = 0
        while total_samples < TOTAL_TARGET_SAMPLES:
            if time.monotonic() > deadline:
                print(f"  !! stream stalled at {total_samples}/{TOTAL_TARGET_SAMPLES} samples "
                      f"after {STREAM_TIMEOUT_S:g}s -- stopping and reporting what was captured, "
                      f"not extending or retrying.")
                break
            await asyncio.sleep(0.05)
            total_samples = sum(p["sample_count"] for p in packets)

        await client.write_gatt_char(CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.STOP), response=True)
        await asyncio.sleep(0.3)
        await client.stop_notify(CHAR_DATA_UUID)

    total_samples = sum(p["sample_count"] for p in packets)
    print(f"\nStream complete: {total_samples} total samples across {len(packets)} packets.")

    # Slice by sample count in delivery order -- no reselection, no boundary
    # shifting. A short stream simply yields a short/incomplete final slice,
    # reported as such.
    def slice_packets(start: int, end: int) -> list[dict]:
        out = []
        pos = 0
        for p in packets:
            p_start = pos
            p_end = pos + p["sample_count"]
            pos = p_end
            if p_end <= start or p_start >= end:
                continue
            lo = max(0, start - p_start)
            hi = min(p["sample_count"], end - p_start)
            out.append({
                **p,
                "sample_count": hi - lo,
                "first_sample_index": p["first_sample_index"] + lo,
                "red": p["red"][lo:hi],
                "ir": p["ir"][lo:hi],
            })
        return out

    pre_roll_packets = slice_packets(0, PRE_ROLL_SAMPLES)
    window_packets = [
        slice_packets(PRE_ROLL_SAMPLES + i * WINDOW_SAMPLES, PRE_ROLL_SAMPLES + (i + 1) * WINDOW_SAMPLES)
        for i in range(N_WINDOWS)
    ]

    reports = {
        "pre_roll": _window_report("pre_roll", pre_roll_packets),
        "windows": [_window_report(f"window_{i}", window_packets[i]) for i in range(N_WINDOWS)],
    }

    whole_stream_continuity = _check_continuity(packets)

    record = {
        "config_id": CONFIG_ID,
        "pre_roll_seconds": PRE_ROLL_SECONDS,
        "pre_roll_note": "10s experimental settling interval, not a validated stability/contact guarantee",
        "window_samples": WINDOW_SAMPLES,
        "n_windows": N_WINDOWS,
        "total_target_samples": TOTAL_TARGET_SAMPLES,
        "total_samples_received": total_samples,
        "whole_stream_continuity": whole_stream_continuity,
        "reports": reports,
        "raw_packets": packets,
        "captured_utc": _utc_now(),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"diagnostic_fingertip_continuous_config{CONFIG_ID}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"\nFull record (including all raw samples) saved to {out_path}")

    print("\n\n========== REPORT ==========")
    print(f"whole-stream continuity: contiguous={whole_stream_continuity['contiguous']} "
          f"gaps={whole_stream_continuity['gaps']} duplicates={whole_stream_continuity['duplicates']}")
    pr = reports["pre_roll"]
    print(f"\npre-roll: samples={pr['total_samples']} (target {PRE_ROLL_SAMPLES}) "
          f"config_readback={pr['config_id_readback']} "
          f"fifo_overflow_flags={pr['fifo_overflow_flag_count']} ambient_light_overflow_flags={pr['ambient_light_overflow_flag_count']}")
    if "red_outliers" in pr and pr["red_outliers"]:
        print(f"  red outliers: {pr['red_outliers']}")
    if "ir_outliers" in pr and pr["ir_outliers"]:
        print(f"  ir outliers: {pr['ir_outliers']}")

    for i, w in enumerate(reports["windows"]):
        print(f"\nwindow {i}: samples={w['total_samples']}/{WINDOW_SAMPLES} complete={w['complete_window']} "
              f"config_readback={w['config_id_readback']} "
              f"fifo_overflow_flags={w['fifo_overflow_flag_count']} ambient_light_overflow_flags={w['ambient_light_overflow_flag_count']} "
              f"continuity_contiguous={w['continuity']['contiguous']}")
        if w["red_cardiac_snr_db"] is not None:
            print(f"  red_cardiac_snr_db={w['red_cardiac_snr_db']:.2f} ir_cardiac_snr_db={w['ir_cardiac_snr_db']:.2f}")
            print(f"  red_periodicity_index={w['red_periodicity_index']:.4f} "
                  f"ir_periodicity_index={w['ir_periodicity_index']:.4f}")
            print(f"  red_non_boundary_acf_peak={w['red_non_boundary_acf_peak']}")
            print(f"  ir_non_boundary_acf_peak={w['ir_non_boundary_acf_peak']}")
            print(f"  worst_cardiac_corr (channel agreement)={w['worst_cardiac_corr']:.4f}")
            print(f"  cross_channel_sub_block_correlations={w['cross_channel_sub_block_correlations']}")
        else:
            print("  cardiac_snr_db / periodicity / ACF peak / channel agreement: NOT COMPUTED (window incomplete)")
        if w.get("red_outliers"):
            print(f"  red outliers: {w['red_outliers']}")
        if w.get("ir_outliers"):
            print(f"  ir outliers: {w['ir_outliers']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
