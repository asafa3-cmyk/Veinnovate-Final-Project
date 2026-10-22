// sensor_profiles.h — MAX30102 register profiles for the production path.
//
// Values are the production starting profile mandated by the integration
// spec §2/§3: 400 sps physical, 4x hardware averaging → 100 effective
// paired samples/s, 411us pulse width (18-bit), full RED+IR retained.
//
// These are STARTING points for the bounded automatic-exposure loop in
// firmware — the loop may reduce/raise LED current at runtime (spec §3),
// but must never leave the accepted ADC range and must freeze the setting
// for the duration of an accepted 30-second window.

#ifndef VEINNET_SENSOR_PROFILES_H
#define VEINNET_SENSOR_PROFILES_H

#include <stdint.h>
#include "protocol.h"

// Raw MAX30105 register addresses, for use with the SparkFun MAX3010x
// library's public readRegister8()/writeRegister8(). The library (v1.1.2)
// defines these as file-scope `static const uint8_t` constants inside
// MAX30105.cpp and does NOT export them via MAX30105.h, so they are
// re-declared here from the MAX30102/MAX30105 datasheet register map
// (Table 9) and cross-checked against the library's own .cpp source during
// this bundle's compile verification — not guessed.
#define MAX30105_REG_FIFOOVERFLOW  0x05
#define MAX30105_REG_MODECONFIG    0x09
#define MAX30105_REG_LED1_PULSEAMP 0x0C  // RED
#define MAX30105_REG_LED2_PULSEAMP 0x0D  // IR
// MODECONFIG bit 7 (SHDN, datasheet pg. 19): "the IC will continue to
// respond to I2C commands but will not update with or take new readings"
// while set -- the actual stop-all-conversions guarantee, unlike writing
// MODE=000 alone. Matches the vendored library's private
// MAX30105_SHUTDOWN constant (MAX30105.cpp), re-declared here for the same
// reason as the register addresses above.
#define MAX30105_MODECONFIG_SHDN_BIT 0x80
#define MAX30105_MODECONFIG_MASK   0x07
#define MAX30105_FIFOOVERFLOW_MASK 0x1F

struct SensorProfile {
  uint8_t  config_id;          // SensorConfigId
  uint8_t  led_brightness;     // MAX30102 LED1_PA/LED2_PA starting value
  uint8_t  sample_avg;         // on-chip averaging (=4 for production, see spec §2)
  uint8_t  led_mode;           // 2 = Red + IR
  uint16_t sample_rate_sps;    // physical FIFO rate (=400 for production)
  uint16_t pulse_width_us;     // integration time (=411 for 18-bit)
  uint16_t adc_range_na;       // full-scale ADC range
  uint16_t led_current_min_ma_x10; // bounded auto-exposure floor (x10 mA)
  uint16_t led_current_max_ma_x10; // bounded auto-exposure ceiling (x10 mA)
};

// Production default: dorsal-metacarpal starting point, matching the
// validated golden dorsal LED current, widened for auto-exposure control.
// 0x7F ~= 25.4mA per spec §3 "increase only when SQI is inadequate"; upper
// bound 0xFF (51mA) is the documented maximum test limit, never a default.
static const SensorProfile PROFILE_DORSAL = {
  /* config_id            */ SENSOR_CONFIG_DORSAL,
  /* led_brightness       */ 0x7F,
  /* sample_avg           */ 4,
  /* led_mode             */ 2,
  /* sample_rate_sps      */ 400,
  /* pulse_width_us       */ 411,
  /* adc_range_na         */ 16384,
  /* led_current_min_x10  */ 60,   // 6.0mA floor
  /* led_current_max_x10  */ 510,  // 51.0mA ceiling — 0xFF, requires explicit approval to reach
};

// Fingertip starting point: lower LED current to avoid saturation on a
// high-perfusion site (spec §3 table).
static const SensorProfile PROFILE_FINGERTIP = {
  /* config_id            */ SENSOR_CONFIG_FINGERTIP,
  /* led_brightness       */ 0x1F,
  /* sample_avg           */ 4,
  /* led_mode             */ 2,
  /* sample_rate_sps      */ 400,
  /* pulse_width_us       */ 411,
  /* adc_range_na         */ 4096,
  /* led_current_min_x10  */ 20,   // 2.0mA floor
  /* led_current_max_x10  */ 300,  // 30.0mA ceiling
};

// Presence/contact thresholds (raw IR counts), site-dependent — reused from
// the validated Phase 1.5 breadboard results, not re-derived here.
#define PRESENCE_THRESHOLD_DORSAL     8000UL
#define PRESENCE_THRESHOLD_FINGERTIP  50000UL

#endif // VEINNET_SENSOR_PROFILES_H
