#!/usr/bin/env python3
"""Controlled no-contact diagnostic for the config-17 no-contact baseline
that would not separate from firm contact even light-sealed (last-block
ratio ~1.59x, but reproducibility poor: no_contact itself drifted from
~65-69k down to ~42k across blocks 8-9 of the light-sealed pass with
NOTHING touching the sensor).

Design (per the second-review correction -- do not infer saturation from
averages, check raw samples and hardware status flags directly):
  - Identical sensor position and enclosure for a whole run (operator holds
    physical setup fixed; --enclosure-label just records what was used).
  - Two LED states per repeat, same order every time:
      1. config 117 -- exact config-17 twin with LED1/LED2 pulse amplitude
         forced to 0 (both channels off), ADC sampling still active.
      2. config 17 -- real LED currents.
  - Repeated --repeats times (default 2) for reproducibility.
  - Every packet's RED/IR, device_monotonic_timestamp_us, host-side
    timestamps, sensor_config_id READBACK (not just what was requested),
    fifo_overflow_count, and the new diagnostic
    AMBIENT_LIGHT_OVERFLOW_SINCE_LAST flag bit (see protocol_decoder.Flags,
    veinnet_sensor_v1.ino's pollAmbientLightOverflow()) are saved -- not
    just DC averages, so a reviewer can check raw values and status flags
    directly rather than trust a summary number.

To check whether a light-sealing cover reflects LED light back into the
detector, run this script TWICE with different --enclosure-label values:
once with the light-sealing cover in place, once with a supported opaque
enclosure that does not have any material directly covering the optical
window. Compare the two runs' output files.

Interpretation guardrails (do not violate in analysis of this script's
output): a reproducible LED-dependent baseline (present with LEDs on config
17, gone with LEDs off config 117) supports LED-related coupling but does
NOT by itself distinguish optical reflection from electrical supply
coupling. Do not conclude either. If unexplained jumps persist across
repeats, that points at acquisition integrity / supply voltage, to inspect
BEFORE selecting any capacitor -- never pick a filter or capacitor from a
guess; this diagnostic exists to determine whether either is even relevant.
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

from ble_uuids import CHAR_CONTROL_UUID, CHAR_DATA_UUID  # noqa: E402
from notification_archive import NotificationJournal
from protocol_decoder import ControlCommand, Flags, decode_data_packet, encode_control_packet  # noqa: E402

OUT_DIR = BUNDLE_ROOT / "var" / "stage9-evidence" / "ambient-light-diagnostic"
SETUP_COUNTDOWN_S = 3
LED_OFF_CONFIG_ID = 117
LED_ON_CONFIG_ID = 17


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


async def run_one(client, config_id: int, label: str, duration_s: float) -> dict:
    print(f"\n=== {label} (config {config_id}) ===")
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

    await client.start_notify(CHAR_DATA_UUID, on_data)
    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.SET_PROFILE, arg0=config_id), response=True
    )
    await asyncio.sleep(0.3)
    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.NEW_SESSION), response=True
    )
    await client.write_gatt_char(CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.START), response=True)
    await asyncio.sleep(duration_s)
    await client.write_gatt_char(CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.STOP), response=True)
    await asyncio.sleep(0.3)
    await client.stop_notify(CHAR_DATA_UUID)

    total_samples = sum(p["sample_count"] for p in packets)
    config_readbacks = {p["sensor_config_id_readback"] for p in packets}
    max_fifo_overflow = max((p["fifo_overflow_count"] for p in packets), default=0)
    alc_ovf_packet_count = sum(1 for p in packets if p["ambient_light_overflow_flag"])
    all_red = [v for p in packets for v in p["red"]]
    all_ir = [v for p in packets for v in p["ir"]]

    print(f"  samples={total_samples} packets={len(packets)} "
          f"config_readback={sorted(config_readbacks)} "
          f"max_fifo_overflow_count={max_fifo_overflow} "
          f"packets_with_ambient_light_overflow_flag={alc_ovf_packet_count}/{len(packets)}")
    if all_red:
        print(f"  red: min={min(all_red)} max={max(all_red)} first5={all_red[:5]} last5={all_red[-5:]}")
        print(f"  ir : min={min(all_ir)} max={max(all_ir)} first5={all_ir[:5]} last5={all_ir[-5:]}")

    return {
        "label": label,
        "config_id_requested": config_id,
        "config_id_readback": sorted(config_readbacks),
        "duration_s": duration_s,
        "total_samples": total_samples,
        "max_fifo_overflow_count": max_fifo_overflow,
        "packets_with_ambient_light_overflow_flag": alc_ovf_packet_count,
        "total_packets": len(packets),
        "packets": packets,
        "captured_utc": _utc_now(),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enclosure-label", required=True,
                         help="e.g. 'light_sealed_cover' or 'opaque_enclosure_no_window_cover'")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--duration-s", type=float, default=5.0)
    args = parser.parse_args()

    print("Scanning for VeinNetSensorV2 ...")
    device = await BleakScanner.find_device_by_filter(lambda d, adv: d.name == "VeinNetSensorV2", timeout=15.0)
    if device is None:
        print("device not found")
        return 1
    print(f"Found device: {device.address}")

    print(f"\nEnclosure: {args.enclosure_label} -- keep sensor position and enclosure IDENTICAL "
          f"for the whole run. No finger/contact at any point in this diagnostic.")

    runs: list[dict] = []
    async with BleakClient(device) as client:
        for rep in range(1, args.repeats + 1):
            print(f"\n########## REPEAT {rep}/{args.repeats} ##########")
            led_off = await run_one(
                client, LED_OFF_CONFIG_ID, f"LED-OFF (repeat {rep})", args.duration_s
            )
            led_off["repeat"] = rep
            led_off["enclosure_label"] = args.enclosure_label
            runs.append(led_off)

            await asyncio.sleep(1.0)

            led_on = await run_one(
                client, LED_ON_CONFIG_ID, f"CONFIG-17 LED-ON (repeat {rep})", args.duration_s
            )
            led_on["repeat"] = rep
            led_on["enclosure_label"] = args.enclosure_label
            runs.append(led_on)

            if led_off["total_samples"] and led_on["total_samples"]:
                off_red_mean = sum(v for p in led_off["packets"] for v in p["red"]) / led_off["total_samples"]
                on_red_mean = sum(v for p in led_on["packets"] for v in p["red"]) / led_on["total_samples"]
                print(f"\n  repeat {rep} summary: LED-off red mean={off_red_mean:.1f}  "
                      f"config-17 red mean={on_red_mean:.1f}  "
                      f"delta={on_red_mean - off_red_mean:.1f}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"ambient_light_diagnostic_{args.enclosure_label}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out_path.write_text(json.dumps(runs, indent=2), encoding="utf-8")
    print(f"\nFull record set saved to {out_path}")

    print("\n\n========== DIAGNOSTIC REPORT (raw only -- no interpretation) ==========")
    for r in runs:
        red_vals = [v for p in r["packets"] for v in p["red"]]
        ir_vals = [v for p in r["packets"] for v in p["ir"]]
        print(f"\nrepeat={r['repeat']} enclosure={r['enclosure_label']} label={r['label']} "
              f"config_requested={r['config_id_requested']} config_readback={r['config_id_readback']}")
        print(f"  samples={r['total_samples']} max_fifo_overflow_count={r['max_fifo_overflow_count']} "
              f"packets_with_ambient_light_overflow_flag={r['packets_with_ambient_light_overflow_flag']}/{r['total_packets']}")
        if red_vals:
            print(f"  red mean={sum(red_vals)/len(red_vals):.1f} min={min(red_vals)} max={max(red_vals)}")
            print(f"  ir  mean={sum(ir_vals)/len(ir_vals):.1f} min={min(ir_vals)} max={max(ir_vals)}")

    print("\nReminder: this script only reports raw numbers and status flags. Do not "
          "conclude optical-vs-electrical coupling, saturation, or a filter/capacitor "
          "choice from this output alone -- see this file's module docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
