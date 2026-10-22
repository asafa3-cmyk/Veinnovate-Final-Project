"""ble_connection_state.py — the "BLE Connection" state machine, kept
strictly separate from "Patient Assignment" (AGENTS.md: "A BLE connection
is the radio/data link between a XIAO and Jetson; a patient assignment is
the explicit, time-bounded binding of one VN-NNN adapter to one
patient/session. Connection alone must never create an assignment.").

This module never reads or writes `patient_assignment.py`'s store, and
nothing here accepts a patient_id. It is keyed by `hardware_uid` (the
provisioned identity), never by BLE address/name, so it composes cleanly
with `live_ble_discovery.py` and `adapter_inventory.py` without ever
exposing a raw BLE address as identity.

In this software-only POC, transitions are driven explicitly (by
`adapter_pairing_simulator.py` in tests/demo, or by the assignment gate
performing its "connect and re-read identity" verification step) rather
than by a real `bleak` client — see `ble_central.py` for the existing
real/simulated single-device transport this module is deliberately
decoupled from.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"


@dataclass(frozen=True)
class ConnectionRecord:
    hardware_uid: str
    state: ConnectionState
    updated_at_utc: float


class BleConnectionRegistry:
    """Per-`hardware_uid` connection status, isolated per device so a
    multi-adapter deployment never shares one process-global connection
    flag (AGENTS.md: "Support multiple simultaneously powered adapters...
    isolated per device/patient session")."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, ConnectionRecord] = {}

    def _set(self, hardware_uid: str, state: ConnectionState, now: float | None) -> ConnectionRecord:
        ts = now if now is not None else time.time()
        record = ConnectionRecord(hardware_uid=hardware_uid, state=state, updated_at_utc=ts)
        with self._lock:
            self._states[hardware_uid] = record
        return record

    def connecting(self, hardware_uid: str, *, now: float | None = None) -> ConnectionRecord:
        return self._set(hardware_uid, ConnectionState.CONNECTING, now)

    def connected(self, hardware_uid: str, *, now: float | None = None) -> ConnectionRecord:
        return self._set(hardware_uid, ConnectionState.CONNECTED, now)

    def disconnected(self, hardware_uid: str, *, now: float | None = None) -> ConnectionRecord:
        return self._set(hardware_uid, ConnectionState.DISCONNECTED, now)

    def get(self, hardware_uid: str) -> ConnectionRecord | None:
        with self._lock:
            return self._states.get(hardware_uid)

    def is_connected(self, hardware_uid: str) -> bool:
        record = self.get(hardware_uid)
        return record is not None and record.state == ConnectionState.CONNECTED
