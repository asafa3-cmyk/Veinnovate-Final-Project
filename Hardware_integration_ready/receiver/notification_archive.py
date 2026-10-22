"""Lossless DATA notification journal, including malformed and rejected packets.

Independent versioned diagnostic extension; never a measurement acceptance gate.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from protocol_decoder import ProtocolError, decode_data_packet


def notification_row(raw: bytes, *, index: int, host_monotonic_ns: int, host_utc: str) -> dict:
    raw = bytes(raw)
    try:
        decoded = asdict(decode_data_packet(raw))
        # Preserve V2 archive-v1 metadata exactly, without upgrading its
        # missing acquisition evidence. Live decoder objects remain uncertain.
        if decoded["protocol_version"] == 2:
            decoded.pop("software_overflow_count")
            decoded.pop("acquisition_uncertainty")
    except ProtocolError:
        decoded = None
    return {
        "archive_version": 2 if decoded and decoded["protocol_version"] == 3 else 1,
        "notification_index": index,
        "host_monotonic_ns": host_monotonic_ns, "host_utc": host_utc,
        "raw_base64": base64.b64encode(raw).decode("ascii"),
        "raw_sha256": hashlib.sha256(raw).hexdigest(), "decoded": decoded,
    }


def replay_notifications(rows: list[dict]) -> dict:
    """Re-decode original bytes, validate every field, report all session changes."""
    packets, changes, malformed = [], [], []
    previous = None
    for index, row in enumerate(rows):
        raw = base64.b64decode(row["raw_base64"], validate=True)
        rebuilt = notification_row(raw, index=index,
                                   host_monotonic_ns=row["host_monotonic_ns"],
                                   host_utc=row["host_utc"])
        if row != rebuilt:
            raise ValueError("notification replay metadata mismatch")
        packet = rebuilt["decoded"]
        if packet is None:
            malformed.append(index)
            continue
        identity = (packet["hardware_uid"], packet["sensor_session_id"])
        if previous is not None and previous != identity:
            changes.append(index)
        previous = identity
        packets.append(packet)
    return {"packets": packets, "session_changes": changes, "malformed_notifications": malformed}


class NotificationJournal:
    """Persist before decoding in auxiliary callbacks; retain data on later failure."""
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / f"notifications-{uuid4().hex}.jsonl"
        self.index = 0
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)

    def append(self, raw: bytes, *, host_monotonic_ns=None, host_utc=None) -> None:
        row = notification_row(raw, index=self.index, host_monotonic_ns=time.monotonic_ns() if host_monotonic_ns is None else host_monotonic_ns,
                               host_utc=datetime.now(timezone.utc).isoformat() if host_utc is None else host_utc)
        content = json.dumps(row, sort_keys=True, separators=(",", ":"))
        envelope = {"row": row, "sha256": hashlib.sha256(content.encode()).hexdigest()}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.index += 1


def replay_journal(path: Path) -> dict:
    rows = []
    for line in path.read_text().splitlines():
        envelope = json.loads(line)
        content = json.dumps(envelope["row"], sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(content.encode()).hexdigest() != envelope["sha256"]:
            raise ValueError("notification journal checksum mismatch")
        rows.append(envelope["row"])
    return replay_notifications(rows)
