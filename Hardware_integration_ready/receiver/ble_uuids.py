"""Single source of truth for BLE Protocol V3 UUIDs on the
Python side. Must be kept in sync by hand with
firmware/veinnet_sensor_v1/common/protocol.h (no shared codegen — see that
file's header comment).
"""

from __future__ import annotations

SERVICE_UUID = "6e4e0200-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_DATA_UUID = "6e4e0201-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_STATUS_UUID = "6e4e0202-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_CONTROL_UUID = "6e4e0203-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_IDENTITY_UUID = "6e4e0204-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_FINAL_UUID = "6e4e0205-b5a3-f393-e0a9-e50e24dcca9e"
