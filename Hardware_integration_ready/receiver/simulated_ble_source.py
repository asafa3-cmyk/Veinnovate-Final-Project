"""simulated_ble_source.py — deterministic synthetic BLE DATA packet source.

Used by tests and by scripts/run_simulator.sh to exercise the decoder,
window assembler, and SQI gate without hardware. Determinism is achieved
with Python's stdlib `random.Random(seed)` — no numpy dependency, no wall
clock in the generated values, so repeated runs with the same seed produce
byte-identical packets.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from protocol_decoder import (
    MAX_SAMPLES_PER_PACKET,
    SensorConfigId,
    encode_data_packet,
)

DEFAULT_SEED = 42
DEFAULT_SAMPLE_RATE_HZ = 100


@dataclass
class SimulatorConfig:
    seed: int = DEFAULT_SEED
    device_id: int = 1
    sensor_session_id: int = 1
    sensor_config_id: int = int(SensorConfigId.DORSAL)
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ
    heart_rate_bpm: float = 72.0
    ir_dc: float = 180000.0
    ir_ac_amplitude: float = 900.0
    red_dc: float = 90000.0
    red_ac_amplitude: float = 400.0
    noise_amplitude: float = 50.0
    drop_packet_indices: frozenset[int] = frozenset()   # simulate loss
    duplicate_packet_indices: frozenset[int] = frozenset()  # simulate duplicates
    reorder_packet_indices: frozenset[int] = frozenset()    # simulate reordering


def _synthetic_sample(rng: random.Random, cfg: SimulatorConfig, t_s: float) -> tuple[int, int]:
    hr_hz = cfg.heart_rate_bpm / 60.0
    ir = cfg.ir_dc + cfg.ir_ac_amplitude * math.sin(2 * math.pi * hr_hz * t_s) + rng.uniform(
        -cfg.noise_amplitude, cfg.noise_amplitude
    )
    red = cfg.red_dc + cfg.red_ac_amplitude * math.sin(2 * math.pi * hr_hz * t_s) + rng.uniform(
        -cfg.noise_amplitude, cfg.noise_amplitude
    )
    ir_int = max(0, min((1 << 18) - 1, int(round(ir))))
    red_int = max(0, min((1 << 18) - 1, int(round(red))))
    return red_int, ir_int


def generate_packets(cfg: SimulatorConfig, total_samples: int) -> list[bytes]:
    """Returns a list of encoded DATA packets covering `total_samples`
    paired RED/IR samples, honoring cfg.{drop,duplicate,reorder}_packet_indices
    by packet sequence number (0-indexed in emission order before any
    reordering/duplication is applied)."""
    rng = random.Random(cfg.seed)
    packets: list[bytes] = []
    sample_index = 0
    packet_seq = 0
    device_ts_us = 0
    dt_s = 1.0 / cfg.sample_rate_hz

    while sample_index < total_samples:
        batch = min(MAX_SAMPLES_PER_PACKET, total_samples - sample_index)
        red_batch = []
        ir_batch = []
        for i in range(batch):
            t_s = (sample_index + i) * dt_s
            r, ir = _synthetic_sample(rng, cfg, t_s)
            red_batch.append(r)
            ir_batch.append(ir)

        packet = encode_data_packet(
            device_id=cfg.device_id,
            sensor_session_id=cfg.sensor_session_id,
            first_sample_index=sample_index,
            device_monotonic_timestamp_us=device_ts_us,
            effective_sample_rate_hz=cfg.sample_rate_hz,
            sensor_config_id=cfg.sensor_config_id,
            flags=0,
            fifo_overflow_count=0,
            red=red_batch,
            ir=ir_batch,
        )

        if packet_seq not in cfg.drop_packet_indices:
            packets.append(packet)
        if packet_seq in cfg.duplicate_packet_indices:
            packets.append(packet)

        sample_index += batch
        device_ts_us += int(batch * dt_s * 1_000_000)
        packet_seq += 1

    for seq in sorted(cfg.reorder_packet_indices):
        if 0 < seq < len(packets):
            packets[seq - 1], packets[seq] = packets[seq], packets[seq - 1]

    return packets


def golden_window_packets(seed: int = DEFAULT_SEED) -> list[bytes]:
    """A clean, complete 3000-sample (30s @ 100Hz) window with no loss,
    duplication, or reordering — the reference "golden capture" fixture for
    H3 parity-style tests in this bundle (window integrity only; frozen
    preprocessing parity itself is out of scope here, see README)."""
    cfg = SimulatorConfig(seed=seed)
    return generate_packets(cfg, total_samples=3000)
