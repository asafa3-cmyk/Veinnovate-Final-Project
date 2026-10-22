"""Opt-in V3 bench finalization; no physiological acceptance decision."""
from dataclasses import asdict, dataclass
import struct

from protocol_decoder import ProtocolError, crc16_ccitt, decode_data_packet
from window_assembler import SessionRingBuffer

FINAL_TIMEOUT_S = 2.0
FINAL_FMT = "<BBBQQIIBIIBBQH"
FINAL_SIZE = struct.calcsize(FINAL_FMT)
assert FINAL_SIZE == 48
WARMUP_FINAL_FMT = "<BBBQQIIBIIBBQIQH"
WARMUP_FINAL_SIZE = struct.calcsize(WARMUP_FINAL_FMT)
assert WARMUP_FINAL_SIZE == 60


@dataclass(frozen=True)
class FinalWitness:
    protocol_version: int
    packet_type: int
    witness_version: int
    hardware_uid: int
    acquisition_nonce: int
    sensor_session_id: int
    emitted_samples: int
    sensor_config_id: int
    fifo_overflow_count: int
    software_overflow_count: int
    acquisition_uncertainty: int
    outcome: int
    boundary_timestamp_us: int
    crc16: int = 0


@dataclass(frozen=True)
class WarmupFinalWitness(FinalWitness):
    warmup_emitted_samples: int = 0
    measurement_boundary_timestamp_us: int = 0


def decode_final_witness(raw):
    if len(raw) not in (FINAL_SIZE, WARMUP_FINAL_SIZE) or crc16_ccitt(raw[:-2]) != int.from_bytes(raw[-2:], "little"):
        raise ProtocolError("invalid FINAL size/CRC")
    if len(raw) == FINAL_SIZE:
        value = FinalWitness(*struct.unpack(FINAL_FMT, raw))
        expected_version = 1
    else:
        fields = struct.unpack(WARMUP_FINAL_FMT, raw)
        value = WarmupFinalWitness(*fields[:13], fields[-1],
            warmup_emitted_samples=fields[13], measurement_boundary_timestamp_us=fields[14])
        expected_version = 2
    if (value.protocol_version, value.packet_type, value.witness_version) != (3, 4, expected_version) or value.outcome not in (0, 1, 2):
        raise ProtocolError("unsupported FINAL contract")
    return value


class BenchCapture:
    witness_version = 1
    def __init__(self, *, uid, nonce, config=25):
        if not nonce:
            raise ValueError("nonzero unique nonce required")
        self.uid, self.nonce, self.config = uid, nonce, config
        self.ring = SessionRingBuffer()
        self.packets = []
        self.journal = []
        self.witness = None
        self.window = None
        self.deadline = None
        self.result = "DATA_PENDING"
        self.reason = None

    @property
    def terminal(self):
        return self.result in ("INTEGRITY_VERIFIED", "MEASUREMENT_REJECTED", "INTEGRITY_UNVERIFIED")

    def finish_unverified(self, reason):
        if not self.terminal:
            self.result, self.reason = "INTEGRITY_UNVERIFIED", reason

    def receive(self, channel, raw, now):
        # Always retain bytes, including wrong-device, malformed and late frames.
        entry = dict(channel=channel, host_monotonic_s=now, raw_hex=bytes(raw).hex())
        self.journal.append(entry)
        if self.terminal:
            entry["disposition"] = "AFTER_FINALIZATION"
            return
        # Timeout is absolute; a late frame cannot revive a closed attempt.
        self.tick(now)
        if self.terminal:
            entry["disposition"] = "LATE"
            return
        try:
            if channel == "DATA":
                packet = decode_data_packet(bytes(raw))
                if packet.protocol_version != 3 or packet.hardware_uid != self.uid or packet.sensor_config_id != self.config:
                    self.finish_unverified("DATA_IDENTITY_OR_VERSION_MISMATCH")
                    return
                if self.packets and (packet.sensor_session_id != self.packets[0].sensor_session_id or
                        packet.first_sample_index != self.packets[-1].first_sample_index + self.packets[-1].sample_count or
                        packet.device_monotonic_timestamp_us <= self.packets[-1].device_monotonic_timestamp_us):
                    self.finish_unverified("DATA_ORDER_SESSION_OR_CLOCK_FAULT")
                    return
                if not self.packets and packet.first_sample_index != 0:
                    self.finish_unverified("DATA_MISSING_START")
                    return
                self.packets.append(packet)
                self.ring.ingest(packet)
                self.window = self.ring.try_cut_window(0)
                if packet.fifo_overflow_count or packet.software_overflow_count or packet.flags & 1:
                    self.result, self.reason = "MEASUREMENT_REJECTED", "CAPTURE_ASSOCIATED_DATA_LOSS"
                    return
                count = sum(p.sample_count for p in self.packets)
                if count > 3000:
                    self.finish_unverified("DATA_OVERRUN")
                elif count == 3000:
                    self.result = "FINAL_PENDING"
                    self.deadline = self.deadline if self.deadline is not None else now + FINAL_TIMEOUT_S
            elif channel == "FINAL":
                value = decode_final_witness(bytes(raw))
                if value.witness_version != self.witness_version:
                    self.finish_unverified("FINAL_POLICY_VERSION_MISMATCH")
                    return
                if (value.hardware_uid, value.acquisition_nonce, value.sensor_config_id) != (self.uid, self.nonce, self.config):
                    entry["disposition"] = "UNATTRIBUTED_WITNESS"
                    return
                if self.packets and value.sensor_session_id != self.packets[0].sensor_session_id:
                    entry["disposition"] = "WRONG_SESSION"
                    return
                if self.witness is not None and value != self.witness:
                    self.finish_unverified("CONFLICTING_FINAL_WITNESS")
                    return
                self.witness = value
                self.deadline = self.deadline if self.deadline is not None else now + FINAL_TIMEOUT_S
            else:
                # Legacy STATUS is retained, but lacks acquisition attribution.
                entry["disposition"] = "STATUS_NOT_A_FINAL_WITNESS"
                return
        except ProtocolError:
            self.finish_unverified("MALFORMED_FRAME")
            return
        self._resolve()

    def _resolve(self):
        w = self.witness
        if w is None or self.terminal:
            return
        if not self.packets:
            # UID + fresh command nonce still identify an acquisition that
            # stopped before its first DATA notification.
            if w.fifo_overflow_count or w.software_overflow_count:
                self.result, self.reason = "MEASUREMENT_REJECTED", "CAPTURE_ASSOCIATED_LOSS"
            elif w.acquisition_uncertainty or w.outcome != 0:
                self.finish_unverified("INCOMPLETE_OR_UNCERTAIN_ACQUISITION")
            return
        if w.sensor_session_id != self.packets[0].sensor_session_id:
            self.witness = None
            return
        last = self.packets[-1]
        if (w.boundary_timestamp_us < last.device_monotonic_timestamp_us or
            w.fifo_overflow_count < max(p.fifo_overflow_count for p in self.packets) or
            w.software_overflow_count < max(p.software_overflow_count for p in self.packets)):
            self.finish_unverified("FINAL_COUNTER_OR_CLOCK_REGRESSION")
        elif w.fifo_overflow_count or w.software_overflow_count:
            self.result, self.reason = "MEASUREMENT_REJECTED", "CAPTURE_ASSOCIATED_LOSS"
        elif w.acquisition_uncertainty or w.outcome == 1 or any(p.acquisition_uncertainty for p in self.packets):
            self.finish_unverified("ACQUISITION_CONTINUITY_UNCERTAIN")
        elif w.outcome != 0 or w.emitted_samples != 3000:
            self.finish_unverified("INCOMPLETE_ACQUISITION")
        elif self.window is not None:
            if not self.window.integrity.is_complete:
                self.finish_unverified("DATA_WINDOW_INCOMPLETE")
            else:
                self.result, self.reason = "INTEGRITY_VERIFIED", "FINAL_WITNESS_MATCHED"

    def tick(self, now):
        if not self.terminal and self.deadline is not None and now >= self.deadline:
            self.finish_unverified("FINAL_WITNESS_TIMEOUT_OR_INCOMPLETE_DATA")

    def record(self):
        return dict(schema_version="bench-integrity-1.0", result=self.result, reason=self.reason,
                    hardware_uid=f"{self.uid:016X}", acquisition_nonce=f"{self.nonce:016X}",
                    physical_sampling_verified=False, measurement_use_permitted=False,
                    timestamp_semantics="PACKET_CONSTRUCTION_NOT_OPTICAL",
                    final_timeout_s=FINAL_TIMEOUT_S,
                    final_witness=asdict(self.witness) if self.witness else None,
                    red=[v for p in self.packets for v in p.red],
                    ir=[v for p in self.packets for v in p.ir],
                    packets=[asdict(p) for p in self.packets])


class WarmupBenchCapture(BenchCapture):
    """Policy 1 keeps phase-local raw indices and a single cumulative loss epoch."""
    witness_version = 2
    warmup_pairs = 500
    warmup_deadline_s = 8.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.warmup_packets = []
        self.warmup_count = 0

    def receive(self, channel, raw, now):
        from protocol_decoder import PacketType
        if channel in ("DATA", "WARMUP") and len(raw) > 1 and raw[1] == PacketType.WARMUP:
            entry = dict(channel="WARMUP", phase="warmup", host_monotonic_s=now, raw_hex=bytes(raw).hex())
            self.journal.append(entry)
            self.tick(now)
            if self.terminal:
                entry["disposition"] = "AFTER_FINALIZATION"
                return
            try:
                p = decode_data_packet(bytes(raw), expected_type=PacketType.WARMUP)
                if (p.protocol_version != 3 or p.hardware_uid != self.uid or
                    p.sensor_config_id != self.config or p.effective_sample_rate_hz != 100):
                    raise ProtocolError("warmup identity/config/rate")
                if self.packets or p.first_sample_index != self.warmup_count:
                    raise ProtocolError("warmup phase/order")
                if self.warmup_packets and (p.sensor_session_id != self.warmup_packets[0].sensor_session_id or
                    p.device_monotonic_timestamp_us <= self.warmup_packets[-1].device_monotonic_timestamp_us):
                    raise ProtocolError("warmup session/clock")
                self.warmup_packets.append(p)
                self.warmup_count += p.sample_count
                if self.warmup_count > self.warmup_pairs:
                    raise ProtocolError("warmup overrun")
                if p.fifo_overflow_count or p.software_overflow_count or p.flags & 1:
                    self.result, self.reason = "MEASUREMENT_REJECTED", "WARMUP_ASSOCIATED_LOSS"
                elif p.acquisition_uncertainty:
                    self.finish_unverified("WARMUP_CONTINUITY_UNCERTAIN")
            except ProtocolError as exc:
                self.finish_unverified("WARMUP_FRAME_FAULT: " + str(exc))
            self._resolve()
            return
        if channel == "DATA" and not self.terminal:
            if self.warmup_count != self.warmup_pairs:
                self.journal.append(dict(channel="DATA", phase="measurement", host_monotonic_s=now, raw_hex=bytes(raw).hex()))
                self.finish_unverified("MEASUREMENT_BEFORE_WARMUP_COMPLETE")
                return
            try:
                p = decode_data_packet(bytes(raw))
                if (p.sensor_session_id != self.warmup_packets[0].sensor_session_id or
                    p.device_monotonic_timestamp_us <= self.warmup_packets[-1].device_monotonic_timestamp_us):
                    raise ProtocolError("phase boundary session/clock")
            except ProtocolError as exc:
                self.journal.append(dict(channel="DATA", phase="measurement", host_monotonic_s=now, raw_hex=bytes(raw).hex()))
                self.finish_unverified("PHASE_BOUNDARY_FAULT: " + str(exc))
                return
        super().receive(channel, raw, now)
        if channel == "DATA":
            self.journal[-1]["phase"] = "measurement"

    def _resolve(self):
        w = self.witness
        if w and not self.terminal and w.outcome == 0 and not (w.fifo_overflow_count or w.software_overflow_count or w.acquisition_uncertainty):
            if w.warmup_emitted_samples != self.warmup_pairs:
                self.finish_unverified("FINAL_WARMUP_COUNT_MISMATCH")
                return
            if self.warmup_packets and w.measurement_boundary_timestamp_us < self.warmup_packets[-1].device_monotonic_timestamp_us:
                self.finish_unverified("FINAL_PHASE_BOUNDARY_REGRESSION")
                return
            if self.packets and w.measurement_boundary_timestamp_us > self.packets[0].device_monotonic_timestamp_us:
                self.finish_unverified("FINAL_PHASE_BOUNDARY_AFTER_MEASUREMENT")
                return
        super()._resolve()

    def record(self):
        record = super().record()
        record.update(schema_version="bench-integrity-2.0", warmup_policy_version="1.0.0",
            phase_index_semantics="separate zero-based indices; one UID/session/config/loss epoch",
            warmup={"phase": "warmup", "expected_pairs": self.warmup_pairs,
                    "red": [v for p in self.warmup_packets for v in p.red],
                    "ir": [v for p in self.warmup_packets for v in p.ir],
                    "packets": [asdict(p) for p in self.warmup_packets]},
            measurement={"phase": "measurement", "expected_pairs": 3000,
                         "first_sample_index": 0})
        return record
