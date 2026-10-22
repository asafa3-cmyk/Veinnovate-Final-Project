"""adapter_inventory.py — persistent, software-only POC inventory of
provisioned Jetson-side adapter identities (AGENTS.md "Provisioned adapter
inventory" state machine).

Scope and invariants (see AGENTS.md, "Constraints"):
  - One immutable `hardware_uid` (the XIAO/nRF52840 hardware UID, read once
    at provisioning time) plus one short human-readable `short_code`
    ("VN-NNN") per physical adapter. The short code is what nurses see and
    type/select; the hardware UID is never shown or used as a nurse-facing
    identity.
  - Provisioning is two-phase. `reserve_provisioning()` atomically assigns
    the next sequential code in `PENDING_PROVISIONING`; only
    `finalize_provisioning()` may make the record usable after production
    firmware reports the reserved UID and code.
  - Patient data never belongs here. This module has no patient_id,
    session_id, or clinical field anywhere in its schema — assignment and
    session state live in `patient_assignment.py`, a separate store.
  - `inventory_status` distinguishes reserved-but-unverified
    (PENDING_PROVISIONING), provisioned-and-usable (PROVISIONED), and taken
    out of service (RETIRED); `consumed`
    tracks single-use lifecycle independently (an adapter can be RETIRED
    without ever having been consumed, e.g. damaged before use).

Storage: a single SQLite file (stdlib `sqlite3`, no extra dependency). Not
using JSON+atomic-rename (as `calibration_store.py` does) because inventory
rows need read-modify-write updates (mark_consumed, retire) with a natural
key — SQLite gives us that with a UNIQUE constraint on `hardware_uid` for
free, which is exactly the fail-closed duplicate-provisioning guard we want.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


SHORT_CODE_PATTERN = re.compile(r"^VN-(\d{3,})$")
PROTOTYPE_REUSE_POLICY = "PROTOTYPE-DEVELOPMENT-REUSE-v1"
DEVELOPMENT_CODES = frozenset(("VN-001", "VN-002", "VN-003"))
_MIN_CODE_WIDTH = 3


class InventoryError(ValueError):
    """Raised for a provisioning/lookup precondition violation. Never
    silently ignored — every rejection here is a fail-closed guard."""


class InventoryStatus:
    PENDING_PROVISIONING = "PENDING_PROVISIONING"
    PROVISIONED = "PROVISIONED"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class AdapterRecord:
    hardware_uid: str
    short_code: str
    inventory_status: str
    consumed: bool
    provisioned_at_utc: float
    identity_verified_at_utc: float | None
    consumed_at_utc: float | None
    updated_at_utc: float

    def to_dict(self) -> dict:
        """Nurse/API-safe representation — contains no patient data by
        construction (there is none on this dataclass to leak)."""
        return {
            "hardware_uid": self.hardware_uid,
            "short_code": self.short_code,
            "inventory_status": self.inventory_status,
            "consumed": self.consumed,
            "provisioned_at_utc": self.provisioned_at_utc,
            "identity_verified_at_utc": self.identity_verified_at_utc,
            "consumed_at_utc": self.consumed_at_utc,
            "updated_at_utc": self.updated_at_utc,
        }


def _row_to_record(row: sqlite3.Row) -> AdapterRecord:
    return AdapterRecord(
        hardware_uid=row["hardware_uid"],
        short_code=row["short_code"],
        inventory_status=row["inventory_status"],
        consumed=bool(row["consumed"]),
        provisioned_at_utc=row["provisioned_at_utc"],
        identity_verified_at_utc=row["identity_verified_at_utc"],
        consumed_at_utc=row["consumed_at_utc"],
        updated_at_utc=row["updated_at_utc"],
    )


class AdapterInventory:
    """Persistent SQLite-backed provisioned-adapter inventory. Thread-safe
    for the POC's needs via SQLite's own file-level locking (`isolation_level`
    default + short transactions); not designed for high-concurrency
    multi-process writers beyond the pairing-gate's concurrent-assignment
    guard, which lives in `patient_assignment.py`, not here."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, stat.S_IRWXU)  # 0700 — no patient data, but still device inventory
        except OSError:
            pass
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # A single sqlite3.Connection is not safe for concurrent use from
        # multiple threads even with check_same_thread=False — every method
        # below that touches self._conn holds this lock for its whole body
        # (an RLock so reservation/finalization/lifecycle methods can call the
        # unlocked read helpers internally without deadlocking).
        self._lock = threading.RLock()
        self._init_schema()
        try:
            os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS adapters (
                hardware_uid TEXT PRIMARY KEY,
                short_code TEXT NOT NULL UNIQUE,
                inventory_status TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                provisioned_at_utc REAL NOT NULL,
                identity_verified_at_utc REAL,
                consumed_at_utc REAL,
                updated_at_utc REAL NOT NULL
            )
            """
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(adapters)").fetchall()}
        if "identity_verified_at_utc" not in columns:
            self._conn.execute("ALTER TABLE adapters ADD COLUMN identity_verified_at_utc REAL")
        # Rows created by the former one-phase API have no connected
        # production-firmware verification evidence. Migrate them fail-closed.
        self._conn.execute(
            """
            UPDATE adapters SET inventory_status = ?
            WHERE inventory_status = ? AND identity_verified_at_utc IS NULL
            """,
            (InventoryStatus.PENDING_PROVISIONING, InventoryStatus.PROVISIONED),
        )
        self._conn.execute("CREATE TABLE IF NOT EXISTS prototype_reuse_policy (hardware_uid TEXT PRIMARY KEY, short_code TEXT NOT NULL UNIQUE, policy_version TEXT NOT NULL, authorization_ref TEXT NOT NULL, authorized_at_utc REAL NOT NULL)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS adapter_lifecycle_audit (event_id TEXT PRIMARY KEY, hardware_uid TEXT NOT NULL, short_code TEXT NOT NULL, event_type TEXT NOT NULL, policy_version TEXT NOT NULL, authorization_ref TEXT NOT NULL, recorded_at_utc REAL NOT NULL, previous_consumed INTEGER NOT NULL, previous_consumed_at_utc REAL)")
        for operation in ("UPDATE", "DELETE"):
            self._conn.execute(f"CREATE TRIGGER IF NOT EXISTS lifecycle_no_{operation.lower()} BEFORE {operation} ON adapter_lifecycle_audit BEGIN SELECT RAISE(ABORT, 'append-only lifecycle history'); END")
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _next_short_code_unlocked(self) -> str:
        cur = self._conn.execute("SELECT short_code FROM adapters")
        max_n = 0
        for (code,) in cur.fetchall():
            m = SHORT_CODE_PATTERN.match(code)
            if m:
                max_n = max(max_n, int(m.group(1)))
        n = max_n + 1
        width = max(_MIN_CODE_WIDTH, len(str(n)))
        return f"VN-{n:0{width}d}"

    def _get_by_uid_unlocked(self, hardware_uid: str) -> AdapterRecord | None:
        row = self._conn.execute(
            "SELECT * FROM adapters WHERE hardware_uid = ?", (_normalize_uid(hardware_uid),)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def _get_by_code_unlocked(self, short_code: str) -> AdapterRecord | None:
        row = self._conn.execute(
            "SELECT * FROM adapters WHERE short_code = ?", (short_code,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def reserve_provisioning(self, hardware_uid: str, *, now: float | None = None) -> AdapterRecord:
        """Atomically reserve one UID and the next sequential VN code.

        Retrying an interrupted reservation for the same UID returns the
        existing pending record and never consumes another code. A finalized
        or retired record cannot be silently reopened.
        """
        hardware_uid = _normalize_uid(hardware_uid)
        with self._lock:
            try:
                # Serialize the read-next-code/write sequence across separate
                # processes or AdapterInventory instances as well as threads.
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._get_by_uid_unlocked(hardware_uid)
                if existing is not None:
                    if existing.inventory_status == InventoryStatus.PENDING_PROVISIONING:
                        self._conn.commit()
                        return existing
                    raise InventoryError(f"hardware_uid {hardware_uid!r} already has a finalized inventory record")

                ts = now if now is not None else time.time()
                short_code = self._next_short_code_unlocked()
                self._conn.execute(
                    """
                    INSERT INTO adapters
                        (hardware_uid, short_code, inventory_status, consumed,
                         provisioned_at_utc, identity_verified_at_utc,
                         consumed_at_utc, updated_at_utc)
                    VALUES (?, ?, ?, 0, ?, NULL, NULL, ?)
                    """,
                    (hardware_uid, short_code, InventoryStatus.PENDING_PROVISIONING, ts, ts),
                )
                self._conn.commit()
            except InventoryError:
                self._conn.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise InventoryError(f"provisioning conflict for {hardware_uid!r}: {exc}") from exc
            except sqlite3.OperationalError as exc:
                self._conn.rollback()
                raise InventoryError("inventory storage is unavailable") from exc
            return self._get_by_uid_unlocked(hardware_uid)  # type: ignore[return-value]

    def provision(self, hardware_uid: str, *, now: float | None = None) -> AdapterRecord:
        """Compatibility name for phase-one reservation; never makes a unit usable."""
        return self.reserve_provisioning(hardware_uid, now=now)

    def finalize_provisioning(
        self,
        *,
        expected_hardware_uid: str,
        expected_short_code: str,
        reported_hardware_uid: str,
        reported_short_code: str,
        now: float | None = None,
    ) -> AdapterRecord:
        """Finalize only an exact production-firmware UID/VN verification."""
        expected_uid = _normalize_uid(expected_hardware_uid)
        reported_uid = _normalize_uid(reported_hardware_uid)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                record = self._get_by_uid_unlocked(expected_uid)
                if record is None:
                    raise InventoryError(f"no pending reservation for hardware_uid {expected_uid!r}")
                if record.inventory_status != InventoryStatus.PENDING_PROVISIONING:
                    raise InventoryError(f"{record.short_code!r} is not pending provisioning")
                if record.short_code != expected_short_code:
                    raise InventoryError("reserved short code does not match the expected short code")
                if reported_uid != expected_uid or reported_short_code != expected_short_code:
                    raise InventoryError("production firmware identity does not match the pending reservation")
                ts = now if now is not None else time.time()
                cursor = self._conn.execute(
                    """
                    UPDATE adapters
                    SET inventory_status = ?, provisioned_at_utc = ?,
                        identity_verified_at_utc = ?, updated_at_utc = ?
                    WHERE hardware_uid = ? AND inventory_status = ?
                    """,
                    (
                        InventoryStatus.PROVISIONED,
                        ts,
                        ts,
                        ts,
                        expected_uid,
                        InventoryStatus.PENDING_PROVISIONING,
                    ),
                )
                if cursor.rowcount != 1:
                    raise InventoryError("pending provisioning changed concurrently")
                self._conn.commit()
            except InventoryError:
                self._conn.rollback()
                raise
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise InventoryError("inventory storage is unavailable") from exc
            return self._get_by_uid_unlocked(expected_uid)  # type: ignore[return-value]

    def abort_provisioning(self, hardware_uid: str, *, now: float | None = None) -> AdapterRecord:
        """Explicitly retire an interrupted pending reservation without reusing its code."""
        hardware_uid = _normalize_uid(hardware_uid)
        with self._lock:
            record = self._get_by_uid_unlocked(hardware_uid)
            if record is None or record.inventory_status != InventoryStatus.PENDING_PROVISIONING:
                raise InventoryError("only a pending provisioning record can be aborted")
            ts = now if now is not None else time.time()
            self._conn.execute(
                "UPDATE adapters SET inventory_status = ?, updated_at_utc = ? WHERE hardware_uid = ?",
                (InventoryStatus.RETIRED, ts, hardware_uid),
            )
            self._conn.commit()
            return self._get_by_uid_unlocked(hardware_uid)  # type: ignore[return-value]

    def get_by_code(self, short_code: str) -> AdapterRecord | None:
        with self._lock:
            return self._get_by_code_unlocked(short_code)

    def get_by_uid(self, hardware_uid: str) -> AdapterRecord | None:
        with self._lock:
            return self._get_by_uid_unlocked(hardware_uid)

    def list_all(self) -> list[AdapterRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM adapters ORDER BY short_code").fetchall()
            return [_row_to_record(r) for r in rows]

    def mark_consumed(self, short_code: str, *, now: float | None = None) -> AdapterRecord:
        with self._lock:
            record = self._get_by_code_unlocked(short_code)
            if record is None:
                raise InventoryError(f"no inventory record for {short_code!r}")
            if record.inventory_status != InventoryStatus.PROVISIONED or record.identity_verified_at_utc is None:
                raise InventoryError(f"{short_code!r} is not a verified provisioned adapter")
            if record.consumed:
                raise InventoryError(f"{short_code!r} is already consumed")
            ts = now if now is not None else time.time()
            try:
                self._conn.execute(
                    "UPDATE adapters SET consumed = 1, consumed_at_utc = ?, updated_at_utc = ? WHERE short_code = ?",
                    (ts, ts, short_code),
                )
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                self._conn.rollback()
                raise InventoryError("inventory storage is unavailable") from exc
            return self._get_by_code_unlocked(short_code)  # type: ignore[return-value]

    def authorize_prototype_reuse(self, short_code: str, hardware_uid: str, *, authorization_ref: str, recover: bool = False, now: float | None = None) -> AdapterRecord:
        """Explicit maintenance operation; caller must exclude active bindings."""
        with self._lock:
            record = self._get_by_code_unlocked(short_code)
            if (short_code not in DEVELOPMENT_CODES or not authorization_ref.strip() or record is None
                or record.hardware_uid != _normalize_uid(hardware_uid)
                or record.inventory_status != InventoryStatus.PROVISIONED
                or record.identity_verified_at_utc is None):
                raise InventoryError("prototype reuse requires the existing verified development identity")
            ts = time.time() if now is None else now
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute("SELECT policy_version FROM prototype_reuse_policy WHERE hardware_uid=?", (record.hardware_uid,)).fetchone()
                if existing and existing[0] != PROTOTYPE_REUSE_POLICY:
                    raise InventoryError("prototype reuse policy conflict")
                if not existing:
                    self._conn.execute("INSERT INTO prototype_reuse_policy VALUES(?,?,?,?,?)", (record.hardware_uid, short_code, PROTOTYPE_REUSE_POLICY, authorization_ref, ts))
                    self._audit(record, "REUSE_AUTHORIZED", PROTOTYPE_REUSE_POLICY, authorization_ref, ts)
                if recover and record.consumed:
                    self._audit(record, "CONSUMED_RECOVERED", PROTOTYPE_REUSE_POLICY, authorization_ref, ts)
                    self._conn.execute("UPDATE adapters SET consumed=0, consumed_at_utc=NULL, updated_at_utc=? WHERE hardware_uid=?", (ts, record.hardware_uid))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self._get_by_code_unlocked(short_code)

    def restore_development_record(self, record: AdapterRecord, *, source_ref: str, now: float | None = None):
        """Restore an existing verified inventory snapshot, never provision a new identity."""
        with self._lock:
            if (record.short_code not in DEVELOPMENT_CODES or record.inventory_status != InventoryStatus.PROVISIONED
                or record.identity_verified_at_utc is None or not source_ref):
                raise InventoryError("verified development inventory snapshot required")
            current = self._get_by_code_unlocked(record.short_code)
            by_uid = self._get_by_uid_unlocked(record.hardware_uid)
            if current or by_uid:
                if current and by_uid and current.hardware_uid == record.hardware_uid and by_uid.short_code == record.short_code:
                    return current
                raise InventoryError("inventory restoration identity conflict")
            ts = time.time() if now is None else now
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute("INSERT INTO adapters (hardware_uid,short_code,inventory_status,consumed,provisioned_at_utc,identity_verified_at_utc,consumed_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?)", (record.hardware_uid,record.short_code,record.inventory_status,int(record.consumed),record.provisioned_at_utc,record.identity_verified_at_utc,record.consumed_at_utc,record.updated_at_utc))
                self._audit(record, "REGISTERED_INVENTORY_RESTORED", PROTOTYPE_REUSE_POLICY, source_ref, ts)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self._get_by_code_unlocked(record.short_code)

    def _audit(self, record, event_type, policy_version, authorization_ref, ts):
        self._conn.execute("INSERT INTO adapter_lifecycle_audit VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), record.hardware_uid, record.short_code, event_type, policy_version, authorization_ref, ts, int(record.consumed), record.consumed_at_utc))

    def finish_use(self, short_code: str, *, session_ref: str, now: float | None = None) -> AdapterRecord:
        """Single-use remains the default; registered development units return unassigned."""
        with self._lock:
            record = self._get_by_code_unlocked(short_code)
            policy = self._conn.execute("SELECT policy_version FROM prototype_reuse_policy WHERE hardware_uid=? AND short_code=?", (record.hardware_uid, short_code)).fetchone() if record else None
            if not policy:
                return record if record and record.consumed else self.mark_consumed(short_code, now=now)
            if policy[0] != PROTOTYPE_REUSE_POLICY or record.consumed:
                raise InventoryError("prototype reuse state conflict")
            ts = time.time() if now is None else now
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._audit(record, "SESSION_ENDED_REUSABLE", policy[0], session_ref, ts)
                self._conn.execute("UPDATE adapters SET updated_at_utc=? WHERE hardware_uid=?", (ts, record.hardware_uid))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self._get_by_code_unlocked(short_code)

    def retire(self, short_code: str, *, now: float | None = None) -> AdapterRecord:
        with self._lock:
            record = self._get_by_code_unlocked(short_code)
            if record is None:
                raise InventoryError(f"no inventory record for {short_code!r}")
            ts = now if now is not None else time.time()
            try:
                self._conn.execute(
                    "UPDATE adapters SET inventory_status = ?, updated_at_utc = ? WHERE short_code = ?",
                    (InventoryStatus.RETIRED, ts, short_code),
                )
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                self._conn.rollback()
                raise InventoryError("inventory storage is unavailable") from exc
            return self._get_by_code_unlocked(short_code)  # type: ignore[return-value]


def _normalize_uid(hardware_uid: str) -> str:
    if not isinstance(hardware_uid, str) or not hardware_uid.strip():
        raise InventoryError("hardware_uid must be a non-empty string")
    return hardware_uid.strip().upper()
