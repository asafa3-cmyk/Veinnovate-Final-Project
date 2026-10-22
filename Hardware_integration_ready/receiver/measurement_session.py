"""measurement_session.py — the "Measurement Session" state machine, kept
separate from `patient_assignment.py` (AGENTS.md requires the two be
treated as distinct state machines, alongside BLE connection and live
discovery).

An assignment is the binding decision ("this VN-NNN belongs to this
patient now"); a measurement session is the bounded run of data
collection that happens under that assignment. In this POC a session
starts immediately when an assignment is committed and ends when the nurse
explicitly ends it (which also marks the adapter consumed — see
`identity_service.py`), but the two remain independently modeled so a
future revision (e.g. multiple sessions per assignment, or a session that
outlives a momentary BLE reconnect) does not have to unpick a merged
concept.

Storage: a dedicated SQLite file, separate from both the inventory and the
assignment store, for the same file-level-separation reasoning as
`patient_assignment.py`.
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


class MeasurementSessionError(ValueError):
    pass


@dataclass(frozen=True)
class MeasurementSessionRecord:
    session_id: str
    assignment_id: str
    hardware_uid: str
    patient_id: str
    started_at_utc: float
    ended_at_utc: float | None
    active: bool

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "assignment_id": self.assignment_id,
            "hardware_uid": self.hardware_uid,
            "patient_id": self.patient_id,
            "started_at_utc": self.started_at_utc,
            "ended_at_utc": self.ended_at_utc,
            "active": self.active,
        }


def _row_to_record(row: sqlite3.Row) -> MeasurementSessionRecord:
    return MeasurementSessionRecord(
        session_id=row["session_id"],
        assignment_id=row["assignment_id"],
        hardware_uid=row["hardware_uid"],
        patient_id=row["patient_id"],
        started_at_utc=row["started_at_utc"],
        ended_at_utc=row["ended_at_utc"],
        active=bool(row["active"]),
    )


class MeasurementSessionStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, stat.S_IRWXU)
        except OSError:
            pass
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # See adapter_inventory.py / patient_assignment.py: a single
        # sqlite3.Connection is not safe for concurrent multi-thread use
        # even with check_same_thread=False — every method holds this lock.
        self._lock = threading.Lock()
        self._init_schema()
        try:
            os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                assignment_id TEXT NOT NULL,
                hardware_uid TEXT NOT NULL,
                patient_id TEXT NOT NULL,
                started_at_utc REAL NOT NULL,
                ended_at_utc REAL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_active_uid
            ON sessions(hardware_uid) WHERE active = 1
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _get_unlocked(self, session_id: str) -> MeasurementSessionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def start(
        self, *, assignment_id: str, hardware_uid: str, patient_id: str, now: float | None = None
    ) -> MeasurementSessionRecord:
        ts = now if now is not None else time.time()
        session_id = str(uuid.uuid4())
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO sessions
                        (session_id, assignment_id, hardware_uid, patient_id, started_at_utc, ended_at_utc, active)
                    VALUES (?, ?, ?, ?, ?, NULL, 1)
                    """,
                    (session_id, assignment_id, hardware_uid, patient_id, ts),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise MeasurementSessionError(
                    f"an active measurement session already exists for hardware_uid {hardware_uid!r}"
                ) from exc
            except sqlite3.OperationalError as exc:
                self._conn.rollback()
                raise MeasurementSessionError("measurement-session storage is unavailable") from exc
            return self._get_unlocked(session_id)  # type: ignore[return-value]

    def get(self, session_id: str) -> MeasurementSessionRecord | None:
        with self._lock:
            return self._get_unlocked(session_id)

    def get_active_by_uid(self, hardware_uid: str) -> MeasurementSessionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE hardware_uid = ? AND active = 1", (hardware_uid,)
            ).fetchone()
            return _row_to_record(row) if row is not None else None

    def end(self, session_id: str, *, now: float | None = None) -> MeasurementSessionRecord:
        with self._lock:
            record = self._get_unlocked(session_id)
            if record is None:
                raise MeasurementSessionError(f"no session {session_id!r}")
            if not record.active:
                raise MeasurementSessionError(f"session {session_id!r} is already ended")
            ts = now if now is not None else time.time()
            try:
                self._conn.execute(
                    "UPDATE sessions SET active = 0, ended_at_utc = ? WHERE session_id = ?",
                    (ts, session_id),
                )
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                self._conn.rollback()
                raise MeasurementSessionError("measurement-session storage is unavailable") from exc
            return self._get_unlocked(session_id)  # type: ignore[return-value]
