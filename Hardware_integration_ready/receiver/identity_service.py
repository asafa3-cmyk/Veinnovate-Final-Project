"""identity_service.py — facade wiring the four separate identity/assignment
state machines together (AGENTS.md items 3, 4, 6) plus the no-scanner
one-adapter pairing window (AGENTS.md: "select/enter the patient... press
Add adapter... switch exactly one adapter from OFF to ON... Bind only when
exactly one previously unseen authorized identity appears... If zero or
multiple new adapters appear, do not guess... ask the nurse to select or
enter the printed short code.").

This is the one object `identity_api.py` (and, in a future physical build,
a real BLE scan callback) talks to. It never guesses an identity: every
method either returns a definite result or raises/returns an explicit
reason code — see `assignment_gate.AssignmentRejected`.
"""

from __future__ import annotations

import time
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from adapter_inventory import AdapterInventory, InventoryError, InventoryStatus
from assignment_gate import (
    AssignmentRejected,
    GateContext,
    IdentityVerifier,
    RejectReason,
    assign as gate_assign,
    preflight as gate_preflight,
)
from ble_connection_state import BleConnectionRegistry
from live_ble_discovery import LiveBleDiscovery
from ble_identity import BleIdentity, canonical_uid
from measurement_session import MeasurementSessionError, MeasurementSessionStore
from patient_assignment import AssignmentError, AssignmentRecord, PatientAssignmentStore
from patient_profile import PatientProfile


DEFAULT_PAIRING_WINDOW_S = 30.0


class PairingWindowState:
    OPEN = "OPEN"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"


class CandidateStatus:
    READY = "READY"                  # provisioned, unconsumed, unassigned — safe to offer for confirm
    UNPROVISIONED = "UNPROVISIONED"  # advertising but no inventory record — never auto-bindable
    CONSUMED = "CONSUMED"
    ALREADY_ASSIGNED = "ALREADY_ASSIGNED"
    PENDING_PROVISIONING = "PENDING_PROVISIONING"
    RETIRED = "RETIRED"


@dataclass
class PairingWindow:
    window_id: str
    patient_id: str
    opened_at_utc: float
    expires_at_utc: float
    baseline_uids: set[str] = field(default_factory=set)
    state: str = PairingWindowState.OPEN
    # In memory only until assign() commits it with the binding.
    profile: PatientProfile | None = None


class IdentityService:
    def __init__(
        self,
        *,
        inventory: AdapterInventory,
        live_discovery: LiveBleDiscovery,
        connection_registry: BleConnectionRegistry,
        assignment_store: PatientAssignmentStore,
        session_store: MeasurementSessionStore,
        pairing_window_s: float = DEFAULT_PAIRING_WINDOW_S,
        identity_verifier: IdentityVerifier | None = None,
        calibration_invalidator: Callable[[object], None] | None = None,
        connected_identity_verifier: Callable[[object, object], Awaitable[BleIdentity]] | None = None,
        session_started_handler: Callable[[object, BleIdentity, dict], Awaitable[None]] | None = None,
        session_ended_handler: Callable[[object], Awaitable[None]] | None = None,
    ) -> None:
        self.inventory = inventory
        self.live_discovery = live_discovery
        self.connection_registry = connection_registry
        self.assignment_store = assignment_store
        self.session_store = session_store
        self.pairing_window_s = pairing_window_s
        self.identity_verifier = identity_verifier
        self.calibration_invalidator = calibration_invalidator
        self.connected_identity_verifier = connected_identity_verifier
        self.session_started_handler = session_started_handler
        self.session_ended_handler = session_ended_handler
        self._windows: dict[str, PairingWindow] = {}
        self._window_lock = threading.RLock()
        # Safe, in-memory diagnostics: never request/profile values.
        self.assignment_diagnostic: dict = {}
        self.discovery_detected_at: dict = {}
        self.ui_delivery: dict = {}

    @classmethod
    def from_db_dir(cls, db_dir: Path | str, **kwargs) -> "IdentityService":
        """Convenience constructor for production wiring: three separate
        SQLite files under one directory, one per persistent store (the
        live-discovery/connection registries stay in-memory by design —
        see their own module docstrings)."""
        db_dir = Path(db_dir)
        return cls(
            inventory=AdapterInventory(db_dir / "adapter_inventory.sqlite3"),
            live_discovery=LiveBleDiscovery(),
            connection_registry=BleConnectionRegistry(),
            assignment_store=PatientAssignmentStore(db_dir / "patient_assignments.sqlite3"),
            session_store=MeasurementSessionStore(db_dir / "measurement_sessions.sqlite3"),
            **kwargs,
        )

    def _ctx(self) -> GateContext:
        return GateContext(
            inventory=self.inventory,
            live_discovery=self.live_discovery,
            connection_registry=self.connection_registry,
            assignment_store=self.assignment_store,
        )

    # --- Inventory / live visibility -----------------------------------

    def list_inventory(self) -> list[dict]:
        return [r.to_dict() for r in self.inventory.list_all()]

    def list_live_waiting(self, *, now: float | None = None) -> list[dict]:
        """Every currently-fresh advertisement, resolved against inventory
        (never patient data) — "waiting for pairing" per AGENTS.md's OFF->ON
        discovery-event language."""
        ts = now if now is not None else time.time()
        out = []
        for entry in self.live_discovery.all_fresh(now=ts):
            record = self.inventory.get_by_uid(entry.hardware_uid)
            if record is None:
                out.append({"hardware_uid": entry.hardware_uid, "short_code": None, "status": CandidateStatus.UNPROVISIONED})
                continue
            status = self._candidate_status(record)
            out.append({"hardware_uid": None, "short_code": record.short_code, "status": status,
                        "detected_at_utc": self.discovery_detected_at.get(record.hardware_uid)})
        return out

    def _candidate_status(self, record) -> str:
        if record.consumed:
            return CandidateStatus.CONSUMED
        if (
            record.inventory_status == InventoryStatus.PENDING_PROVISIONING
            or record.identity_verified_at_utc is None
        ):
            return CandidateStatus.PENDING_PROVISIONING
        if record.inventory_status != InventoryStatus.PROVISIONED:
            return CandidateStatus.RETIRED
        if self.assignment_store.get_active_by_uid(record.hardware_uid) is not None:
            return CandidateStatus.ALREADY_ASSIGNED
        return CandidateStatus.READY

    # --- Pairing window ---------------------------------------------------

    def start_pairing_for_profile(self, *, profile: PatientProfile) -> PairingWindow:
        try:
            patient_id = self.assignment_store.resolve_patient_id(profile)
        except AssignmentError as exc:
            reason = "PATIENT_RECORD_CONFLICT" if str(exc) == "PATIENT_RECORD_CONFLICT" else "PATIENT_RECORD_UNAVAILABLE"
            raise AssignmentRejected(reason, "internal patient record could not be resolved") from exc
        return self.start_pairing(patient_id=patient_id, profile=profile)

    def start_pairing(
        self, *, patient_id: str, now: float | None = None, profile: PatientProfile | None = None
    ) -> PairingWindow:
        """`profile` is required by the HTTP API (identity contract 1.1.0);
        it stays optional here only for the deterministic simulator and
        pre-1.1.0 unit tests, whose bindings then carry no demographics and
        can never be calibrated under multi-session runtime (fail-closed)."""
        if not patient_id or not str(patient_id).strip():
            raise ValueError("patient_id is required to start a pairing window")
        ts = now if now is not None else time.time()
        with self._window_lock:
            window = PairingWindow(
                window_id=str(uuid.uuid4()),
                patient_id=patient_id.strip(),
                opened_at_utc=ts,
                expires_at_utc=ts + self.pairing_window_s,
                baseline_uids=self.live_discovery.known_uids(),
                profile=profile,
            )
            self._windows[window.window_id] = window
            return window

    def cancel_pairing(self, window_id: str, *, now: float | None = None) -> PairingWindow:
        with self._window_lock:
            window = self._require_window(window_id)
            if window.state == PairingWindowState.OPEN:
                window.state = PairingWindowState.CANCELLED
            return window

    def _require_window(self, window_id: str) -> PairingWindow:
        window = self._windows.get(window_id)
        if window is None:
            raise KeyError(f"no pairing window {window_id!r}")
        return window

    def pairing_status(self, window_id: str, *, now: float | None = None) -> dict:
        ts = now if now is not None else time.time()
        with self._window_lock:
            window = self._require_window(window_id)
            if window.state == PairingWindowState.OPEN and ts >= window.expires_at_utc:
                window.state = PairingWindowState.EXPIRED

            new_candidates = []
            if window.state == PairingWindowState.OPEN:
                for entry in self.live_discovery.all_fresh(now=ts):
                    if entry.hardware_uid in window.baseline_uids:
                        continue  # not newly discovered during this window
                    record = self.inventory.get_by_uid(entry.hardware_uid)
                    if record is None:
                        new_candidates.append(
                            {"short_code": None, "status": CandidateStatus.UNPROVISIONED}
                        )
                    else:
                        new_candidates.append(
                            {"short_code": record.short_code, "status": self._candidate_status(record)}
                        )

            # Exactly one newly-seen radio must exist, and it must be READY.
            # One READY radio plus any other new radio is still ambiguous.
            auto_code = None
            if len(new_candidates) == 1 and new_candidates[0]["status"] == CandidateStatus.READY:
                auto_code = new_candidates[0]["short_code"]
            return {
                "window_id": window.window_id,
                "patient_id": window.patient_id,
                "state": window.state,
                "expires_at_utc": window.expires_at_utc,
                "new_candidates": new_candidates,
                "auto_bindable_short_code": auto_code,
            }

    # --- Assignment / session ------------------------------------------

    def assign(
        self,
        *,
        window_id: str,
        short_code: str,
        now: float | None = None,
        connect_and_verify: IdentityVerifier | None = None,
    ) -> dict:
        """Runs the fail-closed gate, then immediately starts the
        measurement session under the new assignment (POC simplification:
        one session per assignment, started together — see
        measurement_session.py docstring)."""
        ts = now if now is not None else time.time()
        with self._window_lock:
            window = self._require_window(window_id)
            if window.state == PairingWindowState.OPEN and ts >= window.expires_at_utc:
                window.state = PairingWindowState.EXPIRED
            if window.state != PairingWindowState.OPEN:
                raise AssignmentRejected(
                    RejectReason.PAIRING_WINDOW_NOT_OPEN,
                    f"pairing window {window_id!r} is {window.state.lower()}",
                )

            record = self.inventory.get_by_code(short_code)
            if record is not None and self.session_store.get_active_by_uid(record.hardware_uid) is not None:
                raise AssignmentRejected(
                    RejectReason.SESSION_ALREADY_ACTIVE,
                    f"an active measurement session already exists for {short_code!r}",
                )

            assignment: AssignmentRecord = gate_assign(
                self._ctx(),
                short_code=short_code,
                patient_id=window.patient_id,
                now=ts,
                connect_and_verify=connect_and_verify or self.identity_verifier,
                profile=window.profile,
            )
            try:
                session = self.session_store.start(
                    assignment_id=assignment.assignment_id,
                    hardware_uid=assignment.hardware_uid,
                    patient_id=window.patient_id,
                    now=ts,
                )
            except MeasurementSessionError as exc:
                # The stores are deliberately separate, so compensate the
                # already-committed assignment if session creation loses a race.
                self.assignment_store.end(assignment.assignment_id, now=ts)
                self.connection_registry.disconnected(assignment.hardware_uid, now=ts)
                raise AssignmentRejected(
                    RejectReason.SESSION_ALREADY_ACTIVE,
                    "measurement session could not be created without a conflicting active session",
                ) from exc
            window.state = PairingWindowState.COMPLETED
            return {"assignment": assignment.to_dict(), "session": session.to_dict()}

    async def assign_v2(self, *, window_id: str, short_code: str, now: float | None = None) -> dict:
        """Production V2 gate: verify the exact advertised BLE handle first,
        then re-run all mutable preconditions immediately before commit."""
        ts = now if now is not None else time.time()
        self.assignment_diagnostic = {"phase": "preflight", "updated_at_utc": ts}
        with self._window_lock:
            window = self._require_window(window_id)
            if window.state == PairingWindowState.OPEN and ts >= window.expires_at_utc:
                window.state = PairingWindowState.EXPIRED
            if window.state != PairingWindowState.OPEN:
                raise AssignmentRejected(
                    RejectReason.PAIRING_WINDOW_NOT_OPEN,
                    f"pairing window {window_id!r} is {window.state.lower()}",
                )
        record, candidate = gate_preflight(self._ctx(), short_code=short_code, now=ts)
        if self.connected_identity_verifier is None:
            raise AssignmentRejected(
                RejectReason.IDENTITY_VERIFICATION_UNAVAILABLE,
                "BLE Protocol V2 connected identity verification is unavailable",
            )
        try:
            self.assignment_diagnostic["phase"] = "connected_identity"
            connected = await self.connected_identity_verifier(candidate.connection_handle, record)
        except AssignmentRejected:
            raise
        except Exception as exc:
            self.assignment_diagnostic["exception_type"] = type(exc).__name__
            dbus_error = getattr(exc, "dbus_error", None)
            if isinstance(dbus_error, str) and dbus_error.startswith("org.bluez.") and all(c.isalnum() or c in "._" for c in dbus_error):
                self.assignment_diagnostic["dbus_error"] = dbus_error
            raise AssignmentRejected(
                RejectReason.IDENTITY_VERIFICATION_FAILED,
                "connected-device identity could not be verified",
            ) from exc
        if (
            canonical_uid(connected.hardware_uid) != canonical_uid(record.hardware_uid)
            or connected.short_code != record.short_code
        ):
            raise AssignmentRejected(
                RejectReason.UID_CODE_MISMATCH,
                "advertised, inventory, and connected GATT identities do not match",
            )

        # assign() repeats authorization, consumed/assigned, freshness and
        # duplicate checks after the await, closing the scan/connect TOCTOU.
        post_verify_ts = time.time() if now is None else now
        self.assignment_diagnostic["phase"] = "post_verify_preflight"
        _current_record, current_candidate = gate_preflight(
            self._ctx(), short_code=short_code, now=post_verify_ts
        )
        if (
            current_candidate.source_token != candidate.source_token
            or current_candidate.connection_handle is not candidate.connection_handle
        ):
            raise AssignmentRejected(
                RejectReason.IDENTITY_VERIFICATION_FAILED,
                "the verified BLE source changed before assignment commit",
            )
        result = self.assign(
            window_id=window_id,
            short_code=short_code,
            now=post_verify_ts,
            connect_and_verify=lambda _expected_uid: connected.hardware_uid,
        )
        if self.session_started_handler is not None:
            try:
                self.assignment_diagnostic["phase"] = "session_start"
                await self.session_started_handler(candidate.connection_handle, connected, result)
            except Exception as exc:
                # A committed binding must never masquerade as an active
                # measurement if its isolated BLE path cannot start.
                self.session_store.end(result["session"]["session_id"], now=post_verify_ts)
                self.assignment_store.end(result["assignment"]["assignment_id"], now=post_verify_ts)
                self.connection_registry.disconnected(record.hardware_uid, now=post_verify_ts)
                raise AssignmentRejected(
                    RejectReason.IDENTITY_VERIFICATION_FAILED,
                    "verified BLE path could not be started",
                ) from exc
        self.assignment_diagnostic = {"phase": "complete", "updated_at_utc": time.time()}
        return result

    # --- Active sessions / pairing profile -------------------------------

    def list_active_sessions(self) -> list[dict]:
        """Every active session with its assignment (including the pairing
        profile), so the dashboard can rebuild its tiles after a reload."""
        out = []
        for assignment in self.assignment_store.list_active():
            session = self.session_store.get_active_by_uid(assignment.hardware_uid)
            if session is None or session.assignment_id != assignment.assignment_id:
                continue  # assignment without its own live session is not monitorable
            out.append({"session": session.to_dict(), "assignment": assignment.to_dict()})
        return out

    def profile_for_session(self, session_id: str) -> PatientProfile | None:
        """The pairing-time profile bound to an *active* session, or None."""
        session = self.session_store.get(session_id)
        if session is None or not session.active:
            return None
        assignment = self.assignment_store.get(session.assignment_id)
        if assignment is None or not assignment.active:
            return None
        return assignment.profile

    def end_session(self, session_id: str, *, now: float | None = None) -> dict:
        """Ends the measurement session, ends its assignment, marks the
        adapter consumed, and drops its BLE connection state — AGENTS.md:
        "close the session, invalidate calibration and patient binding,
        mark the adapter identity consumed, switch it OFF"."""
        ts = now if now is not None else time.time()
        session = self.session_store.get(session_id)
        if session is None:
            raise KeyError(f"no measurement session {session_id!r}")

        assignment = self.assignment_store.get(session.assignment_id)
        record = self.inventory.get_by_uid(session.hardware_uid)
        if (
            assignment is None
            or record is None
            or assignment.hardware_uid != session.hardware_uid
            or assignment.patient_id != session.patient_id
            or assignment.short_code != record.short_code
        ):
            raise AssignmentRejected(
                RejectReason.END_SESSION_STATE_MISMATCH,
                "session, assignment, and inventory identity records do not match",
            )

        if session.active:
            if self.calibration_invalidator is None:
                raise AssignmentRejected(
                    RejectReason.CALIBRATION_INVALIDATION_UNAVAILABLE,
                    "session end is blocked because calibration invalidation is not integrated",
                )
            try:
                self.calibration_invalidator(session)
            except Exception as exc:
                raise AssignmentRejected(
                    RejectReason.CALIBRATION_INVALIDATION_FAILED,
                    "session end is blocked because calibration invalidation failed",
                ) from exc
            # Cross-file SQLite transactions cannot be atomic. Apply the
            # irreversible safety guards first: invalidate calibration, then
            # consume the adapter. Only then release assignment/session rows.
            # Any interrupted sequence therefore fails closed against reuse.
            try:
                if session.active:
                    record = self.inventory.finish_use(record.short_code, session_ref=session_id, now=ts)
                if assignment.active:
                    assignment = self.assignment_store.end(assignment.assignment_id, now=ts)
                session = self.session_store.end(session_id, now=ts)
            except (InventoryError, AssignmentError, MeasurementSessionError) as exc:
                raise AssignmentRejected(
                    RejectReason.END_SESSION_STORAGE_FAILED,
                    "session end could not complete; the adapter remains blocked from reuse",
                ) from exc

        self.connection_registry.disconnected(session.hardware_uid, now=ts)
        self.live_discovery.on_power_off(session.hardware_uid)

        # Stop/remove only this UID's acquisition path. Storage safety state
        # above is already committed even if transport shutdown reports an error.
        if self.session_ended_handler is not None:
            # end_session is intentionally synchronous for legacy simulator
            # callers; async production callers use end_session_v2 below.
            pass

        return {
            "session": session.to_dict(),
            "assignment": assignment.to_dict(),
            "adapter": record.to_dict(),
        }

    async def end_session_v2(self, session_id: str, *, now: float | None = None) -> dict:
        result = self.end_session(session_id, now=now)
        if self.session_ended_handler is not None:
            await self.session_ended_handler(result["session"])
        return result

    def authorize_prototype_reuse(self, short_code: str, hardware_uid: str, *, authorization_ref: str, recover: bool = False, now: float | None = None):
        """Explicit maintenance operation, never an automatic pairing fallback."""
        if (self.assignment_store.get_active_by_uid(hardware_uid) is not None
            or self.session_store.get_active_by_uid(hardware_uid) is not None):
            raise InventoryError("prototype reuse blocked by active assignment/session")
        return self.inventory.authorize_prototype_reuse(short_code, hardware_uid, authorization_ref=authorization_ref, recover=recover, now=now)
