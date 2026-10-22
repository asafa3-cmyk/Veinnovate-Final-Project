#ifndef VEINNET_BATTERY_ADC_H
#define VEINNET_BATTERY_ADC_H
#include <stdint.h>

// Ordinary XIAO nominal 1 MOhm / 510 kOhm divider, 12-bit / 3.6 V SAADC.
// These are plausible engineering bounds, not cell protection thresholds.
static uint16_t batteryMillivoltsFromRaw(uint32_t raw) {
  if (raw == 0 || raw >= 4095u) return 0xFFFFu;
  const uint32_t mv = ((uint64_t)raw * 3600u * 1510u + 4096u * 510u / 2u)
                      / (4096u * 510u);
  return mv >= 2500u && mv <= 4500u ? (uint16_t)mv : 0xFFFFu;
}
#endif
