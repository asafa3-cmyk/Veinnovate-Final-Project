"""Atomic, PHI-free diagnostics for one Stage 9 transport attempt."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


from notification_archive import notification_row, replay_notifications


SCHEMA_VERSION = "2.0.0"
BUNDLE_ROOT = Path(__file__).resolve().parent.parent


class TransportDiagnosticIntegrityError(RuntimeError):
    """A persisted diagnostic directory is incomplete, altered, or ambiguous."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class TransportDiagnostics:
    """Collect transport facts without ever treating them as a QA capture."""

    def __init__(
        self,
        *,
        experiment_id: str,
        hardware_uid: str,
        short_code: str,
        config_id: int,
        repetition: int,
        zone: str | None = None,
        attempt_ordinal: int | None = None,
        attempt_id: str | None = None,
    ) -> None:
        if (zone is None) != (attempt_ordinal is None):
            raise ValueError("zone and attempt ordinal must be supplied together")
        if zone is not None and (zone not in {"Z23", "Z34", "Z45"} or not isinstance(attempt_ordinal, int)):
            raise ValueError("invalid Stage 9B attempt zone or ordinal")
        stamp = utc_now().replace("-", "").replace(":", "").replace(".", "")
        self.attempt_id = attempt_id or (
            f"transport-{short_code}-c{config_id:02d}-r{repetition:02d}-{stamp}-{uuid4().hex[:8]}"
        )
        self.payload = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "stage9_transport_diagnostic",
            "attempt_id": self.attempt_id,
            "experiment_id": experiment_id,
            "device": {"hardware_uid": hardware_uid, "short_code": short_code},
            "slot": {"config_id": config_id, "repetition": repetition},
            "attempt_metadata": (
                None
                if zone is None
                else {"attempt_ordinal": attempt_ordinal, "zone": zone}
            ),
            "timing": {
                "attempt_started_utc": utc_now(),
                "attempt_ended_utc": None,
                "first_data_host_monotonic_ns": None,
                "last_data_host_monotonic_ns": None,
                "first_data_utc": None,
                "last_data_utc": None,
            },
            "handshake": {
                "status_ack_committed": False,
                "start_sent": False,
                "start_count": 0,
            },
            "data": {
                "notifications_received": 0,
                "packets_decoded": 0,
                "accepted_sample_pairs": 0,
                "observed_average_pair_rate_hz": None,
                "rejected_packets": [],
                "notification_index_range": None,
                "packet_first_sample_index": None,
                "packet_last_sample_index": None,
                "sensor_session_id_first": None,
                "sensor_session_id_last": None,
                "raw_red_ir_retained": True,
                "raw_sample_pairs_retained": 0,
            },
            "firmware_reported_capture_completion": False,
            # Populated only on a QA (LOW_PSNR etc.) rejection -- see
            # record_quality_metrics(). Never populated for a transport-level
            # failure (no data ever reached the QA validator), and never a
            # substitute for a published Stage 7 capture record.
            "quality_metrics": None,
            "disconnect_state": "not_connected",
            "completion_state": "INITIALIZED",
            "failure_class": None,
            "error": None,
            "capture_published": False,
            "ledger_entry_created": False,
        }
        self._raw_rows: list[dict] = []
        self._closed = False

    def connected(self) -> None:
        self.payload["disconnect_state"] = "connected"

    def status_committed(self) -> None:
        self.payload["handshake"]["status_ack_committed"] = True

    def start_sent(self) -> None:
        handshake = self.payload["handshake"]
        handshake["start_count"] += 1
        handshake["start_sent"] = True

    def observe_status(self, status) -> None:
        if (
            self.payload["handshake"]["start_sent"]
            and self.payload["data"]["accepted_sample_pairs"] == 3000
            and status.sensor_config_id == 0
        ):
            self.payload["firmware_reported_capture_completion"] = True

    def data_notification(self, *, host_monotonic_ns: int, host_utc: str, raw: bytes | None = None) -> int:
        data = self.payload["data"]
        index = data["notifications_received"]
        data["notifications_received"] += 1
        if raw is not None:
            rows = self.payload.setdefault("notification_archive", [])
            rows.append(notification_row(raw, index=len(rows),
                                         host_monotonic_ns=host_monotonic_ns, host_utc=host_utc))
        timing = self.payload["timing"]
        if timing["first_data_host_monotonic_ns"] is None:
            timing["first_data_host_monotonic_ns"] = host_monotonic_ns
            timing["first_data_utc"] = host_utc
            data["notification_index_range"] = [index, index]
        else:
            data["notification_index_range"][1] = index
        timing["last_data_host_monotonic_ns"] = host_monotonic_ns
        timing["last_data_utc"] = host_utc
        return index

    def decoded(self, packet) -> None:
        data = self.payload["data"]
        packet_index = data["packets_decoded"]
        data["packets_decoded"] += 1
        if data["packet_first_sample_index"] is None:
            data["packet_first_sample_index"] = packet.first_sample_index
            data["sensor_session_id_first"] = packet.sensor_session_id
        data["packet_last_sample_index"] = packet.last_sample_index
        data["sensor_session_id_last"] = packet.sensor_session_id
        red = list(packet.red)
        ir = list(packet.ir)
        if len(red) != len(ir):
            raise ValueError("decoded RED/IR lengths differ")
        self._raw_rows.append({
            "packet_index": packet_index,
            "sensor_session_id": packet.sensor_session_id,
            "sensor_config_id": packet.sensor_config_id,
            "first_sample_index": packet.first_sample_index,
            "device_monotonic_timestamp_us": packet.device_monotonic_timestamp_us,
            "red": red,
            "ir": ir,
        })
        data["raw_sample_pairs_retained"] += len(red)

    def accepted(self, sample_pairs: int) -> None:
        self.payload["data"]["accepted_sample_pairs"] += sample_pairs

    def set_average_pair_rate(self, *, elapsed_s: float) -> None:
        if elapsed_s > 0:
            self.payload["data"]["observed_average_pair_rate_hz"] = round(
                self.payload["data"]["accepted_sample_pairs"] / elapsed_s, 3
            )

    def rejected(self, notification_index: int, reason_code: str) -> None:
        self.payload["data"]["rejected_packets"].append(
            {"notification_index": notification_index, "reason_code": reason_code}
        )

    def finish(self, *, state: str, failure_class: str | None = None, error: str | None = None) -> None:
        if self._closed:
            return
        self.payload["completion_state"] = state
        self.payload["failure_class"] = failure_class
        self.payload["error"] = error
        self.payload["timing"]["attempt_ended_utc"] = utc_now()
        self._closed = True

    def disconnected(self, *, clean: bool) -> None:
        self.payload["disconnect_state"] = "disconnected" if clean else "disconnect_failed"

    def record_quality_metrics(self, quality_metrics: dict) -> None:
        self.payload["quality_metrics"] = quality_metrics

    def published(self) -> None:
        self.payload["capture_published"] = True

    def ledger_created(self) -> None:
        self.payload["ledger_entry_created"] = True

    def persist(self, batch_root: Path) -> Path:
        """Publish one checksummed diagnostic directory by atomic rename."""
        if not self._closed:
            raise RuntimeError("transport diagnostic must be finalized before persistence")
        batch_root = batch_root.expanduser().resolve()
        validate_external_batch_root(batch_root)
        diagnostics_root = batch_root / "diagnostics"
        diagnostics_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        final_path = diagnostics_root / self.attempt_id
        if final_path.exists():
            raise RuntimeError("transport diagnostic attempt already exists")
        staging = diagnostics_root / f".{self.attempt_id}.{uuid4().hex}.tmp"
        staging.mkdir(mode=0o700)
        try:
            diagnostic_data = (
                json.dumps(self.payload, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            raw_data = (
                "".join(
                    json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                    for row in self._raw_rows
                )
            ).encode("utf-8")
            files = {
                "diagnostic.json": diagnostic_data,
                "raw-red-ir.jsonl": raw_data,
            }
            for name, data in files.items():
                _write(staging / name, data)
            manifest = "".join(
                f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                for name, data in files.items()
            ).encode("ascii")
            _write(staging / "manifest.sha256", manifest)
            _fsync_directory(staging)
            os.replace(staging, final_path)
            _fsync_directory(diagnostics_root)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return final_path


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_external_batch_root(batch_root: Path) -> None:
    batch_root = batch_root.expanduser().resolve()
    for protected in (
        BUNDLE_ROOT,
        BUNDLE_ROOT.parent / "VeinNet_integration_ready",
        BUNDLE_ROOT.parent / "VeinoGate_Dashboard_integration_ready",
    ):
        protected = protected.resolve()
        if batch_root == protected or protected in batch_root.parents:
            raise RuntimeError("Stage 9 diagnostics must remain outside production bundle paths")


def load_and_verify_transport_diagnostic(path: str | Path) -> dict:
    """Authenticate a historical v1 or raw-retaining v2 attempt directory."""
    path = Path(path)
    try:
        manifest_path = path / "manifest.sha256"
        manifest_lines = manifest_path.read_text(encoding="ascii").splitlines()
        if not manifest_lines:
            raise TransportDiagnosticIntegrityError("diagnostic manifest is empty")
        entries: dict[str, str] = {}
        for line in manifest_lines:
            fields = line.split()
            if len(fields) != 2:
                raise TransportDiagnosticIntegrityError("diagnostic manifest line is malformed")
            digest, name = fields
            if (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or name in entries
                or Path(name).name != name
            ):
                raise TransportDiagnosticIntegrityError("diagnostic manifest entry is invalid")
            entries[name] = digest
        if "diagnostic.json" not in entries:
            raise TransportDiagnosticIntegrityError("diagnostic.json is not manifested")

        diagnostic_bytes = (path / "diagnostic.json").read_bytes()
        if hashlib.sha256(diagnostic_bytes).hexdigest() != entries["diagnostic.json"]:
            raise TransportDiagnosticIntegrityError("diagnostic.json checksum mismatch")
        payload = json.loads(diagnostic_bytes)
        version = payload.get("schema_version")
        expected_payload_files = (
            {"diagnostic.json"}
            if version == "1.0.0"
            else {"diagnostic.json", "raw-red-ir.jsonl"}
            if version == "2.0.0"
            else None
        )
        if expected_payload_files is None:
            raise TransportDiagnosticIntegrityError("unsupported diagnostic schema version")
        if set(entries) != expected_payload_files:
            raise TransportDiagnosticIntegrityError("diagnostic manifest file set is invalid")
        actual_files = {item.name for item in path.iterdir() if item.is_file()}
        if actual_files != expected_payload_files | {"manifest.sha256"}:
            raise TransportDiagnosticIntegrityError(
                "diagnostic directory contains missing or unmanifested files"
            )
        for name in expected_payload_files:
            data = (path / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != entries[name]:
                raise TransportDiagnosticIntegrityError(f"diagnostic checksum mismatch: {name}")

        if version == "2.0.0":
            attempt_metadata = payload.get("attempt_metadata")
            requires_zone = payload.get("experiment_id") == "STAGE9B-VN001-EXPOSURE-001"
            if (requires_zone and attempt_metadata is None) or (
                attempt_metadata is not None
                and (
                    set(attempt_metadata) != {"attempt_ordinal", "zone"}
                    or not isinstance(attempt_metadata["attempt_ordinal"], int)
                    or isinstance(attempt_metadata["attempt_ordinal"], bool)
                    or attempt_metadata["attempt_ordinal"] < 1
                    or attempt_metadata["zone"] not in {"Z23", "Z34", "Z45"}
                )
            ):
                raise TransportDiagnosticIntegrityError(
                    "diagnostic has invalid immutable Stage 9B attempt metadata"
                )
            rows = []
            for line_number, line in enumerate(
                (path / "raw-red-ir.jsonl").read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                row = json.loads(line)
                expected_keys = {
                    "packet_index",
                    "sensor_session_id",
                    "sensor_config_id",
                    "first_sample_index",
                    "device_monotonic_timestamp_us",
                    "red",
                    "ir",
                }
                if (
                    set(row) != expected_keys
                    or row["packet_index"] != line_number - 1
                    or not isinstance(row["red"], list)
                    or not isinstance(row["ir"], list)
                    or len(row["red"]) != len(row["ir"])
                    or any(not isinstance(value, int) or isinstance(value, bool) for value in row["red"] + row["ir"])
                ):
                    raise TransportDiagnosticIntegrityError(
                        f"raw RED/IR row {line_number} is invalid"
                    )
                rows.append(row)
            retained = sum(len(row["red"]) for row in rows)
            data = payload.get("data", {})
            if (
                data.get("raw_red_ir_retained") is not True
                or data.get("raw_sample_pairs_retained") != retained
                or data.get("packets_decoded") != len(rows)
            ):
                raise TransportDiagnosticIntegrityError(
                    "raw RED/IR manifest content disagrees with diagnostic counts"
                )
        if "notification_archive" in payload:
            rows = payload["notification_archive"]
            if len(rows) != payload["data"]["notifications_received"]:
                raise TransportDiagnosticIntegrityError("notification archive count mismatch")
            replay_notifications(rows)
        return payload
    except TransportDiagnosticIntegrityError:
        raise
    except Exception as exc:
        raise TransportDiagnosticIntegrityError(
            f"diagnostic directory is incomplete or malformed: {exc}"
        ) from exc
