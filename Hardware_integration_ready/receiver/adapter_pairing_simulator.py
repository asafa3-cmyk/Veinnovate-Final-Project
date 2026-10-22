"""adapter_pairing_simulator.py — deterministic, hardware-free multi-device
simulator for the identity/assignment layer (AGENTS.md First Task /
required-work item 5: "Add a deterministic multi-device simulator").

Every scenario builder below returns a plain, ordered list of `SimEvent`s
with explicit timestamps — no wall-clock sleeping, no randomness — so tests
and a future demo/CLI can replay the exact same sequence and get the exact
same result every time. `DeterministicAdapterSimulator.run()` is the only
thing that mutates `live_ble_discovery.LiveBleDiscovery` /
`ble_connection_state.BleConnectionRegistry` state from these events; it
never touches `adapter_inventory` or `patient_assignment` directly — those
are exercised through `assignment_gate.assign()` by the caller, exactly as
a real pairing flow would.

Required coverage (AGENTS.md item 5), each available as a named builder:
  - adapter OFF/ON events               -> events_power_cycle
  - zero newly discovered adapters      -> scenario_zero_new_adapters
  - one newly discovered adapter        -> scenario_one_new_adapter
  - multiple newly discovered adapters  -> scenario_multiple_new_adapters
  - UID/code mismatch                   -> mismatching_verifier (used with assignment_gate.assign)
  - duplicate identity                  -> scenario_duplicate_identity
  - disconnect and reconnect            -> scenario_disconnect_reconnect
  - concurrent assignment attempts      -> see tests/test_assignment_gate.py (thread-driven,
                                            no simulator state needed beyond scenario_one_new_adapter)
"""

from __future__ import annotations

from dataclasses import dataclass

from ble_connection_state import BleConnectionRegistry
from live_ble_discovery import LiveBleDiscovery


class SimEventKind:
    POWER_ON = "POWER_ON"
    POWER_OFF = "POWER_OFF"
    DISCONNECT = "DISCONNECT"
    RECONNECT = "RECONNECT"


@dataclass(frozen=True)
class SimEvent:
    kind: str
    hardware_uid: str
    at_t: float
    source_token: str = "sim"


class DeterministicAdapterSimulator:
    """Replays an ordered list of SimEvents against live-discovery and
    connection-state stores at explicit, caller-chosen timestamps."""

    def __init__(
        self,
        live_discovery: LiveBleDiscovery,
        connection_registry: BleConnectionRegistry,
        *,
        base_time: float = 0.0,
    ) -> None:
        self.live_discovery = live_discovery
        self.connection_registry = connection_registry
        self.base_time = base_time

    def run(self, events: list[SimEvent]) -> None:
        for event in sorted(events, key=lambda e: e.at_t):
            t = self.base_time + event.at_t
            if event.kind == SimEventKind.POWER_ON:
                self.live_discovery.on_advertisement(
                    event.hardware_uid, source_token=event.source_token, now=t
                )
            elif event.kind == SimEventKind.POWER_OFF:
                self.live_discovery.on_power_off(event.hardware_uid, source_token=event.source_token)
            elif event.kind == SimEventKind.DISCONNECT:
                self.connection_registry.disconnected(event.hardware_uid, now=t)
            elif event.kind == SimEventKind.RECONNECT:
                self.connection_registry.connected(event.hardware_uid, now=t)
            else:
                raise ValueError(f"unknown SimEvent kind {event.kind!r}")


# --- Scenario builders -------------------------------------------------


def events_power_cycle(hardware_uid: str, *, on_t: float = 0.0, off_t: float = 1.0) -> list[SimEvent]:
    """A single adapter's OFF->ON->OFF power cycle."""
    return [
        SimEvent(SimEventKind.POWER_ON, hardware_uid, at_t=on_t),
        SimEvent(SimEventKind.POWER_OFF, hardware_uid, at_t=off_t),
    ]


def scenario_zero_new_adapters() -> list[SimEvent]:
    """No adapter powers on during the pairing window."""
    return []


def scenario_one_new_adapter(hardware_uid: str, *, at_t: float = 0.0) -> list[SimEvent]:
    return [SimEvent(SimEventKind.POWER_ON, hardware_uid, at_t=at_t)]


def scenario_multiple_new_adapters(hardware_uids: list[str], *, spacing_s: float = 0.01) -> list[SimEvent]:
    """More than one previously-unseen adapter appears in the same window —
    AGENTS.md requires the pairing flow refuse to guess among them."""
    return [
        SimEvent(SimEventKind.POWER_ON, uid, at_t=i * spacing_s) for i, uid in enumerate(hardware_uids)
    ]


def scenario_duplicate_identity(hardware_uid: str, *, at_t: float = 0.0) -> list[SimEvent]:
    """Two distinct broadcast sources simultaneously claim the same
    provisioned hardware_uid (e.g. a cloning/flashing error) — must be
    rejected, never resolved by picking one arbitrarily."""
    return [
        SimEvent(SimEventKind.POWER_ON, hardware_uid, at_t=at_t, source_token="radio-a"),
        SimEvent(SimEventKind.POWER_ON, hardware_uid, at_t=at_t, source_token="radio-b"),
    ]


def scenario_disconnect_reconnect(
    hardware_uid: str, *, on_t: float = 0.0, disconnect_t: float = 1.0, reconnect_t: float = 2.0
) -> list[SimEvent]:
    """Adapter appears, an (external) connect happens, then the radio link
    drops and comes back — connection state only; live-discovery keeps
    advertising throughout, matching a real BLE supervision-timeout blip
    rather than a full power cycle."""
    return [
        SimEvent(SimEventKind.POWER_ON, hardware_uid, at_t=on_t),
        SimEvent(SimEventKind.DISCONNECT, hardware_uid, at_t=disconnect_t),
        SimEvent(SimEventKind.RECONNECT, hardware_uid, at_t=reconnect_t),
    ]


def mismatching_verifier(reported_uid: str):
    """Builds an `assignment_gate.IdentityVerifier` that always reports a
    fixed (wrong) hardware_uid on connect — simulates a device that
    advertises one identity but answers a different one once connected
    (UID/code mismatch)."""

    def _verify(_expected_uid: str) -> str:
        return reported_uid

    return _verify
