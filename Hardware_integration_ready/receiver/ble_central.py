"""ble_central.py — the real BLE control lifecycle: connect, validate the
GATT layout, subscribe to DATA and STATUS, issue a CRC-protected
NEW_SESSION+START on every connect, detect disconnects, reconnect with
bounded exponential backoff, and send STOP on controlled shutdown.

This module is transport-agnostic: it depends only on the small
`BleClientLike` protocol below, never on `bleak` directly, so the full
connect/disconnect/backoff/session-isolation state machine is unit-testable
with a fake client (see tests/test_ble_lifecycle.py) without Bluetooth
hardware. `bleak_adapter.py` supplies the real client for hardware mode.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from ble_identity import BleIdentity, IdentityProtocolError, canonical_uid, decode_identity_value
from ble_uuids import CHAR_CONTROL_UUID, CHAR_DATA_UUID, CHAR_IDENTITY_UUID, CHAR_STATUS_UUID
from protocol_decoder import ControlCommand, ProtocolError, decode_status_packet, encode_control_packet

logger = logging.getLogger("veinnet.ble_central")


class BleConnectError(RuntimeError):
    """Raised for a failed connect / GATT-layout-validation attempt. Caught
    internally by run() to drive the backoff loop — never propagates out of
    run()."""


class BleClientLike(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def start_notify(self, char_uuid: str, callback) -> None: ...
    async def write_gatt_char(self, char_uuid: str, data: bytes, response: bool = True) -> None: ...
    async def read_gatt_char(self, char_uuid: str) -> bytes: ...
    def get_service_characteristic_uuids(self) -> set[str]: ...


# (device_name, disconnect_callback) -> connected BleClientLike, or raises
# BleConnectError / any Exception on failure to find/connect the device.
ClientConnector = Callable[[object, Callable[[], None]], Awaitable[BleClientLike]]


@dataclass
class BackoffPolicy:
    initial_s: float = 1.0
    max_s: float = 30.0
    factor: float = 2.0

    def sequence(self):
        delay = self.initial_s
        while True:
            yield delay
            delay = min(delay * self.factor, self.max_s)


class BleCentralController:
    """Owns exactly one verified BLE V2 connection lifecycle
    device at a time.

    `on_data`/`on_status` receive raw notification bytes (still undecoded —
    decoding/validation happens in protocol_decoder / window_assembler, kept
    out of this transport module by design). `on_disconnected` is awaited
    every time the connection is lost or a connect attempt fails, so the
    caller can emit a DEVICE_DISCONNECTED dashboard result.

    NEW_SESSION + START is (re)issued on every successful connect, so no
    reconnect can silently continue a stale session — this is enforced here
    AND independently by window_assembler.SessionRingBuffer, which refuses
    to merge samples carrying a different sensor_session_id.
    """

    def __init__(
        self,
        *,
        device_handle: object,
        expected_identity: BleIdentity,
        connector: ClientConnector,
        on_data: Callable[[bytes], Awaitable[None]],
        on_status: Callable[[bytes], Awaitable[None]] | None = None,
        on_disconnected: Callable[[], Awaitable[None]] | None = None,
        on_connection_status_change: Callable[[str], Awaitable[None]] | None = None,
        backoff: BackoffPolicy | None = None,
        auto_start: bool = True,
    ) -> None:
        self.device_handle = device_handle
        self.expected_identity = expected_identity
        self._connector = connector
        self._on_data = on_data
        self._on_status = on_status
        self._on_disconnected = on_disconnected
        self._on_connection_status_change = on_connection_status_change
        self._backoff = backoff or BackoffPolicy()
        self.auto_start = auto_start
        self.client: BleClientLike | None = None
        self._disconnect_event = asyncio.Event()
        self._available_event = asyncio.Event()
        self.connect_attempts = 0
        self.successful_connections = 0
        self.disconnect_callbacks = 0
        self.data_notifications = 0
        self.status_notifications = 0
        self.max_data_notification_bytes = 0
        self.new_session_commands = 0
        self.start_commands = 0
        self.missing_characteristic_errors = 0
        self.identity_mismatch_errors = 0
        self.subscriptions = 0
        self.last_connect_latency_s: float | None = None
        self.link_events: deque[tuple[float, str]] = deque(maxlen=128)
        self.retry_delays_s: deque[float] = deque(maxlen=128)

    def _handle_disconnect(self) -> None:
        self.disconnect_callbacks += 1
        self.link_events.append((time.monotonic(), "DISCONNECT_CALLBACK"))
        self._disconnect_event.set()

    async def _connect_once(self) -> BleClientLike:
        self.connect_attempts += 1
        started = time.monotonic()
        self.link_events.append((started, "CONNECT_ATTEMPT"))
        client = await self._connector(self.device_handle, self._handle_disconnect)
        await client.connect()
        try:
            available = {u.lower() for u in client.get_service_characteristic_uuids()}
            required = {CHAR_DATA_UUID, CHAR_STATUS_UUID, CHAR_CONTROL_UUID, CHAR_IDENTITY_UUID}
            missing = {u for u in required if u.lower() not in available}
            if missing:
                self.missing_characteristic_errors += 1
                raise BleConnectError(f"device does not expose expected characteristics: {sorted(missing)}")

            connected_identity = decode_identity_value(await client.read_gatt_char(CHAR_IDENTITY_UUID))
            if (
                canonical_uid(connected_identity.hardware_uid) != canonical_uid(self.expected_identity.hardware_uid)
                or connected_identity.short_code != self.expected_identity.short_code
            ):
                self.identity_mismatch_errors += 1
                raise BleConnectError(
                    "advertised/inventory/GATT identity mismatch: "
                    f"expected {self.expected_identity.hardware_uid}/{self.expected_identity.short_code}, "
                    f"observed {connected_identity.hardware_uid}/{connected_identity.short_code}"
                )

            async def _on_data_notify(_sender, raw) -> None:
                self.data_notifications += 1
                self.max_data_notification_bytes = max(self.max_data_notification_bytes, len(raw))
                await self._on_data(bytes(raw))

            async def _on_status_notify(_sender, raw) -> None:
                self.status_notifications += 1
                try:
                    decode_status_packet(bytes(raw))
                except ProtocolError:
                    return
                if self._on_status is not None:
                    await self._on_status(bytes(raw))

            await client.start_notify(CHAR_DATA_UUID, _on_data_notify)
            self.subscriptions += 1
            await client.start_notify(CHAR_STATUS_UUID, _on_status_notify)
            self.subscriptions += 1
            await client.write_gatt_char(
                CHAR_CONTROL_UUID, encode_control_packet(command=int(ControlCommand.NEW_SESSION))
            )
            self.new_session_commands += 1
            if self.auto_start:
                await client.write_gatt_char(
                    CHAR_CONTROL_UUID, encode_control_packet(command=int(ControlCommand.START))
                )
                self.start_commands += 1
            self.successful_connections += 1
            self.last_connect_latency_s = time.monotonic() - started
            self.link_events.append((time.monotonic(), "CONNECTED"))
            return client
        except Exception as exc:
            try:
                await client.disconnect()
            finally:
                if isinstance(exc, BleConnectError):
                    raise
                raise BleConnectError("connected V2 setup/verification failed") from exc

    async def run(self, stop_event: asyncio.Event) -> None:
        """Runs until stop_event is set. On each disconnect (including a
        failed connect attempt), calls on_disconnected() and retries with
        bounded exponential backoff. On controlled shutdown, sends STOP and
        disconnects cleanly before returning."""
        backoff_iter = self._backoff.sequence()

        while not stop_event.is_set():
            self._disconnect_event = asyncio.Event()
            self._available_event.clear()
            try:
                self.client = await self._connect_once()
                backoff_iter = self._backoff.sequence()  # reset after a clean connect
                if self._on_connection_status_change is not None:
                    await self._on_connection_status_change("CONNECTED")
            except Exception as exc:
                logger.warning("BLE connect failed: %s", exc)
                self.client = None
                if self._on_disconnected is not None:
                    await self._on_disconnected()
                if self._on_connection_status_change is not None:
                    await self._on_connection_status_change("RECONNECTING")
                delay = next(backoff_iter)
                self.retry_delays_s.append(delay)
                self.link_events.append((time.monotonic(), "RETRY_WAIT"))
                await self._sleep_or_stop(delay, stop_event)
                continue

            wait_stop = asyncio.ensure_future(stop_event.wait())
            wait_disconnect = asyncio.ensure_future(self._disconnect_event.wait())
            try:
                await asyncio.wait({wait_stop, wait_disconnect}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (wait_stop, wait_disconnect):
                    if not task.done():
                        task.cancel()

            if stop_event.is_set():
                client = self.client
                if client is not None:
                    try:
                        await asyncio.wait_for(client.write_gatt_char(
                            CHAR_CONTROL_UUID, encode_control_packet(command=int(ControlCommand.STOP))
                        ), timeout=1.5)
                    except Exception:
                        pass  # best-effort STOP; still disconnect below regardless
                    await asyncio.wait_for(client.disconnect(), timeout=3)
                if self._on_connection_status_change is not None:
                    await self._on_connection_status_change("DISCONNECTED")
                return

            # Disconnected unexpectedly (not a controlled shutdown).
            self.client = None
            if self._on_disconnected is not None:
                await self._on_disconnected()
            if self._on_connection_status_change is not None:
                await self._on_connection_status_change("RECONNECTING")
            delay = next(backoff_iter)
            self.retry_delays_s.append(delay)
            self.link_events.append((time.monotonic(), "RETRY_WAIT"))
            await self._sleep_or_stop(delay, stop_event)

    def device_available(self, device_handle) -> None:
        """Wake an existing assigned path on a new unique UID advertisement.

        The next connection still reads and checks GATT UID/code. This does
        not create a patient assignment or start acquisition.
        """
        self.device_handle = device_handle
        self._available_event.set()

    async def _sleep_or_stop(self, delay: float, stop_event: asyncio.Event) -> None:
        waits = [asyncio.create_task(stop_event.wait()),
                 asyncio.create_task(self._available_event.wait())]
        try:
            await asyncio.wait(waits, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
