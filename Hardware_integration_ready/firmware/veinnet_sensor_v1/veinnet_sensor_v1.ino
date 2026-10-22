// veinnet_sensor_v1.ino — production MAX30102/XIAO nRF52840 acquisition +
// BLE Protocol V2 peripheral for the VeinNet clinical prototype.
//
// Board:  Seeed XIAO BLE - nRF52840
// Compile-verified against (see bundle README "Firmware compile verification"):
//   Seeeduino:nrf52 core   1.1.9
//   Bluefruit52Lib         (bundled with the core above — see note below)
//   SparkFun MAX3010x      1.1.2
// arduino-cli compile --fqbn Seeeduino:nrf52:xiaonRF52840 firmware/veinnet_sensor_v1
//
// IMPORTANT — BLE library correction found during this bundle's compile
// verification pass: an earlier draft of this firmware used ArduinoBLE,
// which does not compile against the Seeeduino:nrf52 core at all (that core
// is derived from Adafruit's nRF52 Arduino core, which uses the SoftDevice
// directly via the bundled Bluefruit52Lib — not mbed/Cordio or an external
// HCI-UART module, which is all ArduinoBLE 1.x/2.x support). This firmware
// uses Bluefruit52Lib, the BLE stack actually shipped with and compiled
// against this board package. arduino-libs.lock.txt has been updated to
// drop the ArduinoBLE pin accordingly.
//
// Replaces the superseded 6-byte/50Hz Phase-2 sketch. Differences:
//   - FIFO-driven acquisition (no delay()-based sampling clock)
//   - full 18-bit RED+IR, never cast to uint16_t
//   - versioned, sequenced, session-scoped BLE protocol (protocol.h)
//   - separate DATA/STATUS/CONTROL characteristics
//   - corrected charge-current pin logic (device_status.h)
//
// This firmware performs NO model preprocessing (no filtering, no AC
// extraction, no resampling) — raw RED/IR only, per spec S3.
//
// Resilient-mode retransmission (a XIAO-side ring buffer replaying a window
// after reconnect) is explicitly NOT implemented in this firmware. An
// earlier draft of this file allocated a 36KB ring buffer while claiming
// this behavior; that buffer has been removed rather than left unused while
// making an unimplemented claim (see bundle README "Known limitations" —
// item 7). A disconnect during acquisition means samples for that period
// are genuinely lost from the host's perspective; window_assembler.py /
// sqi_gate.py will correctly reject any window with loss above the 1%
// threshold rather than silently accepting a gap.

#include <Wire.h>
#include "MAX30105.h"
#include <bluefruit.h>
#include "common/protocol.h"
#include "common/sensor_profiles.h"
#include "common/device_status.h"
#include "common/battery_adc.h"
#ifdef VEINNET_STAGE9_EXPERIMENT
#include "common/stage9_experiment_profiles.h"
#include "common/bench_final_witness.h"
#include "common/bench_warmup.h"
#endif

// ── BLE service/characteristics (Bluefruit52Lib) ─────────────────────────
BLEService ppgService(VEINNET_SERVICE_UUID);
BLECharacteristic dataChar(VEINNET_CHAR_DATA_UUID, BLENotify,
                            DATA_HEADER_SIZE + VEINNET_MAX_SAMPLES_PER_PACKET * SAMPLE_PAIR_SIZE + CRC_SIZE,
                            /* fixed_len= */ false);
BLECharacteristic statusChar(VEINNET_CHAR_STATUS_UUID, BLENotify, STATUS_PACKET_SIZE, /* fixed_len= */ true);
BLECharacteristic controlChar(VEINNET_CHAR_CONTROL_UUID, BLEWrite, CONTROL_PACKET_SIZE, /* fixed_len= */ true);
BLECharacteristic identityChar(VEINNET_CHAR_IDENTITY_UUID, BLERead, IDENTITY_VALUE_SIZE, /* fixed_len= */ true);

#ifdef VEINNET_STAGE9_EXPERIMENT
BLECharacteristic finalChar(VEINNET_CHAR_FINAL_UUID, BLERead | BLENotify,
                           WARMUP_FINAL_WITNESS_SIZE, /* fixed_len= */ false);
static BenchFinalWitness g_benchFinal;
static BenchWarmup g_benchWarmup;
static volatile uint64_t g_benchNonce = 0;
#endif
MAX30105 particleSensor;

// ── Device / session identity ───────────────────────────────────────────
// Stage 5 creates an approved per-unit restore build with this value set to
// the inventory code suffix. Zero is deliberately unprovisioned and cannot
// pass the Jetson identity gate.
#ifndef VEINNET_SHORT_CODE_NUMBER
#define VEINNET_SHORT_CODE_NUMBER 0
#endif
static uint64_t g_hardwareUid = 0;
static uint32_t g_sensorSessionId = 0;
static uint32_t g_nextSampleIndex = 0;
static uint32_t g_fifoOverflowCount = 0;
static ConstructionClock g_constructionClock;
static uint8_t g_sensorConfigId = SENSOR_CONFIG_UNAVAILABLE;
static bool     g_sensorAvailable = false;
// Boot-time diagnostic register snapshot (reset/shutdown/LED-off sequence
// in initializeSensorIfPresent()), printed once Serial is ready in setup().
static uint8_t  g_bootReg09BeforeReset = 0xFF;
static uint8_t  g_bootReg09AfterReset = 0xFF;
static uint8_t  g_bootReg09AfterShdn = 0xFF;
static uint8_t  g_bootReg0CLed1Pa = 0xFF;
static uint8_t  g_bootReg0DLed2Pa = 0xFF;
static volatile bool g_startRequested = false;
static volatile bool g_streaming = false;
static PendingTransmission g_transmission;

#ifdef VEINNET_STAGE9_EXPERIMENT
// Set only by the CCCD write event itself (onDataCccdWrite), never polled.
// BLECharacteristic::notifyEnabled() reads the same underlying CCCD state but
// on some centrals/stacks the write event and the state visible to a
// concurrently-polling loop() iteration can be observed out of order; gating
// notify() attempts on this event-driven flag instead removes that race.
static volatile bool g_dataNotifySubscribed = false;
#endif
#ifdef VEINNET_STAGE9_EXPERIMENT
static uint32_t g_stage9SamplesSent = 0;
static const SensorProfile *g_stage9SelectedProfile = NULL;
// V3 queue stores the first real sample directly; no placeholder exists.
// Diagnostic-only (config-17 no-contact-baseline investigation): latched
// true by pollAmbientLightOverflow() whenever MAX30105 INTR_STATUS_1's
// ALC_OVF bit (ambient light cancellation overflow) is observed, cleared
// each time a DATA packet is built -- "since last packet" semantics,
// mirroring how FIFO_OVERFLOW_SINCE_LAST is documented (numeric total is
// g_stage9AlcOvfEventCount below, for Serial cross-checking on the bench).
static bool g_stage9AlcOvfSeenSincePacket = false;
static uint16_t g_stage9AlcOvfEventCount = 0;
// onControlWrite() runs on Bluefruit's Ada Callback Task, a separate FreeRTOS
// task from loop() -- NOT loop() itself and not an ISR. stage9PrepareProfile()/
// stage9StartCapture()/stage9SafeOff() perform blocking, unsynchronized I2C
// transactions on the same TWI bus that loop() concurrently polls
// (particleSensor.check()/available()/nextSample()). Calling them directly
// from the callback let two tasks interleave I2C transactions on the shared
// bus with no locking, which explains both the intermittently corrupted
// register readback observed live today (fifo=0x00/red=0x6F instead of the
// requested 0x40/0x1F) and the zero-DATA failures downstream of it. The
// callback now only records intent; loop() performs all Stage 9 I2C work.
// The pre-existing non-experiment SET_PROFILE callback path is outside this
// engineering correction and remains separately flagged for production work.
enum Stage9PendingCommand : uint8_t {
  STAGE9_PENDING_NONE = 0,
  STAGE9_PENDING_SET_PROFILE,
  STAGE9_PENDING_CLEAR_PROFILE,
  STAGE9_PENDING_START,
  STAGE9_PENDING_STOP,
  STAGE9_PENDING_BENCH_START,
  STAGE9_PENDING_WARMUP_BENCH_START,
};
static volatile uint8_t g_stage9PendingCommand = STAGE9_PENDING_NONE;
// TIME_SYNC requires a STATUS response, and sendStatus() reads MAX30102 FIFO
// pointers. Record the request in the callback task and perform those I2C
// reads from loop() alongside every other Stage 9 sensor operation.
static volatile bool g_stage9StatusPending = false;
#endif

static SensorProfile g_activeProfile = PROFILE_DORSAL;

// ── CRC16-CCITT (0xFFFF init, poly 0x1021) — matches receiver/protocol_decoder.py
static uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; b++) {
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : (crc << 1);
    }
  }
  return crc;
}

// ── Verify actual registers match the requested profile before trusting it.
// Register addresses are re-declared locally (sensor_profiles.h) because the
// SparkFun MAX3010x v1.1.2 library does not export its internal register
// constants — confirmed by reading MAX30105.cpp during bundle compile
// verification, not assumed.
static bool applyAndVerifyProfile(const SensorProfile &p) {
  particleSensor.setup(p.led_brightness, p.sample_avg, p.led_mode,
                        p.sample_rate_sps, p.pulse_width_us, p.adc_range_na);
  delay(50);

  uint8_t modeCfg = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_MODECONFIG);
  uint8_t ledPa1  = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_LED1_PULSEAMP);
  bool ledOk = (ledPa1 == p.led_brightness);
  bool modeOk = ((modeCfg & MAX30105_MODECONFIG_MASK) == p.led_mode);

  if (ledOk && modeOk) {
    g_sensorConfigId = p.config_id;
    return true;
  }
  g_sensorConfigId = SENSOR_CONFIG_UNVERIFIED;
  return false;
}

#ifdef VEINNET_STAGE9_EXPERIMENT
static uint8_t stage9Spo2Register(uint16_t adcRangeNa) {
  const uint8_t adcBits = adcRangeNa == 4096 ? 0x20 : adcRangeNa == 8192 ? 0x40 : 0x60;
  return adcBits | 0x0F; // 400 sps (0x0C) + 411 us (0x03)
}

static void stage9SafeOff() {
  // SHDN correction: writing MODECONFIG=0x00 sets MODE=000, which is not a
  // documented mode and does NOT assert SHDN (bit 7) -- it does not
  // guarantee the analog LED-pulse/ADC-integration pipeline actually stops.
  // A conversion already in flight under the PRIOR LED setting can still
  // complete and push into the FIFO after this call, landing as a stale
  // sample in whatever session starts next (traced on-device: config-17
  // LED-on value 11001/4665 appeared 31 samples into a subsequent
  // LED-off/config-117 capture; see
  // var/stage9-evidence/ambient-light-diagnostic/ and
  // tests/test_stage9_cross_session_leak.py). particleSensor.shutDown()
  // asserts SHDN via the vendored library's own read-modify-write
  // (MAX30105.cpp bitMask(MODECONFIG, 0x7F, 0x80)), which the datasheet
  // documents as: "the IC will continue to respond to I2C commands but
  // will not update with or take new readings" -- a real guarantee no
  // further conversions occur, not an ambiguous mode value. The later
  // MODECONFIG=0x03 write in stage9StartCapture() already clears SHDN as
  // part of that full-register overwrite, so no separate wakeUp() call is
  // needed.
  particleSensor.shutDown();
  particleSensor.writeRegister8(MAX30105_ADDRESS, MAX30105_REG_LED1_PULSEAMP, 0x00);
  particleSensor.writeRegister8(MAX30105_ADDRESS, MAX30105_REG_LED2_PULSEAMP, 0x00);
  g_streaming = false;
  g_startRequested = false;
  g_transmission.clear();
  g_stage9AlcOvfSeenSincePacket = false;
}

static bool stage9PrepareProfile(const SensorProfile &p) {
  const uint8_t redPa = stage9RedPa(p);
  const uint8_t irPa = stage9IrPa(p);
  stage9SafeOff();
  particleSensor.clearFIFO();
  g_fifoOverflowCount = particleSensor.getHardwareQueueOverflowCount();
  particleSensor.writeRegister8(MAX30105_ADDRESS, 0x08, 0x40); // sample average 4
  particleSensor.writeRegister8(MAX30105_ADDRESS, 0x0A, stage9Spo2Register(p.adc_range_na));
  particleSensor.writeRegister8(MAX30105_ADDRESS, MAX30105_REG_LED1_PULSEAMP, redPa);
  particleSensor.writeRegister8(MAX30105_ADDRESS, MAX30105_REG_LED2_PULSEAMP, irPa);

  const uint8_t mode = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_MODECONFIG);
  const uint8_t fifo = particleSensor.readRegister8(MAX30105_ADDRESS, 0x08);
  const uint8_t spo2 = particleSensor.readRegister8(MAX30105_ADDRESS, 0x0A);
  const uint8_t red = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_LED1_PULSEAMP);
  const uint8_t ir = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_LED2_PULSEAMP);
  // mode no longer needs to read back exactly 0x00: stage9SafeOff() now
  // calls particleSensor.shutDown() (SHDN correction, see stage9SafeOff()),
  // which sets bit 7 while preserving whatever mode bits were previously
  // active -- the SHDN bit being set is the actual guarantee that
  // conversions have stopped, not the whole register reading zero.
  const bool verified = (mode & MAX30105_MODECONFIG_SHDN_BIT) != 0 && fifo == 0x40 &&
      spo2 == stage9Spo2Register(p.adc_range_na) &&
      red == redPa && ir == irPa;
  if (Serial) {
    Serial.print("# STAGE9_PROFILE config="); Serial.print(p.config_id);
    Serial.print(" mode="); Serial.print(mode, HEX);
    Serial.print(" fifo="); Serial.print(fifo, HEX);
    Serial.print(" spo2="); Serial.print(spo2, HEX);
    Serial.print(" red="); Serial.print(red, HEX);
    Serial.print(" ir="); Serial.print(ir, HEX);
    Serial.print(" verified="); Serial.println(verified ? "true" : "false");
  }
  g_sensorConfigId = verified ? p.config_id : SENSOR_CONFIG_UNVERIFIED;
  if (!verified) stage9SafeOff();
  return verified;
}

static bool stage9StartCapture() {
  if (!g_sensorAvailable || g_stage9SelectedProfile == NULL ||
      !stage9PrepareProfile(*g_stage9SelectedProfile)) return false;
  // Read BEFORE clearFIFO() below resets it to 0: this is the cumulative
  // software-ring overflow count from whatever capture last ran (only
  // stage9StartCapture() ever calls clearFIFO(), so nothing else has reset
  // it since). Reading it after clearFIFO() would always print 0 and
  // silently discard the one number this diagnostic exists to report.
  if (Serial) {
    Serial.print("# STAGE9_PREV_CAPTURE_SW_QUEUE_OVERFLOW_TOTAL count=");
    Serial.println(particleSensor.getSoftwareQueueOverflowCount());
    Serial.print("# STAGE9_PREV_CAPTURE_ALC_OVF_TOTAL count=");
    Serial.println(g_stage9AlcOvfEventCount);
  }
  particleSensor.clearFIFO();
  // Explicit FIFO pointer reset (belt-and-suspenders with clearFIFO() above)
  // plus a dummy INTR_STATUS_1 read to clear any stale interrupt-status bits
  // left over from a prior cycle. A MAX30102 FIFO that believes it is still
  // mid-cycle, or an unread INTR_STATUS_1, is a known cause of the sensor
  // silently ceasing to produce new samples with no error indication.
  particleSensor.writeRegister8(MAX30105_ADDRESS, 0x04, 0x00); // FIFO_WR_PTR
  particleSensor.writeRegister8(MAX30105_ADDRESS, 0x05, 0x00); // OVF_COUNTER
  particleSensor.writeRegister8(MAX30105_ADDRESS, 0x06, 0x00); // FIFO_RD_PTR
  particleSensor.readRegister8(MAX30105_ADDRESS, 0x00);        // INTR_STATUS_1, clear-on-read
  // particleSensor.clearFIFO() above also resets the library's software
  // ring (head/tail/freshCount/pendingCount) -- see vendored MAX30105.cpp/.h
  // in this sketch directory. Confirms no stale sample from a prior
  // profile/recording can be read as the first sample of this one.
  if (Serial) {
    Serial.print("# STAGE9_SW_QUEUE_RESET overflow_count=");
    Serial.println(particleSensor.getSoftwareQueueOverflowCount());
  }
  g_fifoOverflowCount = 0;
  // Every START has its own zero-based acquisition epoch. Multiple clean
  // command-7 acquisitions may share one sensor session on one connection.
  g_nextSampleIndex = 0;
  g_stage9SamplesSent = 0;
  g_stage9AlcOvfEventCount = 0;
  g_stage9AlcOvfSeenSincePacket = false;
  particleSensor.resetAcquisitionIntegrity(); // explicit new acquisition epoch
  particleSensor.writeRegister8(MAX30105_ADDRESS, MAX30105_REG_MODECONFIG, 0x03);
  const uint8_t modeReadback = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_MODECONFIG);
  const bool modeVerified = (modeReadback & MAX30105_MODECONFIG_MASK) == 0x03;
  if (Serial) {
    Serial.print("# STAGE9_START_MODECONFIG readback=0x"); Serial.print(modeReadback, HEX);
    Serial.print(" masked=0x"); Serial.print(modeReadback & MAX30105_MODECONFIG_MASK, HEX);
    Serial.print(" verified="); Serial.println(modeVerified ? "true" : "false");
  }
  if (!modeVerified) {
    g_sensorConfigId = SENSOR_CONFIG_UNVERIFIED;
    stage9SafeOff();
    return false;
  }
  g_startRequested = true;
  g_streaming = true;
  if (Serial) Serial.println("# STAGE9_START_ARMED g_streaming=true");
  return true;
}

// Runs from loop() only -- the one place allowed to touch the I2C bus for
// Stage 9 control actions. See g_stage9PendingCommand declaration for why.
static void benchClose(uint8_t outcome) {
  if (!g_benchFinal.armed) return;
  const uint8_t config = g_sensorConfigId;
  stage9SafeOff();
  particleSensor.pollHardwareOverflow();
  const uint64_t stamp = g_constructionClock.observe(micros(), millis());
  const uint8_t uncertain = particleSensor.getAcquisitionUncertainty() |
      (g_constructionClock.uncertain ? ACQUISITION_CLOCK_UNCERTAIN : 0);
  g_benchFinal.freeze(g_hardwareUid, g_sensorSessionId,
                      g_benchFinal.warmup ? g_stage9SamplesSent : g_nextSampleIndex, config,
      particleSensor.getHardwareQueueOverflowCount(),
      particleSensor.getSoftwareQueueOverflowCount(), uncertain, outcome, stamp);
  g_benchFinal.value.crc16 = crc16_ccitt((const uint8_t*)&g_benchFinal.value,
                                      FINAL_WITNESS_SIZE - CRC_SIZE);
  if (g_benchFinal.warmup) {
    g_benchFinal.freezeWarmup(g_benchWarmup.emitted, g_benchWarmup.measurementBoundaryUs);
    g_benchFinal.warmupValue.crc16 = crc16_ccitt(g_benchFinal.bytes(), g_benchFinal.size()-CRC_SIZE);
    // Config denotes the verified selection, not active LED drive. SafeOff
    // has asserted SHDN and zeroed both LEDs; START always reapplies/readbacks
    // the selection before wake. Only a fully clean v2 closure retains it.
    const bool clean = outcome == 0 && config == 25 &&
        g_benchFinal.value.emitted_samples == STAGE9_CAPTURE_SAMPLES &&
        g_benchWarmup.emitted == BENCH_WARMUP_PAIRS &&
        !g_benchFinal.value.fifo_overflow_count &&
        !g_benchFinal.value.software_overflow_count && !uncertain;
    g_sensorConfigId = clean ? config : SENSOR_CONFIG_UNAVAILABLE;
  }
  finalChar.write(g_benchFinal.bytes(), g_benchFinal.size());
}

static void stage9ProcessPendingCommand() {
  uint8_t cmd = g_stage9PendingCommand;
  if (cmd != STAGE9_PENDING_NONE) {
    g_stage9PendingCommand = STAGE9_PENDING_NONE;
    switch (cmd) {
      case STAGE9_PENDING_SET_PROFILE:
        if (g_stage9SelectedProfile != NULL) stage9PrepareProfile(*g_stage9SelectedProfile);
        break;
      case STAGE9_PENDING_CLEAR_PROFILE:
        g_sensorConfigId = SENSOR_CONFIG_UNVERIFIED;
        stage9SafeOff();
        break;
      case STAGE9_PENDING_START:
        g_benchWarmup.legacy();
        stage9StartCapture();
        break;
      case STAGE9_PENDING_BENCH_START:
        g_benchWarmup.legacy();
        g_benchFinal.arm(g_benchNonce);
        if (!stage9StartCapture()) benchClose(2);
        break;
      case STAGE9_PENDING_WARMUP_BENCH_START:
        g_benchFinal.arm(g_benchNonce, true);
        g_benchWarmup.begin(millis());
        if (!stage9StartCapture()) benchClose(2);
        else g_benchWarmup.startedMs = millis(); // sensor has just been woken
        break;
      case STAGE9_PENDING_STOP:
        benchClose(2);
        // Read before stage9SafeOff() (which does not itself reset this
        // counter -- only clearFIFO() does) so a manually-stopped capture's
        // software-ring overflow total is visible without waiting for the
        // next START.
        if (Serial) {
          Serial.print("# STAGE9_STOP_SW_QUEUE_OVERFLOW_TOTAL count=");
          Serial.println(particleSensor.getSoftwareQueueOverflowCount());
          Serial.print("# STAGE9_STOP_ALC_OVF_TOTAL count=");
          Serial.println(g_stage9AlcOvfEventCount);
        }
        stage9SafeOff();
        break;
      default:
        break;
    }
  }
  // Process this after the pending profile command so TIME_SYNC observes and
  // acknowledges the committed config even when both writes arrive before
  // the next loop iteration.
  if (g_stage9StatusPending) {
    g_stage9StatusPending = false;
    sendStatus();
  }
}
#endif

// Hardware counter saturates at 31 and is cleared by a FIFO pop. The
// driver latches it before every drain; this additional poll never wraps it.
static void pollFifoOverflow() {
  particleSensor.pollHardwareOverflow();
  g_fifoOverflowCount = particleSensor.getHardwareQueueOverflowCount();
}

#ifdef VEINNET_STAGE9_EXPERIMENT
// Diagnostic-only: MAX30105::getINT1() reads INTR_STATUS_1 (address 0x00,
// clear-on-read); bit 0x20 is ALC_OVF (ambient light cancellation
// overflow). Polled every loop() iteration alongside pollFifoOverflow() --
// same "may miss one event between polls" limitation, same accepted
// tradeoff (spec marks INT as "skip in Phase 1", not wired as a real
// interrupt). Nothing else reads register 0x00 during streaming, so this
// clear-on-read does not race any other consumer.
static void pollAmbientLightOverflow() {
  if (particleSensor.getINT1() & 0x20) {
    g_stage9AlcOvfSeenSincePacket = true;
    g_stage9AlcOvfEventCount++;
  }
}
#endif

// ── STATUS packet ────────────────────────────────────────────────────────
static ChargeState readChargeState() {
  // HIGH also covers a full battery; this pin alone cannot establish "charged".
  return digitalRead(CHG_STAT_PIN) == LOW ? CHARGE_STATE_CHARGING : CHARGE_STATE_DISCHARGING;
}

static void updateBatteryAdcEnable() {
  // Seeed requires the divider enabled throughout active charging.
  digitalWrite(BATTERY_ADC_ENABLE_PIN,
               readChargeState() == CHARGE_STATE_CHARGING ? LOW : HIGH);
}

static uint32_t g_batteryRaw = 0;
static uint16_t readBatteryMillivolts() {
  digitalWrite(BATTERY_ADC_ENABLE_PIN, LOW);
  delay(10); // Bounded divider settling; reads are allowed only while idle.
  analogRead(BATTERY_ADC_PIN); // Discard the first conversion after enable.
  const uint32_t raw = analogRead(BATTERY_ADC_PIN);
  g_batteryRaw = raw;
  updateBatteryAdcEnable(); // Restore charge-aware state, also on invalid reads.
  return batteryMillivoltsFromRaw(raw);
}

// USB diagnostics perform no optical, BLE-control, or identity writes.
static void pollBatteryDiagnostic() {
  static char command[16];
  static uint8_t length = 0;
  static bool overflow = false;
  for (uint8_t budget = 0; budget < 16 && Serial.available(); ++budget) {
    const char c = Serial.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (length < sizeof(command)-1) command[length++] = c;
      else overflow = true;
      continue;
    }
    command[length] = 0;
    if (!overflow && strcmp(command, "BATTERY") == 0) {
      const uint16_t mv = g_streaming ? BATTERY_MV_UNKNOWN_SENTINEL : readBatteryMillivolts();
      char uid[17];
      snprintf(uid, sizeof(uid), "%08lX%08lX", (unsigned long)(g_hardwareUid >> 32), (unsigned long)(uint32_t)g_hardwareUid);
      Serial.print("{\"event\":\"battery\",\"diagnostic_version\":\"1.0.0\",\"hardware_uid\":\""); Serial.print(uid);
      Serial.print("\",\"short_code\":\"VN-");
      char code[6]; snprintf(code, sizeof(code), "%03u", VEINNET_SHORT_CODE_NUMBER); Serial.print(code);
      Serial.print("\",\"firmware_version\":\"3.0.2\",\"uptime_ms\":"); Serial.print(millis());
      Serial.print(",\"raw_adc\":"); if (g_streaming) Serial.print("null"); else Serial.print(g_batteryRaw);
      Serial.print(",\"battery_mv\":"); if (mv == BATTERY_MV_UNKNOWN_SENTINEL) Serial.print("null"); else Serial.print(mv);
      Serial.print(",\"charge_indication\":\""); Serial.print(readChargeState() == CHARGE_STATE_CHARGING ? "CHARGING" : "NOT_CHARGING_OR_FULL");
      Serial.print("\",\"usb_connected\":"); Serial.print((NRF_POWER->USBREGSTATUS & POWER_USBREGSTATUS_VBUSDETECT_Msk) ? "true" : "false");
      Serial.print(",\"validation_status\":\""); Serial.print(g_streaming ? "BUSY" : mv == BATTERY_MV_UNKNOWN_SENTINEL ? "INVALID" : "HARDWARE_VALIDATION_REQUIRED");
      Serial.println("\"}");
    }
    length = 0; overflow = false;
  }
}
static void sendStatus() {
  StatusPacket s;
  s.protocol_version = VEINNET_PROTOCOL_VERSION;
  s.packet_type = PACKET_TYPE_STATUS;
  s.hardware_uid = g_hardwareUid;

  // Candidate ADC voltage; the backend requires per-UID physical evidence.
  // Never pause optical streaming for an ADC measurement.
  s.battery_mv = g_streaming ? BATTERY_MV_UNKNOWN_SENTINEL : readBatteryMillivolts();
  s.battery_pct = BATTERY_PCT_UNKNOWN_SENTINEL; // Percentage belongs to backend policy.
  s.charge_state = readChargeState();
  s.contact_state = (g_sensorAvailable && g_streaming) ? CONTACT_STATE_PRESENT : CONTACT_STATE_NONE;
  s.fifo_write_ptr = g_sensorAvailable ? particleSensor.getWritePointer() : 0;
  s.fifo_read_ptr = g_sensorAvailable ? particleSensor.getReadPointer() : 0;
  s.fifo_overflow_count = particleSensor.getHardwareQueueOverflowCount();
  s.software_overflow_count = particleSensor.getSoftwareQueueOverflowCount();
  s.acquisition_uncertainty = particleSensor.getAcquisitionUncertainty();
  s.sensor_config_id = g_sensorConfigId;
  s.firmware_version_major = 3;
  s.firmware_version_minor = 0;
  s.firmware_version_patch = 2;
  s.uptime_s = millis() / 1000;
  s.crc16 = crc16_ccitt((const uint8_t*)&s, STATUS_PACKET_SIZE - CRC_SIZE);

  if (statusChar.notifyEnabled()) {
    statusChar.notify((const uint8_t*)&s, STATUS_PACKET_SIZE);
  }
}

// ── CONTROL packet handling ─────────────────────────────────────────────
static void onControlWrite(uint16_t conn_hdl, BLECharacteristic *chr, uint8_t *data, uint16_t len) {
  (void)conn_hdl; (void)chr;
#ifdef VEINNET_STAGE9_EXPERIMENT
  // Unconditional entry trace, before any other processing, to prove
  // whether this Ada Callback Task invocation happens at all and with what
  // raw bytes -- diagnosing whether a downstream failure is "callback never
  // ran" vs "callback ran but a later step failed silently".
  if (Serial) {
    Serial.print("# ONCONTROLWRITE_ENTRY len="); Serial.print(len);
    Serial.print(" bytes=");
    for (uint16_t i = 0; i < len; i++) {
      if (data[i] < 0x10) Serial.print('0');
      Serial.print(data[i], HEX);
    }
    Serial.println();
  }
#endif
  if (len < CONTROL_PACKET_SIZE) return;

  ControlPacket c;
  memcpy(&c, data, CONTROL_PACKET_SIZE);

  uint16_t crc = crc16_ccitt((const uint8_t*)&c, CONTROL_PACKET_SIZE - CRC_SIZE);
#ifdef VEINNET_STAGE9_EXPERIMENT
  if (Serial) {
    Serial.print("# ONCONTROLWRITE_CRC command="); Serial.print(c.command);
    Serial.print(" computed=0x"); Serial.print(crc, HEX);
    Serial.print(" received=0x"); Serial.print(c.crc16, HEX);
    Serial.print(" protocol_version="); Serial.println(c.protocol_version);
  }
#endif
  if (crc != c.crc16) return; // corrupt control write — ignore, do not act on it
  if (c.protocol_version != VEINNET_PROTOCOL_VERSION) return;

  switch (c.command) {
    case CONTROL_CMD_BENCH_START:
#ifdef VEINNET_STAGE9_EXPERIMENT
      if (c.arg_u64 && !g_streaming && !g_benchFinal.armed &&
          c.arg_u64 != g_benchNonce && g_stage9PendingCommand == STAGE9_PENDING_NONE) {
        g_benchNonce = c.arg_u64;
        g_stage9PendingCommand = STAGE9_PENDING_BENCH_START;
      }
#endif
      break;
    case CONTROL_CMD_WARMUP_BENCH_START:
#ifdef VEINNET_STAGE9_EXPERIMENT
      if (c.arg0 == BENCH_WARMUP_POLICY && c.arg_u64 && !g_streaming &&
          !g_benchFinal.armed && !g_benchFinal.pending && c.arg_u64 != g_benchNonce &&
          g_stage9PendingCommand == STAGE9_PENDING_NONE && g_sensorConfigId == 25 &&
          g_stage9SelectedProfile && g_stage9SelectedProfile->config_id == 25) {
        g_benchNonce = c.arg_u64;
        g_stage9PendingCommand = STAGE9_PENDING_WARMUP_BENCH_START;
      }
#endif
      break;
    case CONTROL_CMD_START:
      // Identity remains readable when the sensor is absent, but acquisition
      // never starts until MAX30102 initialization and profile readback pass.
#ifdef VEINNET_STAGE9_EXPERIMENT
      // Deferred to loop() -- see g_stage9PendingCommand. This callback runs
      // on Bluefruit's Ada Callback Task, not loop(); it must never touch I2C.
      g_stage9PendingCommand = STAGE9_PENDING_START;
#else
      // Boot ends with MODECONFIG=SHDN and both LED currents at zero. Reapply
      // the selected profile on explicit START so acquisition can begin.
      g_startRequested = g_sensorAvailable && applyAndVerifyProfile(g_activeProfile);
      if (g_startRequested) { particleSensor.clearFIFO(); particleSensor.resetAcquisitionIntegrity(); }
      g_streaming = g_startRequested;
#endif
      break;
    case CONTROL_CMD_STOP:
#ifdef VEINNET_STAGE9_EXPERIMENT
      g_stage9PendingCommand = STAGE9_PENDING_STOP;
#else
      g_startRequested = false;
      g_streaming = false;
#endif
      break;
    case CONTROL_CMD_SET_PROFILE: {
      if (!g_sensorAvailable) break;
#ifdef VEINNET_STAGE9_EXPERIMENT
      const SensorProfile *p = findStage9Profile(c.arg0);
      if (Serial) {
        Serial.print("# STAGE9_SET_PROFILE arg=");
        Serial.println(c.arg0);
      }
      if (p == NULL) {
        g_stage9SelectedProfile = NULL;
        g_stage9PendingCommand = STAGE9_PENDING_CLEAR_PROFILE;
        break;
      }
      g_stage9SelectedProfile = p;
      g_stage9PendingCommand = STAGE9_PENDING_SET_PROFILE;
#else
      const SensorProfile &p = (c.arg0 == SENSOR_CONFIG_FINGERTIP) ? PROFILE_FINGERTIP : PROFILE_DORSAL;
      g_activeProfile = p;
      applyAndVerifyProfile(p);
#endif
      // New profile invalidates the current session (spec S4: reposition/
      // config change increments sensor_session_id).
      g_sensorSessionId++;
      break;
    }
    case CONTROL_CMD_NEW_SESSION:
      g_sensorSessionId++;
      // Sample indices are scoped to sensor_session_id. The previous code
      // advanced the session while retaining the prior session's terminal
      // index, causing the next first packet to be rejected as loss/order.
      g_nextSampleIndex = 0;
      break;
    case CONTROL_CMD_TIME_SYNC:
      // Host records (device_monotonic_timestamp_us, host_utc) pairs itself
      // from DATA packets. STATUS is the explicit acknowledgment and also
      // provides a bounded notification-readiness/profile-state response.
#ifdef VEINNET_STAGE9_EXPERIMENT
      // sendStatus() reads MAX30102 FIFO pointers, so defer it out of the Ada
      // Callback Task to keep all Stage 9 I2C work serialized in loop().
      g_stage9StatusPending = true;
#else
      sendStatus();
#endif
      break;
    default:
      break;
  }
}

// ── Connection lifecycle ─────────────────────────────────────────────────
static void onConnect(uint16_t conn_hdl) {
  (void)conn_hdl;
  if (Serial) Serial.println("# Central connected.");
  // Streaming only starts once the host explicitly sends CONTROL_CMD_START
  // (see receiver/ble_central.py) — never auto-streams on bare connect.
}

#ifdef VEINNET_STAGE9_EXPERIMENT
// Event-driven CCCD-write observer for the DATA characteristic. Fires exactly
// when the central's notification subscription write actually lands, unlike
// polling notifyEnabled() from loop(), which can observe a stale value if the
// write hasn't been processed by the SoftDevice yet.
static void onDataCccdWrite(uint16_t conn_hdl, BLECharacteristic *chr, uint16_t cccd_value) {
  (void)conn_hdl; (void)chr;
  g_dataNotifySubscribed = (cccd_value & BLE_GATT_HVX_NOTIFICATION) != 0;
  if (Serial) {
    Serial.print("# DATA_CCCD_WRITE value=0x"); Serial.print(cccd_value, HEX);
    Serial.print(" subscribed="); Serial.println(g_dataNotifySubscribed ? "true" : "false");
  }
}
#endif

static void onDisconnect(uint16_t conn_hdl, uint8_t reason) {
  (void)conn_hdl; (void)reason;
  g_startRequested = false;
  g_streaming = false;
#ifdef VEINNET_STAGE9_EXPERIMENT
  g_dataNotifySubscribed = false;
#endif
#ifdef VEINNET_STAGE9_EXPERIMENT
  // Same Ada Callback Task as onControlWrite -- defer the I2C work to loop()
  // rather than calling stage9SafeOff() here directly.
  if (g_sensorAvailable) g_stage9PendingCommand = STAGE9_PENDING_STOP;
#endif
  if (Serial) Serial.println("# Central disconnected; advertising resumes automatically.");
  // Bluefruit.Advertising.restartOnDisconnect(true) (set in setup()) resumes
  // advertising without further action here. The host-side orchestrator
  // treats this event as session-ending: it emits DEVICE_DISCONNECTED and
  // issues a fresh CONTROL_CMD_NEW_SESSION + CONTROL_CMD_START on reconnect,
  // so no sample-index or session-id continuity is assumed across a
  // disconnect on either side.
}

// Identity BLE is deliberately initialized before probing MAX30102. This
// permits XIAO-only UID/VN bring-up while all acquisition remains fail-closed.
static void initializeIdentityBle() {
  // Requests a larger connection MTU/event length so the ~181-byte DATA
  // packet (18 samples) fits in one notification once the central
  // negotiates it. Actual negotiated MTU is a physical-BLE-stack property
  // of the connected central and is NOT verified on real hardware by this
  // bundle (see README S9 "physical BLE MTU/reliability validation").
  const uint32_t uidLow = NRF_FICR->DEVICEID[0];
  const uint32_t uidHigh = NRF_FICR->DEVICEID[1];
  g_hardwareUid = ((uint64_t)uidHigh << 32) | uidLow;
  Bluefruit.configPrphConn(247, BLE_GAP_EVENT_LENGTH_MIN, 16, 16);
  Bluefruit.begin(1, 0); // 1 peripheral connection, 0 central roles
  Bluefruit.setName(VEINNET_BLE_DEVICE_NAME);
  Bluefruit.Periph.setConnectCallback(onConnect);
  Bluefruit.Periph.setDisconnectCallback(onDisconnect);

  ppgService.begin();

  dataChar.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
#ifdef VEINNET_STAGE9_EXPERIMENT
  dataChar.setCccdWriteCallback(onDataCccdWrite);
#endif
  dataChar.begin();

  statusChar.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  statusChar.begin();

  controlChar.setPermission(SECMODE_NO_ACCESS, SECMODE_OPEN);
  controlChar.setWriteCallback(onControlWrite);
  controlChar.begin();

  IdentityValue identity = {};
  identity.protocol_version = VEINNET_PROTOCOL_VERSION;
  identity.flags = VEINNET_SHORT_CODE_NUMBER > 0 ? IDENTITY_FLAG_PROVISIONED : 0;
  identity.hardware_uid = g_hardwareUid;
  identity.short_code = VEINNET_SHORT_CODE_NUMBER;
  identity.crc16 = crc16_ccitt((const uint8_t*)&identity, IDENTITY_VALUE_SIZE - CRC_SIZE);
  identityChar.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  identityChar.begin();
#ifdef VEINNET_STAGE9_EXPERIMENT
  finalChar.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  finalChar.begin();
#endif
  identityChar.write((const uint8_t*)&identity, IDENTITY_VALUE_SIZE);

  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  // 128-bit Service Data AD element: service UUID in Bluetooth little-endian
  // wire order, protocol_version (u8), then canonical UID as little-endian
  // uint64. Together with Flags this is exactly 30 legacy-ADV bytes.
  const uint8_t serviceData[] = {
    0x9E,0xCA,0xDC,0x24,0x0E,0xE5,0xA9,0xE0,0x93,0xF3,0xA3,0xB5,0x00,0x02,0x4E,0x6E,
    VEINNET_PROTOCOL_VERSION,
    (uint8_t)(g_hardwareUid), (uint8_t)(g_hardwareUid >> 8),
    (uint8_t)(g_hardwareUid >> 16), (uint8_t)(g_hardwareUid >> 24),
    (uint8_t)(g_hardwareUid >> 32), (uint8_t)(g_hardwareUid >> 40),
    (uint8_t)(g_hardwareUid >> 48), (uint8_t)(g_hardwareUid >> 56)
  };
  Bluefruit.Advertising.addData(BLE_GAP_AD_TYPE_SERVICE_DATA_128BIT_UUID,
                                serviceData, sizeof(serviceData));
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.start(0); // 0 = advertise indefinitely

  if (Serial) Serial.println("# VeinNet Sensor V2 identity advertising.");
}

static void initializeSensorIfPresent() {
  Wire.begin();
  for (int i = 0; i < 10 && !g_sensorAvailable; i++) {
#ifdef VEINNET_STAGE9_EXPERIMENT
    // Stage 8 physically established this exact module at 100 kHz. The
    // experimental sweep does not raise the bus clock without a separate
    // validation gate.
    g_sensorAvailable = particleSensor.begin(Wire, I2C_SPEED_STANDARD);
#else
    g_sensorAvailable = particleSensor.begin(Wire, I2C_SPEED_FAST);
#endif
    if (!g_sensorAvailable) delay(500);
  }
  if (!g_sensorAvailable) {
    g_streaming = false;
    g_sensorConfigId = SENSOR_CONFIG_UNAVAILABLE;
    if (Serial) Serial.println("# SENSOR_UNAVAILABLE: MAX30102 not found; identity BLE remains available.");
    return;
  }

  // Boot-level shutdown diagnostic: full explicit soft-reset (MODECONFIG
  // bit 0x40, polled until self-clearing per softReset()) THEN assert SHDN
  // (bit 0x80) THEN force both LED pulse-amplitude registers to 0x00, the
  // instant the sensor is reachable on I2C -- before .setup()'s own
  // internal softReset() or any profile load can leave the part converting
  // with the LED physically active. Confirmed on-device by direct visual
  // inspection: the red LED inside the optical aperture was on immediately
  // at USB power-up even after a prior fix that only asserted SHDN without
  // a preceding reset. Register values are captured here and reported via
  // Serial once available (setup() prints g_bootReg09/0C/0D after this
  // function returns, since Serial may not be ready yet at this point).
  g_bootReg09BeforeReset = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_MODECONFIG);
  particleSensor.softReset();
  g_bootReg09AfterReset = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_MODECONFIG);
  particleSensor.shutDown();
  particleSensor.writeRegister8(MAX30105_ADDRESS, MAX30105_REG_LED1_PULSEAMP, 0x00);
  particleSensor.writeRegister8(MAX30105_ADDRESS, MAX30105_REG_LED2_PULSEAMP, 0x00);
  g_bootReg09AfterShdn = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_MODECONFIG);
  g_bootReg0CLed1Pa = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_LED1_PULSEAMP);
  g_bootReg0DLed2Pa = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_LED2_PULSEAMP);

  particleSensor.clearFIFO();
  g_fifoOverflowCount = 0;
#ifdef VEINNET_STAGE9_EXPERIMENT
  // setup() establishes the library's internal two-channel FIFO parser.
  // Zero pulse amplitude ensures this initialization emits no optical drive;
  // mode and both PA registers are then explicitly disabled again. setup()
  // internally calls softReset(), which can clear SHDN -- stage9SafeOff()
  // immediately after re-asserts it so no window is left with SHDN clear.
  particleSensor.setup(0x00, 4, 2, 400, 411, 16384);
  stage9SafeOff();
  {
    const uint8_t mode = particleSensor.readRegister8(MAX30105_ADDRESS, MAX30105_REG_MODECONFIG);
    if (Serial) {
      Serial.print("# BOOT_SHUTDOWN_VERIFY mode=0x"); Serial.print(mode, HEX);
      Serial.print(" shdn_asserted="); Serial.println((mode & MAX30105_MODECONFIG_SHDN_BIT) != 0 ? "true" : "false");
    }
  }
  g_sensorConfigId = SENSOR_CONFIG_UNAVAILABLE;
#else
  if (!applyAndVerifyProfile(g_activeProfile) && Serial) {
    Serial.println("# WARNING: sensor register readback did not match requested profile.");
  }
  g_streaming = g_startRequested && g_sensorConfigId != SENSOR_CONFIG_UNAVAILABLE;
#endif
}

// ── Setup ────────────────────────────────────────────────────────────────
void setup() {
  digitalWrite(BATTERY_ADC_ENABLE_PIN, LOW);
  pinMode(BATTERY_ADC_ENABLE_PIN, OUTPUT);
  pinMode(CHG_STAT_PIN, INPUT);
  updateBatteryAdcEnable();
  Serial.begin(115200);

  pinMode(CHARGE_CURRENT_PIN, OUTPUT);
  digitalWrite(CHARGE_CURRENT_PIN, CHARGE_CURRENT_50MA_LEVEL); // 50mA default (spec S8)
  analogReference(AR_INTERNAL);
  analogReadResolution(12);
  analogSampleTime(40); // High-impedance battery divider; pinned core API.

  // Sensor shutdown is safety-critical and must not wait on a serial
  // monitor being attached. The previous ordering blocked here for up to
  // 10 seconds waiting for Serial before ever touching the MAX30102 --
  // during that entire window the LED was left in whatever state powered
  // on with, outside firmware control (confirmed on-device: LED on
  // immediately at USB power-up, before any bounded-wait timeout could
  // have elapsed). initializeSensorIfPresent() asserts SHDN as its first
  // I2C action, so it must run before, not after, the Serial wait.
  initializeIdentityBle();
  initializeSensorIfPresent();

  unsigned long t0 = millis();
  while (!Serial && millis() - t0 < 10000) delay(10); // bounded startup window, diagnostics only

  g_sensorSessionId = 1; // first session this boot; a fresh NEW_SESSION
                          // command bumps this further on every (re)connect
  if (Serial && g_sensorAvailable) {
    Serial.println("# VeinNet Sensor V2 sensor ready.");
    Serial.print("# BOOT_REG09 before_reset=0x"); Serial.print(g_bootReg09BeforeReset, HEX);
    Serial.print(" after_reset=0x"); Serial.print(g_bootReg09AfterReset, HEX);
    Serial.print(" after_shdn=0x"); Serial.print(g_bootReg09AfterShdn, HEX);
    Serial.print(" shdn_asserted="); Serial.println((g_bootReg09AfterShdn & MAX30105_MODECONFIG_SHDN_BIT) != 0 ? "true" : "false");
    Serial.print("# BOOT_LED_PA led1_0x0C=0x"); Serial.print(g_bootReg0CLed1Pa, HEX);
    Serial.print(" led2_0x0D=0x"); Serial.println(g_bootReg0DLed2Pa, HEX);
  }

  // Final boot-state interlock. This must remain the last sensor operation
  // in setup(): MAX30105::setup() calls softReset(), which clears SHDN and
  // configures an active sampling mode. Do not rely on an earlier shutdown.
  if (g_sensorAvailable) {
    particleSensor.writeRegister8(MAX30105_ADDRESS, 0x09, 0x80); // SHDN = 1
    particleSensor.writeRegister8(MAX30105_ADDRESS, 0x0C, 0x00); // LED1_PA = 0
    particleSensor.writeRegister8(MAX30105_ADDRESS, 0x0D, 0x00); // LED2_PA = 0
    particleSensor.clearFIFO();
    g_fifoOverflowCount = 0;
    }
}

// ── Loop ─────────────────────────────────────────────────────────────────
static uint8_t g_packetBuf[DATA_HEADER_SIZE + VEINNET_MAX_SAMPLES_PER_PACKET * SAMPLE_PAIR_SIZE + CRC_SIZE];
static uint32_t g_lastStatusMs = 0;

void loop() {
  updateBatteryAdcEnable();
  pollBatteryDiagnostic();
  // Poll through idle periods so more than one unobserved micros wrap cannot
  // be silently interpreted as a short interval on the next START.
  g_constructionClock.observe(micros(), millis());
  if (g_constructionClock.uncertain)
    particleSensor.markAcquisitionUncertain(ACQUISITION_CLOCK_UNCERTAIN);
#ifdef VEINNET_STAGE9_EXPERIMENT
  // Processed unconditionally, even if not connected, so a command set by
  // onControlWrite an instant before a disconnect is still handled cleanly
  // (stage9SafeOff() is always a safe no-op-ish sensor write regardless of
  // link state).
  stage9ProcessPendingCommand();
  if (g_benchFinal.pending && Bluefruit.connected() && finalChar.notifyEnabled()) {
    if (finalChar.notify(g_benchFinal.bytes(), g_benchFinal.size()))
      g_benchFinal.pending = false;
  }
#endif
#ifdef VEINNET_STAGE9_EXPERIMENT
  if (g_streaming && g_benchWarmup.expired(millis())) {
    benchClose(2); stage9SafeOff(); sendStatus(); return;
  }
#endif
  if (!Bluefruit.connected()) {
    delay(10);
    return;
  }

  // Drain FIFO in bursts (spec S2) — never a delay()-based clock. No sensor
  // method is called on the XIAO-only path.
  uint8_t available = 0;
  if (g_sensorAvailable) {
    particleSensor.check(g_transmission.mayDrain());
    pollFifoOverflow();
#ifdef VEINNET_STAGE9_EXPERIMENT
    pollAmbientLightOverflow();
#endif
    available = particleSensor.available();
  }

  // Explicit stop instead of silently emitting overwritten/loss-uncertain data.
  if (g_streaming && (particleSensor.getHardwareQueueOverflowCount() ||
      particleSensor.getSoftwareQueueOverflowCount() || particleSensor.getAcquisitionUncertainty())) {
#ifdef VEINNET_STAGE9_EXPERIMENT
    benchClose(1);
    stage9SafeOff();
#else
    particleSensor.shutDown();
    g_streaming = g_startRequested = false;
    g_transmission.clear();
#endif
    sendStatus(); // cumulative counters survive shutdown and repeated reads
    return;
  }
  if (available > 0 && g_streaming && !g_transmission.pending) {
    uint8_t batch = min(available, (uint8_t)VEINNET_MAX_SAMPLES_PER_PACKET);
    BLEConnection *connection = Bluefruit.Connection(Bluefruit.connHandle());
    const uint16_t mtuPayload = connection == NULL ? 0 : connection->getMtu() - 3;
    const uint16_t fixedBytes = DATA_HEADER_SIZE + CRC_SIZE;
    if (mtuPayload < fixedBytes + SAMPLE_PAIR_SIZE) {
      g_sensorConfigId = SENSOR_CONFIG_UNVERIFIED;
#ifdef VEINNET_STAGE9_EXPERIMENT
      stage9SafeOff();
#else
      particleSensor.shutDown(); g_streaming = g_startRequested = false;
#endif
      sendStatus(); return;
    }
    batch = min(batch, (uint8_t)((mtuPayload - fixedBytes) / SAMPLE_PAIR_SIZE));
#ifdef VEINNET_STAGE9_EXPERIMENT
    const uint32_t remaining = g_benchWarmup.active ?
        BENCH_WARMUP_PAIRS - g_benchWarmup.emitted : STAGE9_CAPTURE_SAMPLES - g_stage9SamplesSent;
    batch = min(batch, (uint8_t)min(remaining, (uint32_t)VEINNET_MAX_SAMPLES_PER_PACKET));
#endif
    DataPacketHeader hdr;
    hdr.protocol_version = VEINNET_PROTOCOL_VERSION;
    hdr.packet_type = PACKET_TYPE_DATA;
#ifdef VEINNET_STAGE9_EXPERIMENT
    if (g_benchWarmup.active) hdr.packet_type = PACKET_TYPE_WARMUP;
#endif
    hdr.hardware_uid = g_hardwareUid;
    hdr.sensor_session_id = g_sensorSessionId;
    hdr.first_sample_index = g_nextSampleIndex;
    hdr.sample_count = batch;
    // Construction time only. FIFO samples have no acquisition timestamps.
    hdr.device_monotonic_timestamp_us = g_constructionClock.observe(micros(), millis());
    if (g_constructionClock.uncertain) particleSensor.markAcquisitionUncertain(ACQUISITION_CLOCK_UNCERTAIN);
    hdr.effective_sample_rate_hz = 100;
    hdr.sensor_config_id = g_sensorConfigId;
    hdr.flags = 0;
#ifdef VEINNET_STAGE9_EXPERIMENT
    if (g_stage9AlcOvfSeenSincePacket) { hdr.flags |= FLAG_AMBIENT_LIGHT_OVERFLOW_SINCE_LAST; g_stage9AlcOvfSeenSincePacket = false; }
#endif
    hdr.fifo_overflow_count = particleSensor.getHardwareQueueOverflowCount();
    hdr.software_overflow_count = particleSensor.getSoftwareQueueOverflowCount();
    hdr.acquisition_uncertainty = particleSensor.getAcquisitionUncertainty();
    memcpy(g_packetBuf, &hdr, DATA_HEADER_SIZE);
    for (uint8_t i = 0; i < batch; ++i) {
      SamplePair pair{particleSensor.getFIFORed(), particleSensor.getFIFOIR()};
      particleSensor.nextSample();
      memcpy(g_packetBuf + DATA_HEADER_SIZE + i*SAMPLE_PAIR_SIZE, &pair, SAMPLE_PAIR_SIZE);
    }
    const uint16_t crc = crc16_ccitt(g_packetBuf, DATA_HEADER_SIZE + batch*SAMPLE_PAIR_SIZE);
    memcpy(g_packetBuf + DATA_HEADER_SIZE + batch*SAMPLE_PAIR_SIZE, &crc, CRC_SIZE);
    g_transmission.stage(DATA_HEADER_SIZE + batch*SAMPLE_PAIR_SIZE + CRC_SIZE, batch);
  }
#ifdef VEINNET_STAGE9_EXPERIMENT
  const bool subscribed = g_dataNotifySubscribed;
#else
  const bool subscribed = dataChar.notifyEnabled();
#endif
  if (g_transmission.pending && g_streaming && subscribed) {
    uint8_t accepted = g_transmission.acknowledge(
        dataChar.notify(g_packetBuf, g_transmission.length), g_nextSampleIndex);
    if (accepted) {
#ifdef VEINNET_STAGE9_EXPERIMENT
      if (g_benchWarmup.active) {
        g_benchWarmup.emitted += accepted;
        if (g_benchWarmup.emitted == BENCH_WARMUP_PAIRS) {
          // Host-visible phase boundary only: no I2C, queue clear, shutdown,
          // profile/session change, or loss-epoch reset at this transition.
          g_benchWarmup.active = false;
          g_benchWarmup.measurementBoundaryUs = g_constructionClock.observe(micros(), millis());
          if (g_constructionClock.uncertain)
            particleSensor.markAcquisitionUncertain(ACQUISITION_CLOCK_UNCERTAIN);
          g_nextSampleIndex = 0; // phase-local measurement index
        }
      } else g_stage9SamplesSent += accepted;
#endif
    }
#ifdef VEINNET_STAGE9_EXPERIMENT
    if (!g_benchWarmup.active && !g_transmission.pending && g_stage9SamplesSent == STAGE9_CAPTURE_SAMPLES) {
      benchClose(0);
      // Cumulative software-ring overflow for the just-finished recording,
      // logged separately from g_fifoOverflowCount (hardware FIFO_OVF_COUNTER,
      // carried in every DATA packet header). A nonzero value here means
      // loop()/BLE notify backpressure let the MCU-side ring lap itself
      // during this recording -- distinct from, and not covered by, the
      // stale-sample-at-profile-boundary fix (see stage9StartCapture()).
      if (Serial) {
        Serial.print("# STAGE9_SW_QUEUE_OVERFLOW_TOTAL count=");
        Serial.println(particleSensor.getSoftwareQueueOverflowCount());
        Serial.print("# STAGE9_ALC_OVF_TOTAL count=");
        Serial.println(g_stage9AlcOvfEventCount);
      }
      stage9SafeOff();
      if (!g_benchFinal.warmup) g_sensorConfigId = SENSOR_CONFIG_UNAVAILABLE;
      // Explicit engineering completion indication. The host records this
      // only after it has accepted all 3000 pairs; it is not a replacement
      // for DATA integrity validation.
      sendStatus();
    }
#endif
  }

  if (millis() - g_lastStatusMs >= 1000) {
    sendStatus();
    g_lastStatusMs = millis();
  }
}
