"""live_ble_discovery.py — the "Live BLE discovery" state machine (AGENTS.md:
keep the persistent inventory and the live discovery list distinct).

This holds only what is *currently advertising*, in memory, never persisted:
powered-off/out-of-range adapters simply are not here. It is fed by
whatever discovers advertisements — real `BleakScanner` in a future
hardware build, or `adapter_pairing_simulator.py` for this software-only
POC. Either way this module's identity concept is exactly one thing: a
`hardware_uid` per advertisement. It has no notion of BLE name and never
exposes address/RSSI/discovery-order as identity (AGENTS.md: "Never
identify by BLE name, address, RSSI, or discovery order").

A `source_token` is accepted alongside `hardware_uid` purely as an opaque
broadcast-source counter (in a future real build this would be the BLE
address of whichever radio sent the advertisement) — it exists ONLY to
answer "are two physically different radios currently claiming the same
provisioned identity right now?" (the "duplicate identity" fail-closed
rejection in `assignment_gate.py`). It is never surfaced as, or used as, a
device identity anywhere; the identity is always `hardware_uid`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


DEFAULT_FRESHNESS_S = 5.0


@dataclass(frozen=True)
class DiscoveredAdapter:
    hardware_uid: str
    source_token: str
    first_seen_utc: float
    last_seen_utc: float
    connection_handle: object | None = None


class LiveBleDiscovery:
    """In-memory registry of currently-advertising `hardware_uid`s. Not a
    database — this state is meaningless across a process restart."""

    def __init__(self, *, freshness_s: float = DEFAULT_FRESHNESS_S) -> None:
        self.freshness_s = freshness_s
        self._lock = threading.Lock()
        # hardware_uid -> source_token -> DiscoveredAdapter
        self._seen: dict[str, dict[str, DiscoveredAdapter]] = {}
        self._advertisement_stats: dict[str, dict[str, float]] = {}
        self.malformed_v2_payloads = 0

    def on_advertisement(
        self, hardware_uid: str, *, source_token: str = "default", now: float | None = None,
        connection_handle: object | None = None,
    ) -> None:
        """Record (or refresh) an advertisement from one broadcast source.
        Two different `source_token`s advertising the same `hardware_uid`
        concurrently is the "duplicate identity" condition surfaced by
        `fresh_candidates()`."""
        ts = now if now is not None else time.time()
        with self._lock:
            by_source = self._seen.setdefault(hardware_uid, {})
            existing = by_source.get(source_token)
            stats = self._advertisement_stats.setdefault(
                hardware_uid, {"count": 0, "interval_count": 0, "interval_sum_s": 0.0, "max_interval_s": 0.0}
            )
            stats["count"] += 1
            if existing is not None:
                interval = max(0.0, ts - existing.last_seen_utc)
                stats["interval_count"] += 1
                stats["interval_sum_s"] += interval
                stats["max_interval_s"] = max(stats["max_interval_s"], interval)
            first_seen = existing.first_seen_utc if existing is not None else ts
            by_source[source_token] = DiscoveredAdapter(
                hardware_uid=hardware_uid,
                source_token=source_token,
                first_seen_utc=first_seen,
                last_seen_utc=ts,
                connection_handle=connection_handle if connection_handle is not None else (
                    existing.connection_handle if existing is not None else None
                ),
            )

    def record_malformed_v2(self) -> None:
        with self._lock:
            self.malformed_v2_payloads += 1

    def advertisement_stats(self, hardware_uid: str) -> dict[str, float | int]:
        with self._lock:
            stats = self._advertisement_stats.get(hardware_uid, {})
            intervals = int(stats.get("interval_count", 0))
            return {
                "received": int(stats.get("count", 0)),
                "mean_interval_s": stats.get("interval_sum_s", 0.0) / intervals if intervals else 0.0,
                "max_interval_s": stats.get("max_interval_s", 0.0),
                "malformed_v2_payloads": self.malformed_v2_payloads,
            }

    def on_power_off(self, hardware_uid: str, *, source_token: str | None = None) -> None:
        """Explicit OFF/vanish event. With no source_token, removes every
        broadcast source for this identity; with one, removes only that
        source (useful for clearing a single side of a simulated
        duplicate-identity scenario)."""
        with self._lock:
            if source_token is None:
                self._seen.pop(hardware_uid, None)
            else:
                by_source = self._seen.get(hardware_uid)
                if by_source is not None:
                    by_source.pop(source_token, None)
                    if not by_source:
                        self._seen.pop(hardware_uid, None)

    def get(self, hardware_uid: str, *, now: float | None = None) -> DiscoveredAdapter | None:
        """Returns any one entry (regardless of freshness) if the identity
        is known at all — callers distinguish "never seen" (None) from
        "seen but stale" via `is_fresh()`. For duplicate-identity detection
        use `fresh_candidates()` instead, which preserves every source."""
        with self._lock:
            by_source = self._seen.get(hardware_uid)
            if not by_source:
                return None
            return next(iter(by_source.values()))

    def is_fresh(self, entry: DiscoveredAdapter, *, now: float | None = None) -> bool:
        ts = now if now is not None else time.time()
        return (ts - entry.last_seen_utc) <= self.freshness_s

    def fresh_candidates(self, hardware_uid: str, *, now: float | None = None) -> list[DiscoveredAdapter]:
        """All currently-fresh broadcast sources for this identity. Normally
        at most one; more than one means two distinct radios are
        simultaneously claiming the same provisioned identity (duplicate
        identity — see assignment_gate.py)."""
        ts = now if now is not None else time.time()
        with self._lock:
            by_source = dict(self._seen.get(hardware_uid, {}))
        return [e for e in by_source.values() if (ts - e.last_seen_utc) <= self.freshness_s]

    def all_fresh(self, *, now: float | None = None) -> list[DiscoveredAdapter]:
        ts = now if now is not None else time.time()
        with self._lock:
            all_entries = [e for by_source in self._seen.values() for e in by_source.values()]
        return [e for e in all_entries if (ts - e.last_seen_utc) <= self.freshness_s]

    def known_uids(self) -> set[str]:
        """Snapshot of every hardware_uid currently tracked (fresh or not)
        — used by the pairing window to compute "previously unseen"."""
        with self._lock:
            return set(self._seen.keys())
