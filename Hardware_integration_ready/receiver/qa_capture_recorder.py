"""Fail-closed, reproducible persistence for Stage 7 QA captures.

The recorder consumes the same raw Protocol V2 notifications as the edge
service and delegates packet decoding, window assembly, and QA record
construction to their existing owners.  Published capture directories are
committed with one same-filesystem rename; a partial write is never exposed
under its final name.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ble_identity import canonical_uid, encode_identity_value
from protocol_decoder import DataPacket, ProtocolError, decode_data_packet
from qa_capture_builder import RegisterSettings, build_capture_record, validate_capture_record
from window_assembler import EFFECTIVE_RATE_HZ, WINDOW_SAMPLE_COUNT, SessionRingBuffer


BUNDLE_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = BUNDLE_ROOT / "schemas" / "qa-capture-record.schema.json"
PROFILES_PATH = BUNDLE_ROOT / "config" / "hardware_profiles.json"
CAPTURE_FORMAT_VERSION = "1.0.0"
_SHORT_CODE_RE = re.compile(r"^VN-[0-9]{3,5}$")


class CaptureIntegrityError(RuntimeError):
    """The input stream or completed artifact failed a mandatory gate."""


@dataclass(frozen=True)
class PacketObservation:
    raw: bytes
    host_monotonic_ns: int
    host_utc: str
    packet: DataPacket


@dataclass(frozen=True)
class CaptureArtifact:
    path: Path
    capture_id: str
    manifest_sha256: str


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CaptureIntegrityError("host UTC timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CaptureIntegrityError("host UTC timestamp must carry the UTC offset")
    return parsed


def load_locked_profile(sensor_config_id: int) -> RegisterSettings:
    payload = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    matches = [
        profile
        for profile in payload["profiles"].values()
        if profile["config_id"] == sensor_config_id
    ]
    if len(matches) != 1:
        raise CaptureIntegrityError(f"sensor_config_id={sensor_config_id} is not one locked profile")
    settings = RegisterSettings.from_hardware_profile(matches[0])
    _validate_register_settings(settings)
    return settings


def _validate_register_settings(settings: RegisterSettings) -> None:
    if (
        settings.sample_rate_sps != 400
        or settings.sample_avg != 4
        or settings.effective_sample_rate_hz != EFFECTIVE_RATE_HZ
        or settings.pulse_width_us != 411
        or settings.led_mode != 2
        or not re.fullmatch(r"0x[0-9A-F]{2}", settings.led_brightness_hex)
        or settings.adc_range_na not in {4096, 8192, 16384}
    ):
        raise CaptureIntegrityError("hardware profile violates the locked Stage 7 acquisition contract")


class QaCaptureRecorder:
    """Collect exactly one complete, ordered Protocol V2 capture window."""

    def __init__(
        self,
        *,
        hardware_uid: str,
        short_code: str,
        sensor_config_id: int,
        register_settings: RegisterSettings | None = None,
    ) -> None:
        self.hardware_uid = canonical_uid(hardware_uid)
        if not _SHORT_CODE_RE.fullmatch(short_code):
            raise CaptureIntegrityError("short_code must match VN-NNN")
        # Reuse the production identity codec's full UID/code validation.
        encode_identity_value(self.hardware_uid, short_code)
        self.short_code = short_code
        self.sensor_config_id = sensor_config_id
        if register_settings is None:
            self.register_settings = load_locked_profile(sensor_config_id)
            self._custom_register_settings = False
        else:
            if not 16 <= sensor_config_id <= 23:
                raise CaptureIntegrityError("experimental register settings require Stage 9 config id 16..23")
            _validate_register_settings(register_settings)
            self.register_settings = register_settings
            self._custom_register_settings = True
        self._ring = SessionRingBuffer()
        self._observations: list[PacketObservation] = []
        self._failed_reason: str | None = None

    def ingest(
        self,
        raw: bytes,
        *,
        host_monotonic_ns: int | None = None,
        host_utc: str | None = None,
    ) -> None:
        """Validate and retain one raw BLE DATA notification.

        Real callbacks may omit host timestamps; deterministic fixtures must
        supply them explicitly.
        """
        if self._failed_reason is not None:
            raise CaptureIntegrityError(f"recorder is failed closed: {self._failed_reason}")
        host_monotonic_ns = time.monotonic_ns() if host_monotonic_ns is None else host_monotonic_ns
        host_utc = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            if host_utc is None
            else host_utc
        )
        try:
            packet = decode_data_packet(raw)
            self._validate_packet(packet, host_monotonic_ns, host_utc)
        except (ProtocolError, CaptureIntegrityError) as exc:
            self._failed_reason = str(exc)
            raise CaptureIntegrityError(str(exc)) from exc

        observation = PacketObservation(bytes(raw), host_monotonic_ns, host_utc, packet)
        self._observations.append(observation)
        self._ring.ingest(packet)

    def _validate_packet(self, packet: DataPacket, host_monotonic_ns: int, host_utc: str) -> None:
        if not isinstance(host_monotonic_ns, int) or isinstance(host_monotonic_ns, bool) or host_monotonic_ns < 0:
            raise CaptureIntegrityError("host monotonic timestamp must be a non-negative integer")
        utc = _parse_utc(host_utc)
        if canonical_uid(packet.hardware_uid) != self.hardware_uid:
            raise CaptureIntegrityError("packet hardware UID does not match the verified recorder identity")
        if packet.sensor_config_id != self.sensor_config_id:
            raise CaptureIntegrityError("packet sensor config does not match the locked recorder profile")
        if packet.effective_sample_rate_hz != EFFECTIVE_RATE_HZ:
            raise CaptureIntegrityError("packet sample rate violates the locked Stage 7 contract")
        if packet.sample_count != len(packet.red) or packet.sample_count != len(packet.ir):
            raise CaptureIntegrityError("decoded packet sample count is inconsistent")
        if self._observations:
            previous = self._observations[-1]
            if host_monotonic_ns <= previous.host_monotonic_ns:
                raise CaptureIntegrityError("host monotonic timestamps are not strictly increasing")
            if utc < _parse_utc(previous.host_utc):
                raise CaptureIntegrityError("host UTC timestamps are disordered")
            if packet.device_monotonic_timestamp_us <= previous.packet.device_monotonic_timestamp_us:
                raise CaptureIntegrityError("device monotonic timestamps are not strictly increasing")

    def build_record(self) -> tuple[dict, str]:
        window = self.verify_complete_window()

        source_digest = self._source_digest()
        capture_id = f"qa-{source_digest[:24]}"
        first = self._observations[0]
        last = self._observations[-1]
        device_end_us = last.packet.device_monotonic_timestamp_us
        record = build_capture_record(
            window,
            capture_id=capture_id,
            hardware_uid=self.hardware_uid,
            short_code=self.short_code,
            register_settings=self.register_settings,
            device_monotonic_timestamp_start_us=first.packet.device_monotonic_timestamp_us,
            device_monotonic_timestamp_end_us=device_end_us,
            capture_start_utc=first.host_utc,
            capture_end_utc=last.host_utc,
        )
        if not record["validation"]["valid"]:
            exc = CaptureIntegrityError(
                "QA validation failed: " + ",".join(record["validation"]["violated_rules"])
            )
            # Purely additive: lets a caller log the numbers behind a
            # rejection for diagnostics. Does not change what is persisted --
            # build_record()/persist() still never write a rejected window to
            # disk, and no accept/reject rule above is affected.
            exc.quality_metrics = record["quality_metrics"]
            raise exc
        _validate_schema(record)
        return record, source_digest

    def verify_complete_window(self):
        """Verify transport/window integrity without applying numeric QA thresholds."""
        if self._failed_reason is not None:
            raise CaptureIntegrityError(f"recorder is failed closed: {self._failed_reason}")
        if not self._observations:
            raise CaptureIntegrityError("no packets were recorded")
        first_index = self._observations[0].packet.first_sample_index
        window = self._ring.try_cut_window(first_index)
        if window is None:
            raise CaptureIntegrityError("capture is truncated before one complete window")
        if not window.integrity.is_complete:
            raise CaptureIntegrityError("window integrity failed; invalid captures are not published")
        if self._observations[-1].packet.last_sample_index != window.last_sample_index:
            raise CaptureIntegrityError("capture contains data beyond the single requested window")
        if canonical_uid(window.device_id) != self.hardware_uid:
            raise CaptureIntegrityError("assembled window identity mismatch")
        if window.sensor_config_id != self.sensor_config_id:
            raise CaptureIntegrityError("assembled window config mismatch")
        return window

    def _source_digest(self) -> str:
        digest = hashlib.sha256()
        digest.update(CAPTURE_FORMAT_VERSION.encode("ascii"))
        digest.update(self.hardware_uid.encode("ascii"))
        digest.update(self.short_code.encode("ascii"))
        digest.update(str(self.sensor_config_id).encode("ascii"))
        if self._custom_register_settings:
            digest.update(_canonical_json(self.register_settings.to_dict()))
        for item in self._observations:
            digest.update(len(item.raw).to_bytes(4, "big"))
            digest.update(item.raw)
            digest.update(item.host_monotonic_ns.to_bytes(8, "big", signed=False))
            utc_bytes = item.host_utc.encode("utf-8")
            digest.update(len(utc_bytes).to_bytes(2, "big"))
            digest.update(utc_bytes)
        return digest.hexdigest()

    def persist(
        self,
        output_root: str | Path,
        *,
        write_hook: Callable[[str], None] | None = None,
    ) -> CaptureArtifact:
        """Atomically publish JSON, raw notifications, CSV, and checksums."""
        record, source_digest = self.build_record()
        output_root = Path(output_root).expanduser().resolve()
        _reject_bundle_output(output_root)
        output_root.mkdir(mode=0o700, parents=True, exist_ok=True)

        session_id = record["session"]["sensor_session_id"]
        config_id = record["session"]["sensor_config_id"]
        final_name = f"qa-{self.short_code}-s{session_id:010d}-c{config_id:02d}-{source_digest[:12]}"
        final_path = output_root / final_name
        if final_path.exists():
            existing = load_and_verify_capture(final_path)
            if existing != record:
                raise CaptureIntegrityError("capture path exists with different verified content")
            manifest_hash = (final_path / "manifest.sha256").read_text(encoding="ascii").strip().split()[0]
            return CaptureArtifact(final_path, record["capture_id"], manifest_hash)

        staging = output_root / f".{final_name}.{uuid4().hex}.tmp"
        staging.mkdir(mode=0o700)
        try:
            files = {
                "record.json": _canonical_json(record),
                "packets.jsonl": self._packets_jsonl(),
                "samples.csv": self._samples_csv(record),
            }
            for name, payload in files.items():
                _write_file(staging / name, payload)
                if write_hook is not None:
                    write_hook(name)

            manifest = {
                "format": "veinnet_qa_capture_bundle",
                "format_version": CAPTURE_FORMAT_VERSION,
                "capture_id": record["capture_id"],
                "source_sha256": source_digest,
                "files": [
                    {"path": name, "bytes": len(files[name]), "sha256": _sha256(files[name])}
                    for name in sorted(files)
                ],
            }
            manifest_bytes = _canonical_json(manifest)
            _write_file(staging / "manifest.json", manifest_bytes)
            if write_hook is not None:
                write_hook("manifest.json")
            manifest_hash = _sha256(manifest_bytes)
            _write_file(staging / "manifest.sha256", f"{manifest_hash}  manifest.json\n".encode("ascii"))
            if write_hook is not None:
                write_hook("manifest.sha256")
            _fsync_directory(staging)
            os.replace(staging, final_path)
            _fsync_directory(output_root)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return CaptureArtifact(final_path, record["capture_id"], manifest_hash)

    def _packets_jsonl(self) -> bytes:
        rows = []
        for index, item in enumerate(self._observations):
            row = {
                "packet_index": index,
                "host_monotonic_ns": item.host_monotonic_ns,
                "host_utc": item.host_utc,
                "raw_base64": base64.b64encode(item.raw).decode("ascii"),
                "raw_sha256": _sha256(item.raw),
            }
            rows.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
        return ("\n".join(rows) + "\n").encode("utf-8")

    def _samples_csv(self, record: dict) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("sample_index", "packet_construction_timestamp_us", "sample_acquisition_timestamp_us", "red", "ir"))
        for item in self._observations:
            packet = item.packet
            for offset, (red, ir) in enumerate(zip(packet.red, packet.ir)):
                writer.writerow(
                    (
                        packet.first_sample_index + offset,
                        packet.device_monotonic_timestamp_us,
                        "", # acquisition time is not measured; never interpolate
                        red,
                        ir,
                    )
                )
        return output.getvalue().encode("utf-8")


def _validate_schema(record: dict) -> None:
    try:
        import jsonschema

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(record)
    except Exception as exc:
        raise CaptureIntegrityError(f"capture schema validation failed: {exc}") from exc


def _reject_bundle_output(output_root: Path) -> None:
    for protected in (BUNDLE_ROOT, BUNDLE_ROOT.parent / "VeinNet_integration_ready", BUNDLE_ROOT.parent / "VeinoGate_Dashboard_integration_ready"):
        protected = protected.resolve()
        if output_root == protected or protected in output_root.parents:
            raise CaptureIntegrityError("experimental captures must be stored outside production bundle paths")


def _write_file(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_and_verify_capture(path: str | Path) -> dict:
    """Verify checksums, then replay raw packets and compare every export."""
    path = Path(path)
    try:
        manifest_bytes = (path / "manifest.json").read_bytes()
        checksum_line = (path / "manifest.sha256").read_text(encoding="ascii").strip()
        expected_manifest_hash, expected_name = checksum_line.split()
        if expected_name != "manifest.json" or _sha256(manifest_bytes) != expected_manifest_hash:
            raise CaptureIntegrityError("manifest checksum mismatch")
        manifest = json.loads(manifest_bytes)
        if manifest.get("format") != "veinnet_qa_capture_bundle" or manifest.get("format_version") != CAPTURE_FORMAT_VERSION:
            raise CaptureIntegrityError("unsupported capture bundle format")
        entries = manifest["files"]
        if [entry.get("path") for entry in entries] != ["packets.jsonl", "record.json", "samples.csv"]:
            raise CaptureIntegrityError("manifest file set/order is invalid")
        payloads: dict[str, bytes] = {}
        for entry in entries:
            file_path = path / entry["path"]
            payload = file_path.read_bytes()
            if len(payload) != entry["bytes"] or _sha256(payload) != entry["sha256"]:
                raise CaptureIntegrityError(f"artifact checksum mismatch: {entry['path']}")
            payloads[entry["path"]] = payload
        record = json.loads(payloads["record.json"])
        _validate_schema(record)
        valid, violated = validate_capture_record(record)
        if (
            not valid
            or record["validation"] != {"valid": valid, "violated_rules": violated}
            or record["capture_id"] != manifest["capture_id"]
        ):
            raise CaptureIntegrityError("capture record validation/manifest identity mismatch")

        recorder = QaCaptureRecorder(
            hardware_uid=record["device"]["hardware_uid"],
            short_code=record["device"]["short_code"],
            sensor_config_id=record["session"]["sensor_config_id"],
            register_settings=(
                RegisterSettings(
                    sample_rate_sps=record["register_settings"]["sample_rate_sps"],
                    sample_avg=record["register_settings"]["sample_avg"],
                    effective_sample_rate_hz=record["register_settings"]["effective_sample_rate_hz"],
                    pulse_width_us=record["register_settings"]["pulse_width_us"],
                    led_mode=record["register_settings"]["led_mode"],
                    led_brightness_hex=record["register_settings"]["led_brightness_hex"],
                    adc_range_na=record["register_settings"]["adc_range_na"],
                )
                if 16 <= record["session"]["sensor_config_id"] <= 23
                else None
            ),
        )
        for line_number, line in enumerate(payloads["packets.jsonl"].decode("utf-8").splitlines(), start=1):
            row = json.loads(line)
            raw = base64.b64decode(row["raw_base64"], validate=True)
            if row.get("packet_index") != line_number - 1 or row.get("raw_sha256") != _sha256(raw):
                raise CaptureIntegrityError(f"raw packet metadata mismatch at line {line_number}")
            recorder.ingest(raw, host_monotonic_ns=row["host_monotonic_ns"], host_utc=row["host_utc"])
        rebuilt, source_digest = recorder.build_record()
        if rebuilt != record or source_digest != manifest.get("source_sha256"):
            raise CaptureIntegrityError("raw packet replay does not reproduce the capture record")
        if recorder._packets_jsonl() != payloads["packets.jsonl"] or recorder._samples_csv(record) != payloads["samples.csv"]:
            raise CaptureIntegrityError("analysis export does not reproduce from the raw packet stream")
        return record
    except CaptureIntegrityError:
        raise
    except Exception as exc:
        raise CaptureIntegrityError(f"capture bundle is incomplete or malformed: {exc}") from exc
