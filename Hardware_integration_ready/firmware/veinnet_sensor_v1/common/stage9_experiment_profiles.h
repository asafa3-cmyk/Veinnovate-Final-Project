#ifndef VEINNET_STAGE9_PROFILES_H
#define VEINNET_STAGE9_PROFILES_H

// Experimental-only OAAT configurations. IDs are outside the production
// SensorConfigId enum but remain valid uint8 values in Protocol V2 packets.
// Config 19 is the shared 25.4 mA / 16384 nA baseline, so the two sweeps
// contain eight unique combinations rather than duplicating it.
static const SensorProfile STAGE9_PROFILES[] = {
  {16, 0x1F, 4, 2, 400, 411, 16384, 62, 62},
  {17, 0x3E, 4, 2, 400, 411, 16384, 124, 124},
  {18, 0x5E, 4, 2, 400, 411, 16384, 188, 188},
  {19, 0x7F, 4, 2, 400, 411, 16384, 254, 254},
  {20, 0x9F, 4, 2, 400, 411, 16384, 318, 318},
  {21, 0xBE, 4, 2, 400, 411, 16384, 380, 380},
  {22, 0x7F, 4, 2, 400, 411, 4096, 254, 254},
  {23, 0x7F, 4, 2, 400, 411, 8192, 254, 254},
  // Owner-authorized Z23 POC comparison profiles (2026-09-15). These use
  // distinct identities so the historical meanings of configs 16/17/18
  // remain immutable. led_brightness is the RED value; the IR override is
  // declared in STAGE9_LED_PAIRS below and verified independently on-device.
  {24, 0x1F, 4, 2, 400, 411, 16384, 62, 62},
  {25, 0x1F, 4, 2, 400, 411, 16384, 62, 124},
  {26, 0x1F, 4, 2, 400, 411, 16384, 62, 188},
  // Diagnostic-only twin of config 17 (STAGE9_METRIC_CORRECTION_HANDOFF.md
  // no-contact-baseline investigation): identical timing/averaging/ADC
  // range, LED pulse amplitude forced to 0 (both channels) so the ADC
  // samples with the LEDs fully off, to separate LED-dependent coupling
  // from any other source of the config-17 no-contact baseline. 117 = 17 +
  // 100, chosen only to keep it visibly paired with 17 and clearly outside
  // both the real config range (16-23) and the production SensorConfigId
  // enum -- never intended as an acquisition profile.
  {117, 0x00, 4, 2, 400, 411, 16384, 0, 0},
};
static const uint8_t STAGE9_PROFILE_COUNT = sizeof(STAGE9_PROFILES) / sizeof(STAGE9_PROFILES[0]);

struct Stage9LedPair {
  uint8_t config_id;
  uint8_t red_pa;
  uint8_t ir_pa;
};

static const Stage9LedPair STAGE9_LED_PAIRS[] = {
  {24, 0x1F, 0x1F},
  {25, 0x1F, 0x3E},
  {26, 0x1F, 0x5E},
};
static const uint8_t STAGE9_LED_PAIR_COUNT = sizeof(STAGE9_LED_PAIRS) / sizeof(STAGE9_LED_PAIRS[0]);

static uint8_t stage9RedPa(const SensorProfile &profile) {
  for (uint8_t i = 0; i < STAGE9_LED_PAIR_COUNT; i++) {
    if (STAGE9_LED_PAIRS[i].config_id == profile.config_id) return STAGE9_LED_PAIRS[i].red_pa;
  }
  return profile.led_brightness;
}

static uint8_t stage9IrPa(const SensorProfile &profile) {
  for (uint8_t i = 0; i < STAGE9_LED_PAIR_COUNT; i++) {
    if (STAGE9_LED_PAIRS[i].config_id == profile.config_id) return STAGE9_LED_PAIRS[i].ir_pa;
  }
  return profile.led_brightness;
}
// Per-capture sample limit: reaching this count triggers stage9StartCapture()'s
// completion path (g_stage9SamplesSent == STAGE9_CAPTURE_SAMPLES), which
// calls stage9SafeOff() -- since the SHDN correction, that genuinely shuts
// the sensor down. A continuous multi-window diagnostic stream (pre-roll +
// several 30s windows on one uninterrupted stream, no restart) needs more
// than 3000 samples before that shutdown fires, or the stream silently
// stalls exactly at 3000 with no error (observed on-device: a 10s pre-roll
// + 3x30s continuous capture stalled at 3000/10000 samples, traced to this
// constant, not a BLE/hardware fault). Overridable at compile time via
// -DSTAGE9_CAPTURE_SAMPLES=<value> for a diagnostic-only build; the
// PRODUCTION DEFAULT REMAINS 3000 -- do not change the value below, add a
// build flag instead (see scripts/build_stage9_firmware.py's diagnostic
// variant).
#ifndef STAGE9_CAPTURE_SAMPLES
#define STAGE9_CAPTURE_SAMPLES 3000u
#endif

static const SensorProfile *findStage9Profile(uint8_t configId) {
  for (uint8_t i = 0; i < STAGE9_PROFILE_COUNT; i++) {
    if (STAGE9_PROFILES[i].config_id == configId) return &STAGE9_PROFILES[i];
  }
  return NULL;
}

#endif
