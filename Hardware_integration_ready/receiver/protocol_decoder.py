"""protocol_decoder.py — BLE Protocol V3 codec (V2 decode retained for archival replay only).

Mirrors ``firmware/veinnet_sensor_v1/common/protocol.h`` byte-for-byte. This
module owns BOTH directions (encode and decode) because it is also used by
``simulated_ble_source.py`` to produce deterministic synthetic DATA packets
for tests that must run without hardware.

Any field change here requires a matching change in protocol.h and a
protocol_version bump — the two are hand-kept in sync, there is no shared
codegen.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum


PROTOCOL_VERSION = 3

DATA_HEADER_SIZE = 40
SAMPLE_PAIR_SIZE = 8
CRC_SIZE = 2
STATUS_PACKET_SIZE = 36
CONTROL_PACKET_SIZE = 14
MAX_SAMPLES_PER_PACKET = 18

# Matches firmware/veinnet_sensor_v1/common/device_status.h sentinels —
# Invalid/unavailable measurement, never a fabricated 0mV/0% reading.
# Publication additionally requires per-UID physical validation; percentage
# is computed only by battery_telemetry.py, not reconstructed into voltage.
BATTERY_MV_UNKNOWN_SENTINEL = 0xFFFF
BATTERY_PCT_UNKNOWN_SENTINEL = 0xFF

# struct format strings — little-endian, matches the PACKED C structs exactly.
_LEGACY_DATA_HEADER_FMT = "<BBQIIBQHBBH"
_DATA_HEADER_FMT = "<BBQIIBQHBBIIB"   # 33 bytes
_SAMPLE_PAIR_FMT = "<II"            # 8 bytes
_LEGACY_STATUS_FULL_FMT = "<BBQHBBBBBHBBBBIH"
_STATUS_FULL_FMT = "<BBQHBBBBBIBBBBIIBH"  # 29 bytes total
_CONTROL_FMT = "<BBBBQH"            # 13 bytes

assert struct.calcsize(_DATA_HEADER_FMT) == DATA_HEADER_SIZE
assert struct.calcsize(_SAMPLE_PAIR_FMT) == SAMPLE_PAIR_SIZE
assert struct.calcsize(_STATUS_FULL_FMT) == STATUS_PACKET_SIZE
assert struct.calcsize(_CONTROL_FMT) == CONTROL_PACKET_SIZE


class PacketType(IntEnum):
    DATA = 1
    STATUS = 2
    CONTROL = 3
    WARMUP = 5


class Flags(IntEnum):
    FIFO_OVERFLOW_SINCE_LAST = 1 << 0
    CLIPPED_SAMPLE_PRESENT = 1 << 1
    SESSION_RESET = 1 << 2
    LOW_BATTERY = 1 << 3
    # Diagnostic-only (STAGE9_METRIC_CORRECTION_HANDOFF.md config-17
    # no-contact-baseline investigation): MAX30105 INTR_STATUS_1 ALC_OVF
    # (ambient light cancellation overflow), polled every loop() iteration
    # and latched until the next DATA packet is built -- set only under
    # VEINNET_STAGE9_EXPERIMENT. See veinnet_sensor_v1.ino's
    # pollAmbientLightOverflow()/g_stage9AlcOvfSeenSincePacket.
    AMBIENT_LIGHT_OVERFLOW_SINCE_LAST = 1 << 4


class SensorConfigId(IntEnum):
    UNAVAILABLE = 0
    UNVERIFIED = 0
    FINGERTIP = 1
    DORSAL = 2


class ControlCommand(IntEnum):
    START = 1
    STOP = 2
    SET_PROFILE = 3
    TIME_SYNC = 4
    NEW_SESSION = 5
    BENCH_START = 6
    WARMUP_BENCH_START = 7


class ProtocolError(ValueError):
    """Raised for malformed, corrupt, or version-mismatched packets."""


def crc16_ccitt(data: bytes) -> int:
    """CRC16-CCITT, init 0xFFFF, poly 0x1021 — matches firmware crc16_ccitt()."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass
class DataPacket:
    protocol_version: int
    packet_type: int
    hardware_uid: int
    sensor_session_id: int
    first_sample_index: int
    sample_count: int
    device_monotonic_timestamp_us: int
    effective_sample_rate_hz: int
    sensor_config_id: int
    flags: int
    fifo_overflow_count: int
    software_overflow_count: int = 0
    acquisition_uncertainty: int = 0
    red: list[int] = field(default_factory=list)
    ir: list[int] = field(default_factory=list)

    @property
    def last_sample_index(self) -> int:
        return self.first_sample_index + self.sample_count - 1

    @property
    def device_id(self) -> int:
        """Compatibility alias for internal V1-era callers; value is UID64."""
        return self.hardware_uid


@dataclass
class StatusPacket:
    protocol_version: int
    packet_type: int
    hardware_uid: int
    battery_mv: int
    battery_pct: int
    charge_state: int
    contact_state: int
    fifo_write_ptr: int
    fifo_read_ptr: int
    fifo_overflow_count: int
    sensor_config_id: int
    firmware_version: tuple[int, int, int]
    uptime_s: int
    software_overflow_count: int = 0
    acquisition_uncertainty: int = 0

    @property
    def device_id(self) -> int:
        return self.hardware_uid


def encode_data_packet(
    *,
    device_id: int,
    sensor_session_id: int,
    first_sample_index: int,
    device_monotonic_timestamp_us: int,
    effective_sample_rate_hz: int,
    sensor_config_id: int,
    flags: int,
    fifo_overflow_count: int,
    red: list[int],
    ir: list[int],
    software_overflow_count: int = 0,
    acquisition_uncertainty: int = 0,
) -> bytes:
    """Encode a DATA packet exactly as the firmware would. Used by the
    simulator and by round-trip tests — never by production Jetson code,
    which only ever decodes."""
    if not (0 < len(red) <= MAX_SAMPLES_PER_PACKET) or len(red) != len(ir):
        raise ProtocolError("sample_count out of range or RED/IR length mismatch")

    header = struct.pack(
        _DATA_HEADER_FMT,
        PROTOCOL_VERSION,
        PacketType.DATA,
        device_id,
        sensor_session_id,
        first_sample_index,
        len(red),
        device_monotonic_timestamp_us,
        effective_sample_rate_hz,
        sensor_config_id,
        flags,
        fifo_overflow_count,
        software_overflow_count,
        acquisition_uncertainty,
    )
    body = b"".join(struct.pack(_SAMPLE_PAIR_FMT, r, i) for r, i in zip(red, ir))
    payload = header + body
    crc = crc16_ccitt(payload)
    return payload + struct.pack("<H", crc)


def decode_data_packet(raw: bytes, *, expected_type=PacketType.DATA) -> DataPacket:
    if not raw or raw[0] not in (2, PROTOCOL_VERSION):
        raise ProtocolError("unsupported protocol_version")
    legacy = raw[0] == 2
    fmt = _LEGACY_DATA_HEADER_FMT if legacy else _DATA_HEADER_FMT
    size = struct.calcsize(fmt)
    if len(raw) < size + CRC_SIZE:
        raise ProtocolError("packet too short")
    fields = struct.unpack_from(fmt, raw)
    version, kind, uid, session, index, count, stamp, rate, config, flags, hw = fields[:11]
    if kind != expected_type or not 1 <= count <= MAX_SAMPLES_PER_PACKET:
        raise ProtocolError("invalid DATA type/sample_count")
    if len(raw) != size + count * SAMPLE_PAIR_SIZE + CRC_SIZE:
        raise ProtocolError("DATA length mismatch")
    if crc16_ccitt(raw[:-CRC_SIZE]) != struct.unpack_from("<H", raw, len(raw)-CRC_SIZE)[0]:
        raise ProtocolError("CRC mismatch")
    samples = [struct.unpack_from(_SAMPLE_PAIR_FMT, raw, size+i*SAMPLE_PAIR_SIZE) for i in range(count)]
    return DataPacket(protocol_version=version, packet_type=kind, hardware_uid=uid,
        sensor_session_id=session, first_sample_index=index, sample_count=count,
        device_monotonic_timestamp_us=stamp, effective_sample_rate_hz=rate,
        sensor_config_id=config, flags=flags, fifo_overflow_count=hw,
        software_overflow_count=0 if legacy else fields[11],
        acquisition_uncertainty=1 if legacy else fields[12],
        red=[x[0] for x in samples], ir=[x[1] for x in samples])

def decode_status_packet(raw: bytes) -> StatusPacket:
    if not raw or raw[0] not in (2, PROTOCOL_VERSION):
        raise ProtocolError("unsupported protocol_version")
    legacy = raw[0] == 2
    fmt = _LEGACY_STATUS_FULL_FMT if legacy else _STATUS_FULL_FMT
    if len(raw) != struct.calcsize(fmt):
        raise ProtocolError("STATUS length mismatch")
    fields = struct.unpack(fmt, raw)
    version, kind, uid, mv, pct, charge, contact, wp, rp, hw, config, major, minor, patch, uptime = fields[:15]
    if kind != PacketType.STATUS:
        raise ProtocolError("expected STATUS packet_type")
    if crc16_ccitt(raw[:-CRC_SIZE]) != fields[-1]:
        raise ProtocolError("STATUS CRC mismatch")
    return StatusPacket(protocol_version=version, packet_type=kind, hardware_uid=uid,
        battery_mv=mv, battery_pct=pct, charge_state=charge, contact_state=contact,
        fifo_write_ptr=wp, fifo_read_ptr=rp, fifo_overflow_count=hw,
        sensor_config_id=config, firmware_version=(major, minor, patch), uptime_s=uptime,
        software_overflow_count=0 if legacy else fields[15],
        acquisition_uncertainty=1 if legacy else fields[16])

def encode_control_packet(*, command: int, arg0: int = 0, arg_u64: int = 0) -> bytes:
    body = struct.pack(_CONTROL_FMT[:-1], PROTOCOL_VERSION, PacketType.CONTROL, command, arg0, arg_u64)
    crc = crc16_ccitt(body)
    return body + struct.pack("<H", crc)
