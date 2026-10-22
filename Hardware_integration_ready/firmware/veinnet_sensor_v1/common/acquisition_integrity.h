#ifndef VEINNET_ACQUISITION_INTEGRITY_H
#define VEINNET_ACQUISITION_INTEGRITY_H
#include <stdint.h>

// Shared by the sensor driver and native deterministic tests. Counters are
// cumulative lower bounds for one explicit acquisition epoch, never wrapping.
enum AcquisitionUncertainty : uint8_t {
  ACQUISITION_UNVERIFIED = 1,
  ACQUISITION_HW_COUNTER_SATURATED = 2,
  ACQUISITION_I2C_UNCERTAIN = 4,
  ACQUISITION_COUNTER_REGRESSION = 8,
  ACQUISITION_COUNTER_SATURATED = 16,
  ACQUISITION_CLOCK_UNCERTAIN = 32,
};

// Extend the pinned core's uint32 micros() across rollover. millis() supplies
// an independent interval check; DWT's 67 s wrap and long unobserved intervals
// must not masquerade as a valid uint32 micros rollover.
struct ConstructionClock {
  bool initialized = false;
  uint32_t previousUs = 0, previousMs = 0;
  uint64_t extendedUs = 0;
  bool uncertain = false;
  uint64_t observe(uint32_t us, uint32_t ms) {
    if (!initialized) { initialized = true; extendedUs = us; }
    else {
      const uint32_t elapsedUs = us - previousUs;
      const uint32_t elapsedMs = ms - previousMs;
      const uint64_t expectedUs = (uint64_t)elapsedMs * 1000;
      const uint64_t error = elapsedUs > expectedUs ? elapsedUs-expectedUs : expectedUs-elapsedUs;
      if (expectedUs >= UINT32_MAX || error > 3000) uncertain = true;
      extendedUs += elapsedUs;
    }
    previousUs = us; previousMs = ms;
    return extendedUs;
  }
};
struct AcquisitionLossRecord {
  uint32_t hardware = 0;
  uint32_t software = 0;
  uint8_t uncertainty = 0;
  uint8_t observed = 0;
  void add(uint32_t &counter, uint32_t amount) {
    if (UINT32_MAX - counter < amount) {
      counter = UINT32_MAX;
      uncertainty |= ACQUISITION_COUNTER_SATURATED;
    } else counter += amount;
  }
  void observeHardware(uint8_t raw) {
    raw &= 31;
    if (raw == 31) uncertainty |= ACQUISITION_HW_COUNTER_SATURATED;
    if (raw < observed) uncertainty |= ACQUISITION_COUNTER_REGRESSION;
    else add(hardware, raw - observed);
    observed = raw;
  }
  // Only a FIFO pop or an explicit hardware FIFO clear resets its register.
  // Neither operation clears the latched evidence.
  void hardwarePopped() { observed = 0; }
  void softwareDrop() { add(software, 1); }
  bool failed() const { return hardware || software || uncertainty; }
  void resetEpoch() { hardware = software = 0; uncertainty = observed = 0; }
};

struct OpticalSample { uint32_t red, ir, green; };
// A payload stays pending until notify accepts it; emitted indices count only
// accepted payloads. While pending, hardware FIFO loss is polled but not drained.
struct PendingTransmission {
  bool pending = false;
  uint16_t length = 0;
  uint8_t samples = 0;
  bool mayDrain() const { return !pending; }
  void stage(uint16_t bytes, uint8_t count) { pending = true; length = bytes; samples = count; }
  void clear() { pending = false; length = 0; samples = 0; }
  uint8_t acknowledge(bool accepted, uint32_t &index) {
    if (!pending || !accepted) return 0;
    uint8_t count = samples;
    index += count;
    clear();
    return count;
  }
};
template <uint8_t Capacity> class AcquisitionQueue {
  static_assert(Capacity > 0, "queue must have capacity");
  OpticalSample slots[Capacity] = {};
  OpticalSample latest = {};
  uint8_t head = 0, tail = 0, count = 0;
public:
  uint8_t available() const { return count; }
  uint8_t freeCapacity() const { return Capacity - count; }
  bool push(const OpticalSample &sample) {
    if (count == Capacity) return false; // never overwrite unread data
    slots[head] = sample;
    latest = sample;
    head = (head + 1) % Capacity;
    ++count;
    return true;
  }
  const OpticalSample &front() const { return slots[tail]; }
  const OpticalSample &newest() const { return latest; }
  void pop() { if (count) { tail = (tail + 1) % Capacity; --count; } }
  void clear() { head = tail = count = 0; latest = OpticalSample{}; }
};
#endif
