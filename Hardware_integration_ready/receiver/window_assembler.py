"""window_assembler.py — reconstructs ordered 30-second/3000-sample windows
from decoded DATA packets, detecting loss, duplicates, reordering, and
session/config changes along the way (spec §4, §5).

This module never applies model preprocessing (no filtering, no resampling,
no normalization) — it only assembles and reports on raw RED/IR integrity.
Preprocessing is out of scope for this hardware-edge bundle by design (spec
§1/§3): it lives in the sibling VeinNet_integration_ready component.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from protocol_decoder import DataPacket

WINDOW_SAMPLE_COUNT = 3000          # 30s * 100Hz
EFFECTIVE_RATE_HZ = 100


@dataclass
class WindowIntegrity:
    expected_samples: int
    received_samples: int
    duplicate_samples: int
    lost_samples: int
    out_of_order_packets: int
    session_changed: bool
    config_changed: bool
    fifo_overflow_count: int  # cumulative overflow-counter delta observed while buffering this window

    software_overflow_count: int = 0
    acquisition_uncertainty: int = 0
    loss_evidence_available: bool = False

    @property
    def fifo_overflow_detected(self) -> bool:
        return self.fifo_overflow_count > 0

    @property
    def loss_fraction(self) -> float:
        if self.expected_samples == 0:
            return 1.0
        return self.lost_samples / self.expected_samples

    @property
    def is_complete(self) -> bool:
        return (
            self.received_samples == self.expected_samples
            and self.duplicate_samples == 0
            and self.out_of_order_packets == 0
            and not self.session_changed
            and not self.config_changed
            and not self.fifo_overflow_detected
            and self.software_overflow_count == 0
            and self.acquisition_uncertainty == 0
            and self.loss_evidence_available
        )


@dataclass
class AssembledWindow:
    sensor_session_id: int
    sensor_config_id: int
    device_id: int
    first_sample_index: int
    last_sample_index: int
    red: list[int]
    ir: list[int]
    sample_indices: list[int]
    integrity: WindowIntegrity


class SessionRingBuffer:
    """Accumulates DATA packets for a single sensor_session_id into an
    ordered, gap-aware sample map, and can cut a window once
    WINDOW_SAMPLE_COUNT contiguous samples exist within one session.

    A packet belonging to a different sensor_session_id than the one
    currently buffered starts a new buffer — samples are never mixed across
    sessions (spec §4 "Reconnect without mixing samples from different
    sessions").
    """

    def __init__(self) -> None:
        self._session_id: int | None = None
        self._config_id: int | None = None
        self._device_id: int | None = None
        self._samples: dict[int, tuple[int, int]] = {}  # index -> (red, ir)
        self._duplicate_count = 0
        self._out_of_order_count = 0
        self._overflow_at_start: int | None = None
        self._latest_overflow_count = 0
        self._latest_software_count = 0
        self._acquisition_uncertainty = 0
        self._loss_evidence_available = True
        self._last_construction_timestamp = None
        self._session_changed_mid_buffer = False
        self._config_changed_mid_buffer = False
        self._last_seen_index: int | None = None

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    def ingest(self, packet: DataPacket) -> None:
        if self._session_id is None:
            self._session_id = packet.sensor_session_id
            self._config_id = packet.sensor_config_id
            self._device_id = packet.device_id
        elif packet.sensor_session_id != self._session_id:
            self._session_changed_mid_buffer = True
            return  # caller should reset() and re-ingest into a fresh buffer
        elif packet.sensor_config_id != self._config_id:
            self._config_changed_mid_buffer = True

        # Counters cover the acquisition epoch, including loss before the
        # first received packet. A nonzero baseline must never be subtracted.
        if packet.protocol_version != 3:
            self._loss_evidence_available = False
        self._acquisition_uncertainty |= packet.acquisition_uncertainty
        if (packet.fifo_overflow_count < self._latest_overflow_count or
                packet.software_overflow_count < self._latest_software_count):
            self._acquisition_uncertainty |= 8
        if packet.device_id != self._device_id or packet.effective_sample_rate_hz != EFFECTIVE_RATE_HZ:
            self._acquisition_uncertainty |= 1
        # Retransmitted/old indices describe an existing construction event.
        # Their duplicate/order faults invalidate the affected window below,
        # but must not invent an acquisition-clock fault for the next window.
        # A newly emitted packet must still advance the construction clock.
        if self._last_seen_index is None or packet.first_sample_index > self._last_seen_index:
            if (self._last_construction_timestamp is not None and
                    packet.device_monotonic_timestamp_us <= self._last_construction_timestamp):
                self._acquisition_uncertainty |= 32
            self._last_construction_timestamp = packet.device_monotonic_timestamp_us
        self._latest_software_count = max(self._latest_software_count, packet.software_overflow_count)
        if self._overflow_at_start is None:
            self._overflow_at_start = packet.fifo_overflow_count
        self._latest_overflow_count = max(self._latest_overflow_count, packet.fifo_overflow_count)

        if self._last_seen_index is not None and packet.first_sample_index < self._last_seen_index:
            self._out_of_order_count += 1

        for offset, (r, i) in enumerate(zip(packet.red, packet.ir)):
            idx = packet.first_sample_index + offset
            if idx in self._samples:
                self._duplicate_count += 1
                continue
            self._samples[idx] = (r, i)
            if self._last_seen_index is None or idx > self._last_seen_index:
                self._last_seen_index = idx

    def try_cut_window(self, start_index: int) -> AssembledWindow | None:
        """Attempt to cut a complete WINDOW_SAMPLE_COUNT window starting at
        start_index. Returns None if not enough samples are buffered yet."""
        indices = range(start_index, start_index + WINDOW_SAMPLE_COUNT)
        present = [idx for idx in indices if idx in self._samples]
        if len(present) < WINDOW_SAMPLE_COUNT:
            lost = WINDOW_SAMPLE_COUNT - len(present)
        else:
            lost = 0

        if not present:
            return None
        max_present = max(present)
        # Only cut once we've either seen every index in range, or we've
        # moved well past the window (so remaining gaps are real loss, not
        # just not-arrived-yet).
        if max_present < start_index + WINDOW_SAMPLE_COUNT - 1 and (self._last_seen_index or 0) < start_index + WINDOW_SAMPLE_COUNT - 1:
            return None

        red = []
        ir = []
        sample_indices = []
        for idx in indices:
            if idx in self._samples:
                r, i = self._samples[idx]
                red.append(r)
                ir.append(i)
                sample_indices.append(idx)

        overflow_delta = 0
        if self._overflow_at_start is not None:
            overflow_delta = self._latest_overflow_count

        integrity = WindowIntegrity(
            expected_samples=WINDOW_SAMPLE_COUNT,
            received_samples=len(sample_indices),
            duplicate_samples=self._duplicate_count,
            lost_samples=lost,
            out_of_order_packets=self._out_of_order_count,
            session_changed=self._session_changed_mid_buffer,
            config_changed=self._config_changed_mid_buffer,
            fifo_overflow_count=overflow_delta,
            software_overflow_count=self._latest_software_count,
            acquisition_uncertainty=self._acquisition_uncertainty,
            loss_evidence_available=self._loss_evidence_available,
        )

        return AssembledWindow(
            sensor_session_id=self._session_id,  # type: ignore[arg-type]
            sensor_config_id=self._config_id,  # type: ignore[arg-type]
            device_id=self._device_id,  # type: ignore[arg-type]
            first_sample_index=start_index,
            last_sample_index=start_index + WINDOW_SAMPLE_COUNT - 1,
            red=red,
            ir=ir,
            sample_indices=sample_indices,
            integrity=integrity,
        )

    def prune_before(self, index: int) -> None:
        """Drop buffered samples below `index` once they can no longer
        belong to any future window (keeps memory bounded for long runs)."""
        for idx in [k for k in self._samples if k < index]:
            del self._samples[idx]
        self._duplicate_count = 0
        self._out_of_order_count = 0
        self._overflow_at_start = self._latest_overflow_count
        self._config_changed_mid_buffer = False
        self._session_changed_mid_buffer = False
