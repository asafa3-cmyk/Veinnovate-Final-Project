// Stage 8 bench-only MAX30102 identification and conservative FIFO probe.
// This is not production firmware and does not advertise BLE.
//
// Safety sequence:
//   1. Boot performs no MAX30102 register writes and never enables its LEDs.
//   2. IDENTIFY probes only I2C address 0x57 and reads REV_ID/PART_ID.
//   3. START_FIFO is rejected until PART_ID is exactly 0x15.
//   4. RED/IR LED current is limited to 0x1F (~6.2 mA) and mode is enabled
//      only after every configuration register has passed readback.
//   5. Every completion or error disables both LED channels and sensor mode.

#include <Wire.h>

static const uint8_t SENSOR_ADDRESS = 0x57;
static const uint8_t REG_FIFO_WR_PTR = 0x04;
static const uint8_t REG_OVF_COUNTER = 0x05;
static const uint8_t REG_FIFO_RD_PTR = 0x06;
static const uint8_t REG_FIFO_DATA = 0x07;
static const uint8_t REG_FIFO_CONFIG = 0x08;
static const uint8_t REG_MODE_CONFIG = 0x09;
static const uint8_t REG_SPO2_CONFIG = 0x0A;
static const uint8_t REG_LED1_PA = 0x0C;
static const uint8_t REG_LED2_PA = 0x0D;
static const uint8_t REG_REV_ID = 0xFE;
static const uint8_t REG_PART_ID = 0xFF;

static const uint8_t EXPECTED_PART_ID = 0x15;
static const uint8_t FIFO_AVERAGE_4 = 0x40;
static const uint8_t SPO2_ADC_4096_SPS400_PW411 = 0x2F;
static const uint8_t LED_CURRENT_6MA = 0x1F;
static const uint8_t MODE_SPO2 = 0x03;
static const uint32_t CAPTURE_MS = 10000;
static const uint32_t RAW_FULL_SCALE = 262143UL;

static bool identityVerified = false;
static char commandBuffer[24];
static uint8_t commandLength = 0;

static bool probeAddress() {
  Wire.beginTransmission(SENSOR_ADDRESS);
  return Wire.endTransmission() == 0;
}

static bool writeRegister(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(SENSOR_ADDRESS);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

static bool readRegister(uint8_t reg, uint8_t &value) {
  Wire.beginTransmission(SENSOR_ADDRESS);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(SENSOR_ADDRESS, (uint8_t)1) != 1) return false;
  value = Wire.read();
  return true;
}

static void printHexByte(uint8_t value) {
  if (value < 0x10) Serial.print('0');
  Serial.print(value, HEX);
}

static void safeSensorOff() {
  // MODE=0 and zero pulse amplitudes are safe even after a partial setup.
  writeRegister(REG_MODE_CONFIG, 0x00);
  writeRegister(REG_LED1_PA, 0x00);
  writeRegister(REG_LED2_PA, 0x00);
}

static void emitError(const char *code) {
  safeSensorOff();
  Serial.print("{\"event\":\"error\",\"code\":\"");
  Serial.print(code);
  Serial.println("\",\"led_drive_enabled\":false}");
}

static void identifySensor() {
  identityVerified = false;
  if (!probeAddress()) {
    Serial.println("{\"event\":\"identity\",\"i2c_address\":\"0x57\",\"ack\":false,\"verified\":false,\"led_drive_enabled\":false}");
    return;
  }

  uint8_t revision = 0;
  uint8_t part = 0;
  if (!readRegister(REG_REV_ID, revision) || !readRegister(REG_PART_ID, part)) {
    Serial.println("{\"event\":\"identity\",\"i2c_address\":\"0x57\",\"ack\":true,\"register_read\":false,\"verified\":false,\"led_drive_enabled\":false}");
    return;
  }

  identityVerified = part == EXPECTED_PART_ID;
  Serial.print("{\"event\":\"identity\",\"i2c_address\":\"0x57\",\"ack\":true,\"revision_id\":\"0x");
  printHexByte(revision);
  Serial.print("\",\"part_id\":\"0x");
  printHexByte(part);
  Serial.print("\",\"verified\":");
  Serial.print(identityVerified ? "true" : "false");
  Serial.println(",\"led_drive_enabled\":false}");
}

static bool configureConservativeFifo() {
  safeSensorOff();
  if (!writeRegister(REG_FIFO_WR_PTR, 0x00) ||
      !writeRegister(REG_OVF_COUNTER, 0x00) ||
      !writeRegister(REG_FIFO_RD_PTR, 0x00) ||
      !writeRegister(REG_FIFO_CONFIG, FIFO_AVERAGE_4) ||
      !writeRegister(REG_SPO2_CONFIG, SPO2_ADC_4096_SPS400_PW411) ||
      !writeRegister(REG_LED1_PA, LED_CURRENT_6MA) ||
      !writeRegister(REG_LED2_PA, LED_CURRENT_6MA)) {
    return false;
  }

  uint8_t fifo = 0, spo2 = 0, red = 0, ir = 0;
  if (!readRegister(REG_FIFO_CONFIG, fifo) ||
      !readRegister(REG_SPO2_CONFIG, spo2) ||
      !readRegister(REG_LED1_PA, red) ||
      !readRegister(REG_LED2_PA, ir) ||
      fifo != FIFO_AVERAGE_4 || spo2 != SPO2_ADC_4096_SPS400_PW411 ||
      red != LED_CURRENT_6MA || ir != LED_CURRENT_6MA) {
    return false;
  }

  // MODE is deliberately the final write: no conversion or LED pulse can
  // occur before the complete conservative profile passes readback.
  return writeRegister(REG_MODE_CONFIG, MODE_SPO2);
}

static bool readFifoPair(uint32_t &red, uint32_t &ir) {
  Wire.beginTransmission(SENSOR_ADDRESS);
  Wire.write(REG_FIFO_DATA);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(SENSOR_ADDRESS, (uint8_t)6) != 6) return false;
  red = ((uint32_t)Wire.read() << 16) | ((uint32_t)Wire.read() << 8) | Wire.read();
  ir = ((uint32_t)Wire.read() << 16) | ((uint32_t)Wire.read() << 8) | Wire.read();
  red &= 0x3FFFFUL;
  ir &= 0x3FFFFUL;
  return true;
}

static void runFifoCapture() {
  if (!identityVerified) {
    Serial.println("{\"event\":\"error\",\"code\":\"IDENTITY_REQUIRED\",\"led_drive_enabled\":false}");
    return;
  }
  if (!configureConservativeFifo()) {
    emitError("CONFIG_READBACK_FAILED");
    return;
  }

  Serial.println("{\"event\":\"capture_start\",\"duration_ms\":10000,\"sample_average\":4,\"physical_sample_rate_sps\":400,\"effective_sample_rate_hz\":100,\"pulse_width_us\":411,\"adc_range_na\":4096,\"red_pa\":\"0x1F\",\"ir_pa\":\"0x1F\"}");
  uint32_t count = 0;
  uint32_t saturated = 0;
  uint32_t start = millis();
  bool failed = false;

  while (millis() - start < CAPTURE_MS) {
    uint8_t writePointer = 0, readPointer = 0;
    if (!readRegister(REG_FIFO_WR_PTR, writePointer) ||
        !readRegister(REG_FIFO_RD_PTR, readPointer)) {
      failed = true;
      break;
    }
    uint8_t available = (writePointer - readPointer) & 0x1F;
    while (available-- > 0) {
      uint32_t red = 0, ir = 0;
      if (!readFifoPair(red, ir)) {
        failed = true;
        break;
      }
      if (red == RAW_FULL_SCALE || ir == RAW_FULL_SCALE) saturated++;
      Serial.print("{\"event\":\"sample\",\"index\":");
      Serial.print(count++);
      Serial.print(",\"red\":");
      Serial.print(red);
      Serial.print(",\"ir\":");
      Serial.print(ir);
      Serial.println('}');
    }
    if (failed) break;
    delay(1);
  }

  uint8_t overflow = 0xFF;
  bool overflowRead = readRegister(REG_OVF_COUNTER, overflow);
  safeSensorOff();
  if (failed || !overflowRead) {
    Serial.println("{\"event\":\"error\",\"code\":\"FIFO_READ_FAILED\",\"led_drive_enabled\":false}");
    return;
  }

  Serial.print("{\"event\":\"capture_complete\",\"samples\":");
  Serial.print(count);
  Serial.print(",\"fifo_overflow_count\":");
  Serial.print(overflow & 0x1F);
  Serial.print(",\"saturated_samples\":");
  Serial.print(saturated);
  Serial.println(",\"led_drive_enabled\":false}");
}

static void handleCommand(const char *command) {
  if (strcmp(command, "IDENTIFY") == 0) {
    identifySensor();
  } else if (strcmp(command, "START_FIFO") == 0) {
    runFifoCapture();
  } else if (strcmp(command, "STOP") == 0) {
    safeSensorOff();
    Serial.println("{\"event\":\"stopped\",\"led_drive_enabled\":false}");
  } else {
    Serial.println("{\"event\":\"error\",\"code\":\"UNKNOWN_COMMAND\",\"led_drive_enabled\":false}");
  }
}

void setup() {
  Serial.begin(115200);
  Wire.begin();
  Wire.setClock(100000);
  Serial.println("{\"event\":\"ready\",\"mode\":\"IDENTIFY_ONLY\",\"led_drive_enabled\":false}");
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      commandBuffer[commandLength] = '\0';
      if (commandLength > 0) handleCommand(commandBuffer);
      commandLength = 0;
    } else if (commandLength < sizeof(commandBuffer) - 1) {
      commandBuffer[commandLength++] = c;
    } else {
      commandLength = 0;
      Serial.println("{\"event\":\"error\",\"code\":\"COMMAND_TOO_LONG\",\"led_drive_enabled\":false}");
    }
  }
  delay(1);
}
