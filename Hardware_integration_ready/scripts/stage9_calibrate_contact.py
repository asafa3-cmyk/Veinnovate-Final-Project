#!/usr/bin/env python3
"""Contact-magnitude calibration pass for one preflight config_id.

Runs a deliberate no-contact capture, then a deliberate firm-contact
capture, back-to-back on the same device -- the same protocol already used
for config 16's CONTACT_CALIBRATION entry in receiver/preflight_stability.py
(2026-09-10). Never derive a new config's calibration by scaling or
extrapolating another config's numbers (LED brightness differs drastically
across profiles) -- this script exists precisely so every config gets its
own real on-device pass before receiver/preflight_stability.py's
CONTACT_CALIBRATION table gains an entry for it.

This script only COLLECTS the raw calibration data and prints/saves the
per-block DC values needed to choose red_no_contact_dc / ir_no_contact_dc /
min_contact_dc_multiplier. It does not edit preflight_stability.py itself --
that edit is made by hand afterward, from the real numbers this produces,
same as config 16's entry was.
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
from protocol_decoder import ControlCommand, decode_data_packet, encode_control_packet  # noqa: E402

OUT_DIR = BUNDLE_ROOT / "var" / "stage9-evidence" / "contact-calibration"
BLOCK_SECONDS = 0.5
EFFECTIVE_RATE_HZ = 100
SETUP_COUNTDOWN_S = 5


async def run_one(client, config_id: int, label: str, filename: str, capture_seconds: float) -> dict:
    print(f"\n=== {label} (config {config_id}) ===")
    for s in range(SETUP_COUNTDOWN_S, 0, -1):
        print(f"  starting in {s}...")
        await asyncio.sleep(1.0)

    red: list[int] = []
    ir: list[int] = []

    journal = NotificationJournal(OUT_DIR / "notification-journals")

    def on_data(_h, raw):
        journal.append(bytes(raw))
        try:
            pkt = decode_data_packet(bytes(raw))
        except Exception as exc:
            print(f"  !! decode error: {exc}")
            return
        red.extend(pkt.red)
        ir.extend(pkt.ir)

    await client.start_notify(CHAR_DATA_UUID, on_data)
    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.SET_PROFILE, arg0=config_id), response=True
    )
    await asyncio.sleep(0.3)
    await client.write_gatt_char(
        CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.NEW_SESSION), response=True
    )
    await client.write_gatt_char(CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.START), response=True)
    await asyncio.sleep(capture_seconds)
    await client.write_gatt_char(CHAR_CONTROL_UUID, encode_control_packet(command=ControlCommand.STOP), response=True)
    await asyncio.sleep(0.3)
    await client.stop_notify(CHAR_DATA_UUID)

    print(f"  samples: {len(red)}")
    block_samples = round(BLOCK_SECONDS * EFFECTIVE_RATE_HZ)
    blocks = []
    for start in range(0, len(red) - block_samples + 1, block_samples):
        end = start + block_samples
        red_dc = sum(red[start:end]) / block_samples
        ir_dc = sum(ir[start:end]) / block_samples
        blocks.append({"start": start, "end": end, "red_dc": red_dc, "ir_dc": ir_dc})
        print(f"    block {len(blocks) - 1}: red_dc={red_dc:.1f} ir_dc={ir_dc:.1f}")
    overall_red = sum(red) / len(red) if red else 0.0
    overall_ir = sum(ir) / len(ir) if ir else 0.0
    print(f"  overall red mean: {overall_red:.1f}  ir mean: {overall_ir:.1f}")
    print(f"  red min/max: {min(red) if red else 'n/a'}/{max(red) if red else 'n/a'}   "
          f"ir min/max: {min(ir) if ir else 'n/a'}/{max(ir) if ir else 'n/a'}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / filename
    record = {
        "label": label,
        "config_id": config_id,
        "red": red,
        "ir": ir,
        "blocks": blocks,
        "overall_red_dc": overall_red,
        "overall_ir_dc": overall_ir,
        "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"  saved raw samples to {out_path}")
    return record


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-id", type=int, required=True)
    parser.add_argument("--hardware-uid", default="A90AFDAF672D66F1")
    parser.add_argument("--capture-seconds", type=float, default=2.0,
                         help="longer than the 2.0s default gives firm-contact time to reach a real plateau")
    parser.add_argument("--light-sealed", action="store_true",
                         help="prints a reminder that the light-sealing cover must be in place for both captures")
    args = parser.parse_args()

    print("Scanning for VeinNetSensorV2 ...")
    device = await BleakScanner.find_device_by_filter(lambda d, adv: d.name == "VeinNetSensorV2", timeout=15.0)
    if device is None:
        print("device not found")
        return 1
    print(f"Found device: {device.address}")

    seal_note = " -- LIGHT-SEALING COVER MUST BE IN PLACE (around the sensor, not blocking the optical window)" if args.light_sealed else ""
    suffix = "_lightsealed" if args.light_sealed else ""

    async with BleakClient(device) as client:
        no_contact = await run_one(
            client, args.config_id,
            f"NO-CONTACT: keep finger completely away from the sensor the whole time{seal_note}",
            f"config{args.config_id}_no_contact{suffix}.json",
            args.capture_seconds,
        )
        await asyncio.sleep(2.0)
        firm_contact = await run_one(
            client, args.config_id,
            f"FIRM-CONTACT: press finger down firmly and hold steady the whole time{seal_note}",
            f"config{args.config_id}_firm_contact{suffix}.json",
            args.capture_seconds,
        )

    print("\n\n========== CALIBRATION SUMMARY ==========")
    print(f"config_id={args.config_id}  capture_seconds={args.capture_seconds}")
    print(f"no_contact:   overall red_dc={no_contact['overall_red_dc']:.1f} ir_dc={no_contact['overall_ir_dc']:.1f}")
    print(f"firm_contact: overall red_dc={firm_contact['overall_red_dc']:.1f} ir_dc={firm_contact['overall_ir_dc']:.1f}")
    if firm_contact["blocks"]:
        last = firm_contact["blocks"][-1]
        print(f"firm_contact LAST BLOCK (plateau check): red_dc={last['red_dc']:.1f} ir_dc={last['ir_dc']:.1f}")
        if no_contact["overall_red_dc"] > 0 and no_contact["overall_ir_dc"] > 0:
            print(f"  last-block/no-contact ratio: red={last['red_dc']/no_contact['overall_red_dc']:.2f}x "
                  f"ir={last['ir_dc']/no_contact['overall_ir_dc']:.2f}x")
    if no_contact["overall_red_dc"] > 0 and no_contact["overall_ir_dc"] > 0:
        red_ratio = firm_contact["overall_red_dc"] / no_contact["overall_red_dc"]
        ir_ratio = firm_contact["overall_ir_dc"] / no_contact["overall_ir_dc"]
        print(f"firm/no-contact ratio: red={red_ratio:.1f}x ir={ir_ratio:.1f}x")
    print("\nUse these real numbers to add a CONTACT_CALIBRATION[config_id] entry "
          "to receiver/preflight_stability.py by hand -- this script does not edit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
