"""Resume persisted active bindings after restart; never create an assignment."""
from ble_identity import canonical_uid


class ActiveRuntimeRestorer:
    def __init__(self, identity_service, runtime):
        self.service, self.runtime = identity_service, runtime
        self.expected = {
            canonical_uid(row["assignment"]["hardware_uid"]): row
            for row in identity_service.list_active_sessions()
        }
        self.attempted = set()

    def _unchanged(self, uid, expected):
        current = self.service.list_active_sessions()
        return any(row["assignment"]["assignment_id"] == expected["assignment"]["assignment_id"]
                   and row["session"]["session_id"] == expected["session"]["session_id"]
                   and canonical_uid(row["assignment"]["hardware_uid"]) == uid
                   and row["assignment"]["patient_id"] == expected["assignment"]["patient_id"]
                   for row in current)

    async def restore(self, uid, device_handle):
        uid = canonical_uid(uid)
        expected = self.expected.get(uid)
        if expected is None or uid in self.attempted:
            return
        # One electronic verification attempt per restart; no automatic retries.
        self.attempted.add(uid)
        service = self.service
        service.assignment_diagnostic = {"phase": "runtime_restore"}
        try:
            record = service.inventory.get_by_uid(uid)
            candidates = service.live_discovery.fresh_candidates(uid)
            if (record is None or record.inventory_status != "PROVISIONED" or record.consumed
                    or len(candidates) != 1 or candidates[0].connection_handle is not device_handle
                    or not self._unchanged(uid, expected)):
                raise RuntimeError("RESTORE_BINDING_OR_DISCOVERY_INVALID")
            identity = await service.connected_identity_verifier(device_handle, record)
            if (canonical_uid(identity.hardware_uid) != uid or identity.short_code != record.short_code
                    or len(service.live_discovery.fresh_candidates(uid)) != 1
                    or not self._unchanged(uid, expected)):
                raise RuntimeError("RESTORE_IDENTITY_OR_BINDING_CHANGED")
            await self.runtime.start_session(device_handle, identity, expected, restored=True)
            if not self._unchanged(uid, expected):
                await self.runtime.end_session(expected["session"]["session_id"])
                raise RuntimeError("RESTORE_BINDING_CHANGED")
            service.assignment_diagnostic = {"phase": "runtime_restored"}
        except Exception as exc:
            service.assignment_diagnostic = {"phase": "runtime_restore_failed", "exception_type": type(exc).__name__}
