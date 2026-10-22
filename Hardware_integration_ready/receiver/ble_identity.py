"""Strict BLE Protocol V3 adapter identity codec.

The canonical UID is the nRF52840 FICR value DEVICEID[1] || DEVICEID[0],
rendered as exactly 16 uppercase hexadecimal digits. Advertising carries
that UID in versioned 128-bit service data. The read-only GATT
identity value repeats the UID and the provisioned VN-NNN code so the
central can verify the exact connected peripheral before enabling data.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from protocol_decoder import crc16_ccitt


PROTOCOL_VERSION = 3
UID_PATTERN = re.compile(r"^[0-9A-F]{16}$")
SHORT_CODE_PATTERN = re.compile(r"^VN-(\d{3,5})$")
IDENTITY_VALUE_SIZE = 16
ADVERTISEMENT_PAYLOAD_SIZE = 9
_IDENTITY_BODY_FMT = "<BBQHH"  # version, flags, UID LE, short-code number, reserved
_PROVISIONED_FLAG = 0x01


class IdentityProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class BleIdentity:
    hardware_uid: str
    short_code: str
    protocol_version: int = PROTOCOL_VERSION


def canonical_uid(value: str | int) -> str:
    if isinstance(value, int):
        if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
            raise IdentityProtocolError("hardware UID is outside uint64 range")
        uid = f"{value:016X}"
    elif isinstance(value, str):
        uid = value.strip().upper()
    else:
        raise IdentityProtocolError("hardware UID must be a string or uint64")
    if not UID_PATTERN.fullmatch(uid):
        raise IdentityProtocolError("hardware UID must be exactly 16 hexadecimal digits")
    if uid in {"0000000000000000", "FFFFFFFFFFFFFFFF"}:
        raise IdentityProtocolError("unprogrammed/sentinel hardware UID is forbidden")
    return uid


def encode_advertising_payload(hardware_uid: str | int) -> bytes:
    """Service-data bytes after the scanner removes the 128-bit UUID."""
    return struct.pack("<BQ", PROTOCOL_VERSION, int(canonical_uid(hardware_uid), 16))


def decode_advertising_payload(raw: bytes) -> str:
    if len(raw) != ADVERTISEMENT_PAYLOAD_SIZE:
        raise IdentityProtocolError(
            f"advertising payload must be {ADVERTISEMENT_PAYLOAD_SIZE} bytes"
        )
    version, uid = struct.unpack("<BQ", raw)
    if version != PROTOCOL_VERSION:
        raise IdentityProtocolError(f"unsupported advertising protocol version {version}")
    return canonical_uid(uid)


def _short_code_number(short_code: str) -> int:
    match = SHORT_CODE_PATTERN.fullmatch(short_code)
    if match is None:
        raise IdentityProtocolError("short code must match VN-NNN")
    number = int(match.group(1))
    if not 1 <= number <= 0xFFFF:
        raise IdentityProtocolError("short code number is outside uint16 range")
    return number


def encode_identity_value(hardware_uid: str | int, short_code: str) -> bytes:
    body = struct.pack(
        _IDENTITY_BODY_FMT,
        PROTOCOL_VERSION,
        _PROVISIONED_FLAG,
        int(canonical_uid(hardware_uid), 16),
        _short_code_number(short_code),
        0,
    )
    return body + struct.pack("<H", crc16_ccitt(body))


def decode_identity_value(raw: bytes) -> BleIdentity:
    if len(raw) != IDENTITY_VALUE_SIZE:
        raise IdentityProtocolError(f"identity value must be {IDENTITY_VALUE_SIZE} bytes")
    body, received_crc = raw[:-2], struct.unpack_from("<H", raw, len(raw) - 2)[0]
    if crc16_ccitt(body) != received_crc:
        raise IdentityProtocolError("identity CRC mismatch")
    version, flags, uid, code_number, reserved = struct.unpack(_IDENTITY_BODY_FMT, body)
    if version != PROTOCOL_VERSION:
        raise IdentityProtocolError(f"unsupported identity protocol version {version}")
    if flags != _PROVISIONED_FLAG or reserved != 0:
        raise IdentityProtocolError("identity flags/reserved fields are invalid")
    hardware_uid = canonical_uid(uid)
    if code_number == 0:
        raise IdentityProtocolError("adapter is not provisioned")
    return BleIdentity(hardware_uid=hardware_uid, short_code=f"VN-{code_number:03d}")
