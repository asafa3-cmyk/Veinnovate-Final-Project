"""patient_assignment.py — the "Patient Assignment" state machine
(AGENTS.md: the explicit, time-bounded binding of one VN-NNN adapter to one
patient/session), kept strictly separate from `ble_connection_state.py`
(the radio link) and from `adapter_inventory.py` (which carries no patient
data at all).

Only `assignment_gate.py` should call `commit()` — it is the single
fail-closed path that resolves a VN-NNN to its hardware UID and verifies a
live device before an assignment is ever created here. This module itself
enforces only the storage-level invariant: at most one *active* assignment
per `hardware_uid` at a time, via a SQLite partial unique index, so a race
between two concurrent commit attempts is caught even if a caller's
higher-level check (`get_active_by_uid`) was itself racy — the database is
the final arbiter, not the caller's in-memory read.

Storage is a dedicated SQLite file, deliberately never the same file as
`adapter_inventory.py`'s, so "patient data out of the adapter inventory"
holds even at the filesystem level, not just at the schema level.

Since the 2026-09-29 owner-authorized scope change (AGENTS.md), each row may
also carry the pairing-time `patient_profile.PatientProfile` (name, national
ID, age, sex, department, bed) in the same row, committed in the same
transaction as the binding. It stays in this 0600 file only.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from patient_profile import PatientProfile


class AssignmentError(ValueError):
    """A rejected assignment precondition (not found / already active /
    etc.) — see `assignment_gate.py` for the reason-code taxonomy built on
    top of this."""


class AssignmentConflictError(AssignmentError):
    """Raised specifically when the database's own uniqueness guard fires
    — i.e. a genuine concurrent-commit race, not a preceding logical
    check. Distinguished from AssignmentError so callers can map it to a
    dedicated CONCURRENT_ASSIGNMENT_CONFLICT reason code."""


@dataclass(frozen=True)
class AssignmentRecord:
    assignment_id: str
    hardware_uid: str
    short_code: str
    patient_id: str
    assigned_at_utc: float
    ended_at_utc: float | None
    active: bool
    profile: PatientProfile | None = None

    def to_dict(self) -> dict:
        return {
            "assignment_id": self.assignment_id,
            "hardware_uid": self.hardware_uid,
            "short_code": self.short_code,
            "patient_id": self.patient_id,
            "assigned_at_utc": self.assigned_at_utc,
            "ended_at_utc": self.ended_at_utc,
            "active": self.active,
            "profile": self.profile.to_dict() if self.profile is not None else None,
        }


_PROFILE_COLUMNS = ("first_name", "last_name", "national_id", "age_years", "sex", "department", "bed")


def _row_to_profile(row: sqlite3.Row) -> PatientProfile | None:
    if row["national_id"] is None:
        return None  # pre-1.1.0 row or a legacy/simulator binding with no profile
    return PatientProfile(**{c: row[c] for c in _PROFILE_COLUMNS})


def _row_to_record(row: sqlite3.Row) -> AssignmentRecord:
    return AssignmentRecord(
        assignment_id=row["assignment_id"],
        hardware_uid=row["hardware_uid"],
        short_code=row["short_code"],
        patient_id=row["patient_id"],
        assigned_at_utc=row["assigned_at_utc"],
        ended_at_utc=row["ended_at_utc"],
        active=bool(row["active"]),
        profile=_row_to_profile(row),
    )


class PatientAssignmentStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, stat.S_IRWXU)  # 0700 — patient_id lives here
        except OSError:
            pass
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()
        try:
            os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assignments (
                assignment_id TEXT PRIMARY KEY,
                hardware_uid TEXT NOT NULL,
                short_code TEXT NOT NULL,
                patient_id TEXT NOT NULL,
                assigned_at_utc REAL NOT NULL,
                ended_at_utc REAL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # Partial unique index: at most one row with active=1 per hardware_uid.
        # This is the actual concurrency guard (see AssignmentConflictError).
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_assignments_active_uid
            ON assignments(hardware_uid) WHERE active = 1
            """
        )
        # Additive, nullable migration for databases created before identity
        # contract 1.1.0 — existing rows simply have no profile.
        existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(assignments)")}
        for column, sql_type in (
            ("first_name", "TEXT"), ("last_name", "TEXT"), ("national_id", "TEXT"),
            ("age_years", "REAL"), ("sex", "TEXT"), ("department", "TEXT"), ("bed", "TEXT"),
        ):
            if column not in existing:
                self._conn.execute(f"ALTER TABLE assignments ADD COLUMN {column} {sql_type}")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS patient_identifiers (
                national_id TEXT PRIMARY KEY,
                patient_id TEXT NOT NULL UNIQUE
            )
        """)
        self._conn.commit()

    def resolve_patient_id(self, profile: PatientProfile) -> str:
        """Reuse a consistent existing record or allocate a random opaque ID.

        The lookup and allocation live only in this protected identity store.
        National ID is never encoded, hashed, or copied into patient_id.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT patient_id FROM patient_identifiers WHERE national_id = ?",
                    (profile.national_id,),
                ).fetchone()
                historical = self._conn.execute(
                    "SELECT DISTINCT patient_id FROM assignments WHERE national_id = ?",
                    (profile.national_id,),
                ).fetchall()
                ids = {r["patient_id"] for r in historical}
                if row is not None:
                    ids.add(row["patient_id"])
                if len(ids) > 1:
                    raise AssignmentError("PATIENT_RECORD_CONFLICT")
                patient_id = next(iter(ids)) if ids else f"P-{uuid.uuid4()}"
                if patient_id == profile.national_id:
                    raise AssignmentError("PATIENT_RECORD_CONFLICT")
                if row is None:
                    self._conn.execute(
                        "INSERT INTO patient_identifiers (national_id, patient_id) VALUES (?, ?)",
                        (profile.national_id, patient_id),
                    )
                self._conn.commit()
                return patient_id
            except AssignmentError:
                self._conn.rollback()
                raise
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AssignmentError("PATIENT_RECORD_UNAVAILABLE") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _get_unlocked(self, assignment_id: str) -> AssignmentRecord | None:
        row = self._conn.execute(
            "SELECT * FROM assignments WHERE assignment_id = ?", (assignment_id,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def get_active_by_uid(self, hardware_uid: str) -> AssignmentRecord | None:
        # A single sqlite3.Connection is not safe for concurrent use from
        # multiple threads even with check_same_thread=False — every method
        # that touches self._conn must hold self._lock, not just the ones
        # that write (see test_patient_assignment.py's real-thread race
        # test, which caught exactly this when only commit() was guarded).
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assignments WHERE hardware_uid = ? AND active = 1", (hardware_uid,)
            ).fetchone()
            return _row_to_record(row) if row is not None else None

    def get(self, assignment_id: str) -> AssignmentRecord | None:
        with self._lock:
            return self._get_unlocked(assignment_id)

    def commit(
        self,
        *,
        hardware_uid: str,
        short_code: str,
        patient_id: str,
        now: float | None = None,
        profile: PatientProfile | None = None,
    ) -> AssignmentRecord:
        """Creates a new active assignment. Fails closed with
        AssignmentConflictError if another active assignment for this
        hardware_uid was committed between the caller's own check and this
        call (see module docstring)."""
        if not patient_id or not str(patient_id).strip():
            raise AssignmentError("patient_id is required")
        ts = now if now is not None else time.time()
        assignment_id = str(uuid.uuid4())
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO assignments
                        (assignment_id, hardware_uid, short_code, patient_id,
                         assigned_at_utc, ended_at_utc, active,
                         first_name, last_name, national_id, age_years, sex, department, bed)
                    VALUES (?, ?, ?, ?, ?, NULL, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assignment_id, hardware_uid, short_code, patient_id, ts,
                        *((getattr(profile, c) for c in _PROFILE_COLUMNS) if profile else (None,) * 7),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise AssignmentConflictError(
                    f"an active assignment already exists for hardware_uid {hardware_uid!r}"
                ) from exc
            except sqlite3.OperationalError as exc:
                self._conn.rollback()
                raise AssignmentError("assignment storage is unavailable") from exc
            return self._get_unlocked(assignment_id)  # type: ignore[return-value]

    def end(self, assignment_id: str, *, now: float | None = None) -> AssignmentRecord:
        with self._lock:
            record = self._get_unlocked(assignment_id)
            if record is None:
                raise AssignmentError(f"no assignment {assignment_id!r}")
            if not record.active:
                raise AssignmentError(f"assignment {assignment_id!r} is already ended")
            ts = now if now is not None else time.time()
            try:
                self._conn.execute(
                    "UPDATE assignments SET active = 0, ended_at_utc = ? WHERE assignment_id = ?",
                    (ts, assignment_id),
                )
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                self._conn.rollback()
                raise AssignmentError("assignment storage is unavailable") from exc
            return self._get_unlocked(assignment_id)  # type: ignore[return-value]

    def list_active(self) -> list[AssignmentRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assignments WHERE active = 1 ORDER BY assigned_at_utc"
            ).fetchall()
            return [_row_to_record(r) for r in rows]
