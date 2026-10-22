"""assignment_gate.py — the fail-closed pre-assignment identity gate
(AGENTS.md: "Enforce a fail-closed pre-assignment identity gate...").

`assign()` is the ONLY supported path from "nurse selected/confirmed
VN-NNN" to a committed `patient_assignment.AssignmentRecord`. It composes
the four separate state machines (AGENTS.md: keep them separate) without
merging their storage:

  1. `adapter_inventory.AdapterInventory`   — resolve VN-NNN -> hardware_uid
  2. `live_ble_discovery.LiveBleDiscovery`  — require one fresh live candidate
  3. `ble_connection_state.BleConnectionRegistry` — connect + re-verify identity
  4. `patient_assignment.PatientAssignmentStore` — commit the binding atomically

Every rejection is a distinct, named reason code — never a generic
exception — so API/UI callers can show the nurse a specific, correct
message instead of a raw stack trace (AGENTS.md: "no raw exception text...
as the primary instruction"). Rejection order matches AGENTS.md's own
list: "missing inventory record, UID/code mismatch, duplicate identity,
multiple matches, or concurrent assignment change".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from adapter_inventory import AdapterInventory
from adapter_inventory import InventoryStatus
from ble_connection_state import BleConnectionRegistry
from live_ble_discovery import DiscoveredAdapter, LiveBleDiscovery
from patient_assignment import AssignmentConflictError, AssignmentError, AssignmentRecord, PatientAssignmentStore
from patient_profile import PatientProfile


class AssignmentRejected(Exception):
    """A named, fail-closed rejection. `reason_code` is stable API surface
    — never change its spelling without a version bump, the dashboard
    matches on it to choose the nurse-facing Hebrew message."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code
        self.message = message


class RejectReason:
    CODE_NOT_FOUND = "CODE_NOT_FOUND"
    ADAPTER_CONSUMED = "ADAPTER_CONSUMED"
    ADAPTER_PENDING_PROVISIONING = "ADAPTER_PENDING_PROVISIONING"
    ADAPTER_RETIRED = "ADAPTER_RETIRED"
    ADAPTER_ALREADY_ASSIGNED = "ADAPTER_ALREADY_ASSIGNED"
    MISSING_LIVE_CANDIDATE = "MISSING_LIVE_CANDIDATE"
    STALE_LIVE_CANDIDATE = "STALE_LIVE_CANDIDATE"
    DUPLICATE_LIVE_IDENTITY = "DUPLICATE_LIVE_IDENTITY"
    UID_CODE_MISMATCH = "UID_CODE_MISMATCH"
    IDENTITY_VERIFICATION_UNAVAILABLE = "IDENTITY_VERIFICATION_UNAVAILABLE"
    IDENTITY_VERIFICATION_FAILED = "IDENTITY_VERIFICATION_FAILED"
    CONCURRENT_ASSIGNMENT_CONFLICT = "CONCURRENT_ASSIGNMENT_CONFLICT"
    ASSIGNMENT_STORAGE_UNAVAILABLE = "ASSIGNMENT_STORAGE_UNAVAILABLE"
    PAIRING_WINDOW_NOT_OPEN = "PAIRING_WINDOW_NOT_OPEN"
    SESSION_ALREADY_ACTIVE = "SESSION_ALREADY_ACTIVE"
    CALIBRATION_INVALIDATION_UNAVAILABLE = "CALIBRATION_INVALIDATION_UNAVAILABLE"
    CALIBRATION_INVALIDATION_FAILED = "CALIBRATION_INVALIDATION_FAILED"
    END_SESSION_STATE_MISMATCH = "END_SESSION_STATE_MISMATCH"
    END_SESSION_STORAGE_FAILED = "END_SESSION_STORAGE_FAILED"
    # Identity contract 1.1.0 (2026-09-29): pairing-time profile validation
    # (codes shared with patient_profile.ProfileReason) and calibration's
    # single-source-of-truth check against the pairing record.
    PROFILE_FIELD_MISSING = "PROFILE_FIELD_MISSING"
    PROFILE_FIELD_INVALID = "PROFILE_FIELD_INVALID"
    NATIONAL_ID_INVALID = "NATIONAL_ID_INVALID"
    AGE_OUT_OF_RANGE = "AGE_OUT_OF_RANGE"
    SEX_INVALID = "SEX_INVALID"
    PAIRING_PROFILE_UNAVAILABLE = "PAIRING_PROFILE_UNAVAILABLE"
    DEMOGRAPHICS_MISMATCH = "DEMOGRAPHICS_MISMATCH"


# hardware_uid (as resolved from inventory) -> hardware_uid re-read from the
# connected device. There is deliberately no pass-through default: a future
# real implementation reads this from a GATT identity characteristic once
# the BLE protocol carries one (see docs/BLE_PROTOCOL_V2_IDENTITY.md) —
# tests inject a mismatching verifier to exercise UID_CODE_MISMATCH.
IdentityVerifier = Callable[[str], str]


@dataclass(frozen=True)
class GateContext:
    """Bundles the four collaborating stores so callers (API/simulator/
    tests) don't have to thread five positional arguments through every
    call."""

    inventory: AdapterInventory
    live_discovery: LiveBleDiscovery
    connection_registry: BleConnectionRegistry
    assignment_store: PatientAssignmentStore


def preflight(
    ctx: GateContext, *, short_code: str, now: float | None = None
) -> tuple[object, DiscoveredAdapter]:
    """Read-only assignment eligibility checks in stable rejection order."""
    ts = now if now is not None else time.time()
    record = ctx.inventory.get_by_code(short_code)
    if record is None:
        raise AssignmentRejected(RejectReason.CODE_NOT_FOUND, f"no registered adapter for {short_code!r}")
    if record.consumed:
        raise AssignmentRejected(RejectReason.ADAPTER_CONSUMED, f"{short_code!r} was already consumed at end-of-use")
    if (
        record.inventory_status == InventoryStatus.PENDING_PROVISIONING
        or record.identity_verified_at_utc is None
    ):
        raise AssignmentRejected(
            RejectReason.ADAPTER_PENDING_PROVISIONING,
            f"{short_code!r} has not completed production-firmware identity verification",
        )
    if record.inventory_status != InventoryStatus.PROVISIONED:
        raise AssignmentRejected(RejectReason.ADAPTER_RETIRED, f"{short_code!r} is not authorized for assignment")
    if ctx.assignment_store.get_active_by_uid(record.hardware_uid) is not None:
        raise AssignmentRejected(
            RejectReason.ADAPTER_ALREADY_ASSIGNED,
            f"{short_code!r} is already bound to an active patient assignment",
        )
    candidates = ctx.live_discovery.fresh_candidates(record.hardware_uid, now=ts)
    if not candidates:
        known = ctx.live_discovery.get(record.hardware_uid, now=ts)
        reason = RejectReason.MISSING_LIVE_CANDIDATE if known is None else RejectReason.STALE_LIVE_CANDIDATE
        raise AssignmentRejected(reason, f"no single fresh live device is advertising {short_code!r}")
    if len(candidates) > 1:
        raise AssignmentRejected(
            RejectReason.DUPLICATE_LIVE_IDENTITY,
            f"{short_code!r}'s identity is advertised by {len(candidates)} sources",
        )
    return record, candidates[0]


def assign(
    ctx: GateContext,
    *,
    short_code: str,
    patient_id: str,
    now: float | None = None,
    connect_and_verify: IdentityVerifier | None = None,
    profile: PatientProfile | None = None,
) -> AssignmentRecord:
    ts = now if now is not None else time.time()
    record, _candidate = preflight(ctx, short_code=short_code, now=ts)

    if connect_and_verify is None:
        raise AssignmentRejected(
            RejectReason.IDENTITY_VERIFICATION_UNAVAILABLE,
            "connected-device identity verification is unavailable for the active BLE protocol",
        )

    # Exactly one fresh candidate: connect and re-verify identity before
    # committing (AGENTS.md: "connect and re-read/verify the identity when
    # the protocol supports it").
    ctx.connection_registry.connecting(record.hardware_uid, now=ts)
    try:
        verified_uid = connect_and_verify(record.hardware_uid)
    except Exception as exc:
        ctx.connection_registry.disconnected(record.hardware_uid, now=ts)
        raise AssignmentRejected(
            RejectReason.IDENTITY_VERIFICATION_FAILED,
            "connected-device identity could not be verified",
        ) from exc
    if verified_uid != record.hardware_uid:
        ctx.connection_registry.disconnected(record.hardware_uid, now=ts)
        raise AssignmentRejected(
            RejectReason.UID_CODE_MISMATCH,
            f"device connected for {short_code!r} reported hardware_uid {verified_uid!r}, "
            f"expected {record.hardware_uid!r}",
        )
    ctx.connection_registry.connected(record.hardware_uid, now=ts)

    try:
        return ctx.assignment_store.commit(
            hardware_uid=record.hardware_uid, short_code=short_code, patient_id=patient_id, now=ts,
            profile=profile,
        )
    except AssignmentConflictError as exc:
        raise AssignmentRejected(RejectReason.CONCURRENT_ASSIGNMENT_CONFLICT, str(exc)) from exc
    except AssignmentError as exc:
        ctx.connection_registry.disconnected(record.hardware_uid, now=ts)
        raise AssignmentRejected(
            RejectReason.ASSIGNMENT_STORAGE_UNAVAILABLE,
            "patient assignment storage is unavailable",
        ) from exc
