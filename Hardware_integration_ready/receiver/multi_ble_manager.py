"""Per-UID BLE controller lifecycle manager for Protocol V2.

Each active identity owns its controller, stop event, asyncio task, and
callbacks. No patient, session, packet buffer, result, or reconnect state is
shared between entries; those live in the per-device EdgeOrchestrator passed
by the caller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ble_central import BleCentralController, ClientConnector
from ble_identity import BleIdentity, canonical_uid


class MultiBleError(RuntimeError):
    pass


@dataclass
class ActiveBlePath:
    identity: BleIdentity
    controller: BleCentralController
    stop_event: asyncio.Event
    task: asyncio.Task
    measurement: object | None = None
    measurement_task: asyncio.Task | None = None


class MultiBleManager:
    def __init__(self, connector: ClientConnector) -> None:
        self._connector = connector
        self._active: dict[str, ActiveBlePath] = {}
        self._lock = asyncio.Lock()

    async def start(self, *, identity: BleIdentity, device_handle: object, orchestrator, measurement=None) -> ActiveBlePath:
        uid = canonical_uid(identity.hardware_uid)
        async with self._lock:
            if uid in self._active:
                raise MultiBleError(f"BLE path already active for {uid}")
            stop_event = asyncio.Event()
            controller = BleCentralController(
                device_handle=device_handle,
                expected_identity=identity,
                connector=self._connector,
                on_data=measurement.on_data if measurement else orchestrator.handle_raw_packet,
                on_status=measurement.on_status if measurement else orchestrator.handle_status_packet,
                on_disconnected=measurement.on_disconnected if measurement else orchestrator.handle_disconnected,
                on_connection_status_change=orchestrator.set_connection_status,
                auto_start=measurement is None,
            )
            task = asyncio.create_task(controller.run(stop_event), name=f"ble-{uid}")
            measurement_task = None
            if measurement is not None:
                measurement.controller = controller
                measurement_task = asyncio.create_task(measurement.run(stop_event), name=f"measurement-{uid}")
            path = ActiveBlePath(identity, controller, stop_event, task, measurement, measurement_task)
            self._active[uid] = path
            return path

    async def stop(self, hardware_uid: str) -> None:
        uid = canonical_uid(hardware_uid)
        async with self._lock:
            path = self._active.pop(uid, None)
        if path is None:
            return
        path.stop_event.set()
        if path.measurement_task is not None:
            path.measurement_task.cancel()
            await asyncio.gather(path.measurement_task, return_exceptions=True)
        await path.task

    def get(self, hardware_uid: str) -> ActiveBlePath | None:
        return self._active.get(canonical_uid(hardware_uid))

    def active_uids(self) -> set[str]:
        return set(self._active)
