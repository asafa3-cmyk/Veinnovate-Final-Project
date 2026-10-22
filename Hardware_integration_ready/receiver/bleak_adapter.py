"""bleak_adapter.py — the real (hardware) BleClientLike implementation,
wrapping `bleak`. Imported lazily by veinnet_edge_service.py's hardware path
only, so the rest of the receiver package (including ble_central.py's state
machine) stays importable and testable without bleak or Bluetooth hardware
installed. The scan transport has focused tests in
tests/test_bleak_adapter_discovery.py; see tests/test_ble_lifecycle.py for
the connection state-machine tests.
"""

from __future__ import annotations

from typing import Callable

from ble_identity import IdentityProtocolError, canonical_uid, decode_advertising_payload, decode_identity_value
from ble_uuids import CHAR_IDENTITY_UUID, SERVICE_UUID


class BleakClientAdapter:
    """Adapts a real bleak BleakClient to the BleClientLike protocol that
    ble_central.BleCentralController expects."""

    def __init__(self, address_or_device, disconnected_callback: Callable[[], None]) -> None:
        from bleak import BleakClient  # type: ignore

        def _on_disconnect(_client) -> None:
            disconnected_callback()

        self._client = BleakClient(address_or_device, disconnected_callback=_on_disconnect)

    async def connect(self) -> None:
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def start_notify(self, char_uuid: str, callback) -> None:
        await self._client.start_notify(char_uuid, callback)

    async def stop_notify(self, char_uuid: str) -> None:
        await self._client.stop_notify(char_uuid)

    async def write_gatt_char(self, char_uuid: str, data: bytes, response: bool = True) -> None:
        await self._client.write_gatt_char(char_uuid, data, response=response)

    async def read_gatt_char(self, char_uuid: str) -> bytes:
        return bytes(await self._client.read_gatt_char(char_uuid))

    def get_service_characteristic_uuids(self) -> set[str]:
        return {c.uuid for s in self._client.services for c in s.characteristics}


async def connect_to_device(device, disconnected_callback: Callable[[], None]) -> BleakClientAdapter:
    """Connect to the exact BLEDevice selected by validated V2 discovery."""
    if device is None:
        raise RuntimeError("validated BLE device handle is unavailable")
    return BleakClientAdapter(device, disconnected_callback)


async def verify_connected_identity(device, inventory_record, *, live_discovery=None, diagnostic=None):
    """One-shot pre-assignment read from the exact scanned BLEDevice.

    The connection is always closed; the per-UID manager establishes the
    streaming connection after the database commit and re-verifies again.
    """
    client = BleakClientAdapter(device, lambda: None)
    failed = False
    def mark(value):
        if diagnostic is not None:
            diagnostic(value)
    try:
        mark("gatt_connect")
        await client.connect()
        mark("gatt_services")
        available = {u.lower() for u in client.get_service_characteristic_uuids()}
        if CHAR_IDENTITY_UUID.lower() not in available:
            raise IdentityProtocolError("identity characteristic is unavailable")
        mark("gatt_identity_read")
        identity = decode_identity_value(await client.read_gatt_char(CHAR_IDENTITY_UUID))
        mark("gatt_identity_compare")
        if (
            canonical_uid(identity.hardware_uid) != canonical_uid(inventory_record.hardware_uid)
            or identity.short_code != inventory_record.short_code
        ):
            raise IdentityProtocolError("advertised/inventory/GATT identity mismatch")
    except Exception:
        failed = True
        raise
    finally:
        if not failed:
            mark("gatt_disconnect")
        try:
            await client.disconnect()
        except Exception:
            mark("gatt_disconnect")
            raise

    if live_discovery is not None:
        # GATT temporarily stops advertising. Obtain a real fresh advertisement
        # after disconnect instead of extending the gate's freshness limit.
        mark("discovery_refresh")
        await scan_v2_adapters(
            live_discovery, timeout_s=live_discovery.freshness_s,
            verified_source=(canonical_uid(identity.hardware_uid), str(device.address), device),
        )
    return identity


def _record_v2_advertisement(live_discovery, device, advertisement_data, *, verified_source=None) -> bool:
    """Accept only the complete V2 service-data identity. Returns True only
    for an actual advertisement from the verified source.

    The platform BLE address is an opaque source/connection handle, never an
    identity or fallback. Missing and malformed payloads remain invisible to
    live discovery.
    """
    payload = advertisement_data.service_data.get(SERVICE_UUID)
    if payload is None:
        return False
    try:
        uid = decode_advertising_payload(bytes(payload))
    except IdentityProtocolError:
        live_discovery.record_malformed_v2()
        return False
    handle = device
    verified = verified_source is not None and (uid, str(device.address)) == verified_source[:2]
    if verified:
        # BlueZ can return a new Python object for the same verified radio.
        # Preserve its verified handle only on a matching actual advertisement;
        # other sources remain separate duplicate-identity candidates.
        handle = verified_source[2]
    live_discovery.on_advertisement(uid, source_token=str(device.address), connection_handle=handle)
    return verified


async def run_v2_discovery(live_discovery, *, on_available=None) -> None:
    """Own one continuous BlueZ scan; refresh identity from real reports.

    DuplicateData is essential for freshness: unchanged service data must
    still report an advertising adapter. No scan/connect retry is performed.
    """
    import asyncio
    from bleak import BleakScanner  # type: ignore

    waiters = []
    live_discovery._v2_continuous_waiters = waiters

    def callback(device, advertisement_data):
        source = None
        uid = None
        was_fresh = False
        payload = advertisement_data.service_data.get(SERVICE_UUID)
        if payload is not None:
            try:
                uid = decode_advertising_payload(bytes(payload))
                was_fresh = bool(live_discovery.fresh_candidates(uid))
                source = next((s for s, _event in waiters
                               if (uid, str(device.address)) == s[:2]), None)
            except IdentityProtocolError:
                pass
        if _record_v2_advertisement(live_discovery, device, advertisement_data,
                                    verified_source=source):
            for registered, event in tuple(waiters):
                if registered[:2] == source[:2]:
                    event.set()
        if uid is not None and not was_fresh and on_available is not None:
            candidates = live_discovery.fresh_candidates(uid)
            if len(candidates) == 1:
                on_available(uid, candidates[0].connection_handle)

    scanner = BleakScanner(detection_callback=callback,
                          bluez={"filters": {"DuplicateData": True}})
    try:
        await scanner.start()
        await asyncio.Future()  # Service cancellation stops the sole scan.
    finally:
        live_discovery._v2_continuous_waiters = None
        await scanner.stop()


async def scan_v2_adapters(live_discovery, *, timeout_s: float = 5.0, verified_source=None) -> None:
    """Run a bounded platform scan and record valid V2 service-data identities.

    BLE addresses are retained only as opaque duplicate/source tokens and
    connection handles; they are never returned as adapter identity. The scan
    intentionally has no platform service-UUID prefilter: the firmware's full
    legacy advertisement carries 128-bit Service Data but no Service UUID List,
    and CoreBluetooth filters its UUID list before exposing Service Data.

    With `verified_source`, the scan ends at that source's first actual
    advertisement. BlueZ reports a stationary board sparsely (Stage16
    evidence: ~2 reports per 5 s, gaps up to 3.9 s), so a report early in a
    fixed window equal to the freshness limit is already stale at the gate.
    """
    import asyncio
    from bleak import BleakScanner  # type: ignore

    waiters = getattr(live_discovery, "_v2_continuous_waiters", None)
    if waiters is not None and verified_source is not None:
        # Reuse the running scan after GATT disconnect. Wait for an actual
        # matching advertisement; never extend freshness from a GATT read.
        event = asyncio.Event()
        waiter = (verified_source, event)
        waiters.append(waiter)
        try:
            await asyncio.wait_for(event.wait(), timeout_s)
        except asyncio.TimeoutError:
            pass
        finally:
            waiters.remove(waiter)
        return

    verified_seen = asyncio.Event()

    def callback(device, advertisement_data) -> None:
        if _record_v2_advertisement(live_discovery, device, advertisement_data,
                                    verified_source=verified_source):
            verified_seen.set()

    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    try:
        await asyncio.wait_for(verified_seen.wait(), timeout_s)
    except asyncio.TimeoutError:
        pass  # No refresh: the candidate stays stale and the gate fails closed.
    finally:
        await scanner.stop()


def default_connector():
    return connect_to_device
