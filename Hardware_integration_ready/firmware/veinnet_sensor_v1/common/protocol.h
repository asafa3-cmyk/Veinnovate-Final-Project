// protocol.h — BLE Protocol V3 (VeinNet hardware-edge bundle)
//
// Shared wire-format definition for the XIAO nRF52840 firmware and the
// Jetson-side Python decoder (receiver/protocol_decoder.py). Both sides MUST
// stay byte-for-byte in sync with this file; there is no shared codegen, so
// any field change here requires a matching change there and a protocol_version bump.
//
// All multi-byte integers are little-endian. All structs are packed (no
// compiler-inserted padding) — see PACKED below.
//
// Architecture-sensitive: changing UUIDs or payload layout requires the
// reviewing agent's recorded sign-off per the source repository's AGENTS.md
// before the change lands there. This bundle's initial version was verified
// against the frozen VeinNet PersonalDelta contract during bundle creation.

#ifndef VEINNET_PROTOCOL_H
#define VEINNET_PROTOCOL_H

#include <stdint.h>
#include "acquisition_integrity.h"

#if defined(__GNUC__)
  #define PACKED __attribute__((packed))
#else
  #define PACKED
#endif

// ── Identity ─────────────────────────────────────────────────────────────
#define VEINNET_PROTOCOL_VERSION   3

#define VEINNET_BLE_DEVICE_NAME    "VeinNetSensorV3"

// Custom 128-bit base UUID family: 6E4E0001-... through 6E4E0003-...
// (Distinct from the superseded Phase-2 19B1xxxx UUIDs — a new protocol
// version requires new UUIDs so old centrals never silently misparse V1
// frames as the old 6-byte payload.)
#define VEINNET_SERVICE_UUID        "6E4E0200-B5A3-F393-E0A9-E50E24DCCA9E"
#define VEINNET_CHAR_DATA_UUID      "6E4E0201-B5A3-F393-E0A9-E50E24DCCA9E"
#define VEINNET_CHAR_STATUS_UUID    "6E4E0202-B5A3-F393-E0A9-E50E24DCCA9E"
#define VEINNET_CHAR_CONTROL_UUID   "6E4E0203-B5A3-F393-E0A9-E50E24DCCA9E"
#define VEINNET_CHAR_IDENTITY_UUID  "6E4E0204-B5A3-F393-E0A9-E50E24DCCA9E"
// Opt-in V3 bench extension, schema 1. Existing layouts are unchanged.
#define VEINNET_CHAR_FINAL_UUID     "6E4E0205-B5A3-F393-E0A9-E50E24DCCA9E"

// ── Packet types ─────────────────────────────────────────────────────────
enum PacketType : uint8_t {
  PACKET_TYPE_DATA    = 1,
  PACKET_TYPE_STATUS   = 2,
  PACKET_TYPE_CONTROL  = 3,
  PACKET_TYPE_WARMUP   = 5, // opt-in bench policy 1; same raw-pair layout
};

// ── DATA packet flags (bitfield) ────────────────────────────────────────
#define FLAG_FIFO_OVERFLOW_SINCE_LAST   (1u << 0)
#define FLAG_CLIPPED_SAMPLE_PRESENT     (1u << 1)
#define FLAG_SESSION_RESET              (1u << 2)
#define FLAG_LOW_BATTERY                (1u << 3)
// Diagnostic-only (config-17 no-contact-baseline investigation,
// STAGE9_METRIC_CORRECTION_HANDOFF.md): MAX30105 INTR_STATUS_1 ALC_OVF
// (ambient light cancellation overflow) observed since the previous DATA
// packet. Set only under VEINNET_STAGE9_EXPERIMENT.
#define FLAG_AMBIENT_LIGHT_OVERFLOW_SINCE_LAST (1u << 4)

// Maximum paired RED+IR samples per DATA notification. Chosen so a full
// packet (header + N*8 + crc) fits inside a negotiated 247-byte ATT MTU
// (185-byte payload after ATT/GATT overhead): 33 + N*8 + 2 <= 185 → N <= 18.
#define VEINNET_MAX_SAMPLES_PER_PACKET  18

// ── DATA packet header (fixed 40 bytes, followed by sample_count*8 bytes
//    of RED/IR pairs, followed by a 2-byte CRC16-CCITT over every
//    preceding byte of this packet) ─────────────────────────────────────
typedef struct PACKED {
  uint8_t  protocol_version;              // = VEINNET_PROTOCOL_VERSION
  uint8_t  packet_type;                   // = PACKET_TYPE_DATA
  uint64_t hardware_uid;                  // DEVICEID[1] || DEVICEID[0]
  uint32_t sensor_session_id;             // increments on reboot / reacquire / reposition
  uint32_t first_sample_index;            // zero-based monotonic index within sensor_session_id
  uint8_t  sample_count;                  // 1..VEINNET_MAX_SAMPLES_PER_PACKET
  uint64_t device_monotonic_timestamp_us; // packet construction clock; NOT optical acquisition time
  uint16_t effective_sample_rate_hz;      // nominal 100
  uint8_t  sensor_config_id;              // verified register profile in effect
  uint8_t  flags;                         // FLAG_* bitfield
  uint32_t fifo_overflow_count;           // cumulative overflow counter, this session
  uint32_t software_overflow_count;
  uint8_t acquisition_uncertainty;
} DataPacketHeader;                        // sizeof == 33

// Each sample pair, following the header, sample_count times:
typedef struct PACKED {
  uint32_t red;  // full 18-bit value, zero-extended into 32 bits — never clipped
  uint32_t ir;   // full 18-bit value, zero-extended into 32 bits — never clipped
} SamplePair;     // sizeof == 8

#define DATA_HEADER_SIZE 40
#define SAMPLE_PAIR_SIZE   8
#define CRC_SIZE           2

// ── STATUS packet (fixed 36 bytes incl. CRC) ────────────────────────────
typedef struct PACKED {
  uint8_t  protocol_version;
  uint8_t  packet_type;                   // = PACKET_TYPE_STATUS
  uint64_t hardware_uid;
  uint16_t battery_mv;
  uint8_t  battery_pct;
  uint8_t  charge_state;                  // 0=discharging,1=charging,2=charged,3=fault
  uint8_t  contact_state;                 // 0=no_contact,1=contact,2=unstable
  uint8_t  fifo_write_ptr;
  uint8_t  fifo_read_ptr;
  uint32_t fifo_overflow_count;
  uint8_t  sensor_config_id;
  uint8_t  firmware_version_major;
  uint8_t  firmware_version_minor;
  uint8_t  firmware_version_patch;
  uint32_t uptime_s;
  uint32_t software_overflow_count;
  uint8_t acquisition_uncertainty;
  uint16_t crc16;
} StatusPacket;                            // sizeof == 36

#define STATUS_PACKET_SIZE 36

// Read-only connected identity. CRC covers the first 14 bytes. short_code
// is the numeric suffix of VN-NNN; zero means not provisioned and is always
// rejected by the Jetson identity gate. Reserved bytes must remain zero.
typedef struct PACKED {
  uint8_t  protocol_version;
  uint8_t  flags;                         // bit 0 = provisioned
  uint64_t hardware_uid;                  // canonical numeric UID
  uint16_t short_code;
  uint16_t reserved;
  uint16_t crc16;
} IdentityValue;

#define IDENTITY_VALUE_SIZE 16
#define IDENTITY_FLAG_PROVISIONED 0x01
#define IDENTITY_ADV_PAYLOAD_SIZE 9

// ── CONTROL packet (host → device, variable length up to 20 bytes) ─────
enum ControlCommand : uint8_t {
  CONTROL_CMD_START          = 1,
  CONTROL_CMD_STOP           = 2,
  CONTROL_CMD_SET_PROFILE    = 3,
  CONTROL_CMD_TIME_SYNC      = 4,
  CONTROL_CMD_NEW_SESSION    = 5,
  CONTROL_CMD_BENCH_START    = 6, // arg_u64: unique nonzero acquisition nonce
  CONTROL_CMD_WARMUP_BENCH_START = 7, // arg0=policy 1, arg_u64=fresh nonce
};

typedef struct PACKED {
  uint8_t  protocol_version;
  uint8_t  packet_type;                   // = PACKET_TYPE_CONTROL
  uint8_t  command;                       // ControlCommand
  uint8_t  arg0;                          // e.g. profile_id for SET_PROFILE
  uint64_t arg_u64;                       // e.g. host UTC epoch_us for TIME_SYNC
  uint16_t crc16;
} ControlPacket;                           // sizeof == 14

#define CONTROL_PACKET_SIZE 14

// Only BENCH_START produces this immutable acquisition-closing witness.
// Device timestamps are construction/boundary clocks, not optical clocks.
typedef struct PACKED {
  uint8_t protocol_version;
  uint8_t packet_type; // 4; separate FINAL characteristic
  uint8_t witness_version; // 1
  uint64_t hardware_uid;
  uint64_t acquisition_nonce;
  uint32_t sensor_session_id;
  uint32_t emitted_samples;
  uint8_t sensor_config_id;
  uint32_t fifo_overflow_count;
  uint32_t software_overflow_count;
  uint8_t acquisition_uncertainty;
  uint8_t outcome; // 0=bounded complete, 1=integrity failure, 2=aborted/start failed
  uint64_t boundary_timestamp_us;
  uint16_t crc16;
} FinalIntegrityWitness;
#define FINAL_WITNESS_SIZE 48

// Schema 2 explicitly closes both phases of warm-up policy 1.
typedef struct PACKED {
  uint8_t protocol_version, packet_type, witness_version;
  uint64_t hardware_uid, acquisition_nonce;
  uint32_t sensor_session_id, emitted_samples; // measurement pairs only
  uint8_t sensor_config_id;
  uint32_t fifo_overflow_count, software_overflow_count; // whole epoch
  uint8_t acquisition_uncertainty, outcome;
  uint64_t boundary_timestamp_us; // closing construction clock
  uint32_t warmup_emitted_samples;
  uint64_t measurement_boundary_timestamp_us; // phase switch construction clock
  uint16_t crc16;
} WarmupFinalIntegrityWitness;
#define WARMUP_FINAL_WITNESS_SIZE 60
static_assert(sizeof(WarmupFinalIntegrityWitness) == WARMUP_FINAL_WITNESS_SIZE,
              "warm-up FINAL wire size");

// ── Sensor config identity ──────────────────────────────────────────────
// sensor_config_id must be assigned only after reading back and verifying
// the MAX30102 registers actually match the requested profile (spec §2).
enum SensorConfigId : uint8_t {
  SENSOR_CONFIG_UNAVAILABLE = 0,  // sensor absent or profile unverified; never accepted
  SENSOR_CONFIG_UNVERIFIED = SENSOR_CONFIG_UNAVAILABLE,
  SENSOR_CONFIG_FINGERTIP  = 1,
  SENSOR_CONFIG_DORSAL     = 2,
};

#endif // VEINNET_PROTOCOL_H

static_assert(sizeof(DataPacketHeader) == DATA_HEADER_SIZE, "DATA wire size");
static_assert(sizeof(StatusPacket) == STATUS_PACKET_SIZE, "STATUS wire size");
static_assert(sizeof(FinalIntegrityWitness) == FINAL_WITNESS_SIZE, "FINAL wire size");
