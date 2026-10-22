// device_status.h — battery, contact and FIFO health state for STATUS packets.
//
// Charge-current pin logic corrected per integration spec S8: the historical
// firmware's comment ("LOW = 50mA") was backwards for this board's BQ25100
// wiring. Spec: P0.13 HIGH ~= 50mA, P0.13 LOW ~= 100mA. Default to 50mA
// (HIGH) for the 220mAh cell unless the battery datasheet explicitly
// approves 100mA — do not flip this default without that documented check.
//
// Pin symbols corrected against the actual Seeeduino:nrf52 1.1.9 board
// package variant.h (Seeed_XIAO_nRF52840/variant.h), which does NOT define a
// bare `P0_13` symbol — that was a naming error in an earlier draft of this
// file that would not compile. The board package instead exposes:
//   PIN_CHARGING_CURRENT (Arduino pin 22, wired to nRF52 P0.13)
//   VBAT_ENABLE           (Arduino pin 14) — drive LOW to enable battery ADC read
//   PIN_VBAT              (Arduino pin 32 / analog) — battery voltage divider input
// per https://wiki.seeedstudio.com/XIAO_BLE (battery charging current /
// battery reading sections), referenced from variant.h itself.

#ifndef VEINNET_DEVICE_STATUS_H
#define VEINNET_DEVICE_STATUS_H

#include <stdint.h>

#define CHARGE_CURRENT_PIN      PIN_CHARGING_CURRENT
#define CHG_STAT_PIN            (23u) // Seeeduino:nrf52 1.1.9 variant.cpp: D23 = P0.17 (~CHG).
#define BATTERY_ADC_ENABLE_PIN  VBAT_ENABLE
#define BATTERY_ADC_PIN         PIN_VBAT

// HIGH = ~50mA (production default for 220mAh cell, C/4 rate)
// LOW  = ~100mA (only with explicit battery-datasheet approval)
#define CHARGE_CURRENT_50MA_LEVEL   HIGH
#define CHARGE_CURRENT_100MA_LEVEL  LOW

enum ChargeState : uint8_t {
  CHARGE_STATE_DISCHARGING = 0,
  CHARGE_STATE_CHARGING    = 1,
  CHARGE_STATE_CHARGED     = 2,
  CHARGE_STATE_FAULT       = 3,
};

enum ContactState : uint8_t {
  CONTACT_STATE_NONE     = 0,
  CONTACT_STATE_PRESENT  = 1,
  CONTACT_STATE_UNSTABLE = 2,
};

// Historical provisional 1S Li-Po engineering thresholds (millivolts).
// These do not define the POC display percentage; that mapping is backend-only.
// They remain conservative defaults pending the spec's required electrical
// runtime measurement (H5) — do not treat as independently validated yet.
#define BATTERY_MV_FULL       4200
#define BATTERY_MV_EMPTY      3300
#define BATTERY_MV_LOW        3500   // technical low-battery warning
#define BATTERY_MV_CRITICAL   3400   // refuse to start a new reading below this

// Invalid/unavailable ADC measurement, and backend-owned percentage, sentinels.
// Never interpret these as a real 0mV / 0% reading. Backend publication also
// requires fresh identity-checked STATUS and per-UID physical validation.
#define BATTERY_MV_UNKNOWN_SENTINEL   0xFFFFu
#define BATTERY_PCT_UNKNOWN_SENTINEL  0xFFu

#endif // VEINNET_DEVICE_STATUS_H
