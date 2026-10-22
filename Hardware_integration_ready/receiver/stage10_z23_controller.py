"""Frozen Z23 POC exposure policy loader and bounded Stage 10 controller.

Cardiac quality is deliberately outside this module.  It gates only exposure,
transport integrity, and the exact preflight -> fixed 3000-pair transition.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from capture_quality_metrics import mean
from protocol_decoder import Flags, decode_data_packet
from window_assembler import AssembledWindow, SessionRingBuffer

import prototype_continuous as _proto


BUNDLE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BUNDLE_ROOT.parent
POLICY_PATH = BUNDLE_ROOT / "config" / "stage10_z23_poc_exposure_policy_v1.json"
CALIBRATIONS_PATH = BUNDLE_ROOT / "config" / "stage10_z23_poc_same_pair_calibrations_v1.json"
VN001_UID = "A90AFDAF672D66F1"
# Same-pair no-contact references are device-specific optical evidence: one
# retained registry per hardware UID. A device without a registry here has no
# reference and must fail closed; another unit's values are never substituted.
SAME_PAIR_REGISTRIES = {VN001_UID: CALIBRATIONS_PATH}
AUTHORIZATION_PATH = PROJECT_ROOT / "stage9_external_evidence" / "STAGE10_Z23_POC_BEGIN_AUTHORIZATION.json"
EXPECTED_POLICY_SHA256 = "b66e9c5ffd8264a7d12e2f42195a249d30e3f0f0d0144fd2f6e1bfe94f189b2b"
EXPECTED_REVIEW_SHA256 = "8809cd5b9d2a81d8606300ae386cd040193c6e2a9ebcb961f944a3f68d19f883"
EXPECTED_PLAN_SHA256 = "78198cd5ddce4f140f7e2bbed0882c50f1d4bb2043f75ebee2d03851c116de42"


class Stage10PolicyError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, order=True)
class ExposurePair:
    red_pa: int
    ir_pa: int

    @classmethod
    def from_hex(cls, red: str, ir: str) -> "ExposurePair":
        return cls(int(red, 16), int(ir, 16))

    def to_dict(self) -> dict:
        return {"red_pa": f"0x{self.red_pa:02X}", "ir_pa": f"0x{self.ir_pa:02X}"}


@dataclass(frozen=True)
class SamePairCalibration:
    config_id: int
    pair: ExposurePair
    red_no_contact_dc: float
    ir_no_contact_dc: float
    multiplier: float
    source: str
    source_sha256: str


@dataclass(frozen=True)
class LoadedStage10Policy:
    payload: dict
    policy_sha256: str
    authorization: dict
    pairs_to_config: dict[ExposurePair, int]
    calibrations: dict[ExposurePair, SamePairCalibration]
    hardware_uid: str
    short_code: str

    @property
    def seed(self) -> ExposurePair:
        value = self.payload["initial_seed"]
        return ExposurePair.from_hex(value["red_pa"], value["ir_pa"])


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stage10PolicyError(f"cannot load {path.name}: {exc}") from exc


def _resolve_declared(base: Path, value: str) -> Path:
    return (base.parent / value).resolve()


def load_stage10_z23_policy(
    policy_path: Path = POLICY_PATH,
    authorization_path: Path = AUTHORIZATION_PATH,
    calibrations_path: Path | None = None,
    hardware_uid: str = VN001_UID,
) -> LoadedStage10Policy:
    """Load only the reviewed immutable policy plus separate owner approval.

    The policy's embedded ``stage10_runtime_authorized=false`` is required.
    Authorization is additive provenance; it never rewrites or bypasses that
    historical field.  The same-pair registry is selected by ``hardware_uid``
    and must declare that exact UID.
    """
    hardware_uid = hardware_uid.upper()
    if calibrations_path is None:
        if hardware_uid not in SAME_PAIR_REGISTRIES:
            raise Stage10PolicyError(
                f"no same-pair no-contact reference registry for hardware UID {hardware_uid}"
            )
        calibrations_path = SAME_PAIR_REGISTRIES[hardware_uid]
    policy_path = policy_path.resolve()
    authorization_path = authorization_path.resolve()
    calibrations_path = calibrations_path.resolve()
    if sha256_file(policy_path) != EXPECTED_POLICY_SHA256:
        raise Stage10PolicyError("immutable Stage 10 policy hash mismatch")
    policy = _json(policy_path)
    allowed_rows = [
        (row.get("red_pa"), row.get("ir_pa")) for row in policy.get("allowed_pairs", [])
    ]
    expected_pairs = [
        ("0x1F", "0x1F"), ("0x1F", "0x3E"), ("0x1F", "0x5E"),
        ("0x3E", "0x3E"), ("0x5E", "0x5E"),
    ]
    fixed = policy.get("fixed_acquisition", {})
    preflight = policy.get("preflight", {})
    if (
        policy.get("schema_version") != "1.0.0"
        or policy.get("policy_id") != "STAGE10-Z23-POC-EXPOSURE-v1"
        or policy.get("scope") != "Z23_ENGINEERING_POC_ONLY"
        or policy.get("stage10_runtime_authorized") is not False
        or policy.get("unobserved_combinations_forbidden") is not True
        or allowed_rows != expected_pairs
        or fixed != {
            "adc_range_na": 16384, "sample_rate_sps": 400, "sample_avg": 4,
            "effective_sample_rate_hz": 100, "pulse_width_us": 411,
            "led_mode": 2, "measurement_pairs": 3000,
            "measurement_duration_s": 30,
        }
        or preflight.get("total_deadline_s") != 5.0
        or preflight.get("evaluation_pairs") != 100
        or preflight.get("block_pairs") != 50
        or preflight.get("maximum_adjustment_events") != 2
        or preflight.get("maximum_evaluations") != 3
        or policy.get("measurement_freeze", {}).get("no_restart_at_preflight_to_measurement_transition") is not True
    ):
        raise Stage10PolicyError("Stage 10 policy semantics mismatch")

    auth = _json(authorization_path)
    declared_policy = auth.get("policy", {})
    declared_review = auth.get("independent_review", {})
    declared_plan = auth.get("verification_plan", {})
    review_path = _resolve_declared(authorization_path, declared_review.get("path", ""))
    plan_path = _resolve_declared(authorization_path, declared_plan.get("path", ""))
    authorization = auth.get("authorization", {})
    exit_state = auth.get("legacy_and_exit_state", {})
    if (
        auth.get("record_type") != "stage10_z23_poc_begin_authorization"
        or auth.get("scope") != policy["scope"]
        or declared_policy.get("sha256") != EXPECTED_POLICY_SHA256
        or _resolve_declared(authorization_path, declared_policy.get("path", "")) != policy_path
        or declared_review.get("sha256") != EXPECTED_REVIEW_SHA256
        or sha256_file(review_path) != EXPECTED_REVIEW_SHA256
        or declared_plan.get("sha256") != EXPECTED_PLAN_SHA256
        or sha256_file(plan_path) != EXPECTED_PLAN_SHA256
        or authorization.get("begin_stage10_implementation") is not True
        or authorization.get("execute_frozen_finite_verification_plan") is not True
        or authorization.get("runtime_use_during_scoped_verification") is not True
        or authorization.get("automatic_extra_captures") != 0
        or exit_state.get("immutable_policy_runtime_authorization_field_remains_false") is not True
        or exit_state.get("stage10_z23_poc_pass") is not False
        or auth.get("not_a_signature") is not True
    ):
        raise Stage10PolicyError("Stage 10 begin authorization is absent or mismatched")
    review = _json(review_path)
    if (
        review.get("exit_gate", {}).get("verdict") != "Z23_POC_POLICY_READY"
        or review.get("exit_gate", {}).get("stage10_authorized") is not False
    ):
        raise Stage10PolicyError("independent policy-ready review is absent")

    registry = _json(calibrations_path)
    if (
        registry.get("record_type") != "stage10_z23_poc_same_pair_calibration_registry"
        or registry.get("policy_id") != policy["policy_id"]
        or registry.get("hardware_uid") != hardware_uid
        or not registry.get("short_code")
        or registry.get("minimum_contact_dc_multiplier") != 3.0
    ):
        raise Stage10PolicyError("same-pair calibration registry mismatch")
    configs = [24, 25, 26, 17, 18]
    pairs_to_config = {
        ExposurePair.from_hex(red, ir): config
        for (red, ir), config in zip(expected_pairs, configs)
    }
    calibrations: dict[ExposurePair, SamePairCalibration] = {}
    for row in registry.get("pairs", []):
        pair = ExposurePair.from_hex(row["red_pa"], row["ir_pa"])
        source = _resolve_declared(calibrations_path, row["source"])
        if pair not in pairs_to_config or row["config_id"] != pairs_to_config[pair]:
            raise Stage10PolicyError("unallowed or misbound same-pair calibration")
        if sha256_file(source) != row["source_sha256"]:
            raise Stage10PolicyError("same-pair calibration source hash mismatch")
        source_payload = _json(source)
        baseline = source_payload.get("preflight_contact_baseline") or source_payload.get("installed_preflight_baseline")
        if (
            source_payload.get("config_id") != row["config_id"]
            or baseline is None
            or float(baseline["red_no_contact_dc"]) != float(row["red_no_contact_dc"])
            or float(baseline["ir_no_contact_dc"]) != float(row["ir_no_contact_dc"])
            or float(baseline["min_contact_dc_multiplier"]) != 3.0
        ):
            raise Stage10PolicyError("same-pair calibration values do not match retained evidence")
        calibrations[pair] = SamePairCalibration(
            config_id=row["config_id"], pair=pair,
            red_no_contact_dc=float(row["red_no_contact_dc"]),
            ir_no_contact_dc=float(row["ir_no_contact_dc"]),
            multiplier=float(registry["minimum_contact_dc_multiplier"]),
            source=row["source"], source_sha256=row["source_sha256"],
        )
    if set(calibrations) != set(pairs_to_config):
        raise Stage10PolicyError("all five exact-pair calibrations are required")
    return LoadedStage10Policy(policy, EXPECTED_POLICY_SHA256, auth, pairs_to_config, calibrations,
                               hardware_uid, registry["short_code"])


@dataclass(frozen=True)
class Evaluation:
    red_dc: float
    ir_dc: float
    red_max: int
    ir_max: int
    red_shift_pct: float
    ir_shift_pct: float
    samples: int

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str | None
    pair: ExposurePair
    next_pair: ExposurePair | None
    evaluation: Evaluation


class BoundedExposureController:
    def __init__(self, loaded: LoadedStage10Policy, *, clock=time.monotonic) -> None:
        self.loaded = loaded
        self.clock = clock
        self.started = clock()
        self.pair = loaded.seed
        self.evaluated: list[ExposurePair] = []
        self.channel_direction: dict[str, int] = {}
        self.adjustments = 0

    def _deadline_s(self) -> float:
        """Frozen policy deadline (5 s); prototype-continuous mode allows 15 s.
        The immutable policy file and its hash are not modified."""
        frozen = float(self.loaded.payload["preflight"]["total_deadline_s"])
        return max(frozen, _proto.CONFIG["preflight_deadline_s"]) if _proto.enabled() else frozen

    def _adjacent(self, value: int, direction: int) -> int | None:
        row = self.loaded.payload["adjacent_levels"][f"0x{value:02X}"]
        target = row["up" if direction > 0 else "down"]
        return None if target is None else int(target, 16)

    def evaluate(
        self, red: list[int], ir: list[int], *, fifo_overflow: bool = False,
        integrity_valid: bool = True,
    ) -> Decision:
        pf = self.loaded.payload["preflight"]
        if self.clock() - self.started > self._deadline_s():
            raise Stage10PolicyError("PREFLIGHT_TIMEOUT")
        if len(self.evaluated) >= pf["maximum_evaluations"]:
            raise Stage10PolicyError("NON_CONVERGENCE")
        if self.pair in self.evaluated:
            raise Stage10PolicyError("OSCILLATION")
        if self.pair not in self.loaded.calibrations:
            raise Stage10PolicyError("NO_SAME_PAIR_CALIBRATION")
        if not integrity_valid:
            raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE")
        if fifo_overflow:
            raise Stage10PolicyError("FIFO_OVERFLOW")
        if len(red) != pf["evaluation_pairs"] or len(ir) != pf["evaluation_pairs"]:
            raise Stage10PolicyError("NON_CONVERGENCE")
        split = pf["block_pairs"]
        red_blocks, ir_blocks = (red[:split], red[split:]), (ir[:split], ir[split:])
        def shift(blocks: tuple[list[int], list[int]]) -> float:
            first, second = mean(blocks[0]), mean(blocks[1])
            return float("inf") if first == 0 else abs(second - first) / abs(first) * 100
        ev = Evaluation(mean(red), mean(ir), max(red), max(ir), shift(red_blocks), shift(ir_blocks), len(red))
        self.evaluated.append(self.pair)
        if ev.red_max >= pf["clip_value"] or ev.ir_max >= pf["clip_value"]:
            raise Stage10PolicyError("CLIPPING")
        if ev.red_shift_pct > pf["maximum_dc_shift_pct"] or ev.ir_shift_pct > pf["maximum_dc_shift_pct"]:
            raise Stage10PolicyError("NON_CONVERGENCE")
        cal = self.loaded.calibrations[self.pair]
        floors = (
            max(pf["minimum_usable_dc_counts"], pf["minimum_contact_dc_multiplier"] * cal.red_no_contact_dc),
            max(pf["minimum_usable_dc_counts"], pf["minimum_contact_dc_multiplier"] * cal.ir_no_contact_dc),
        )
        directions: dict[str, int] = {}
        for channel, dc, raw_max, floor in (
            ("red", ev.red_dc, ev.red_max, floors[0]),
            ("ir", ev.ir_dc, ev.ir_max, floors[1]),
        ):
            if dc < floor:
                directions[channel] = 1
            elif dc > pf["maximum_target_dc_counts"] or raw_max >= pf["near_saturation_raw_count"]:
                directions[channel] = -1
        if not directions:
            return Decision("CONVERGED", None, self.pair, None, ev)
        if self.adjustments >= pf["maximum_adjustment_events"]:
            raise Stage10PolicyError("NON_CONVERGENCE")
        red, ir = self.pair.red_pa, self.pair.ir_pa
        for channel, direction in directions.items():
            previous = self.channel_direction.get(channel)
            if previous is not None and previous != direction:
                raise Stage10PolicyError("OSCILLATION")
            self.channel_direction[channel] = direction
            if channel == "red":
                target = self._adjacent(red, direction)
                if target is None:
                    raise Stage10PolicyError("NO_CONTACT_OR_WEAK_SIGNAL_AT_BOUND" if direction > 0 else "NEAR_SATURATION_AT_BOUND")
                red = target
            else:
                target = self._adjacent(ir, direction)
                if target is None:
                    raise Stage10PolicyError("NO_CONTACT_OR_WEAK_SIGNAL_AT_BOUND" if direction > 0 else "NEAR_SATURATION_AT_BOUND")
                ir = target
        next_pair = ExposurePair(red, ir)
        if next_pair not in self.loaded.pairs_to_config:
            raise Stage10PolicyError("UNALLOWED_PAIR_REQUIRED")
        if next_pair in self.evaluated:
            raise Stage10PolicyError("OSCILLATION")
        self.adjustments += 1
        old = self.pair
        self.pair = next_pair
        return Decision("ADJUST", None, old, next_pair, ev)

    def assert_deadline(self) -> float:
        elapsed = self.clock() - self.started
        if elapsed > self._deadline_s():
            raise Stage10PolicyError("PREFLIGHT_TIMEOUT")
        return elapsed


class Stage10PacketStream:
    """Validate live packets while separating preflight from the 30 s window."""

    def __init__(self, controller: BoundedExposureController, *, hardware_uid: str, evaluation_transform=None) -> None:
        self.controller = controller
        self.hardware_uid = hardware_uid.upper()
        self.current_pair = controller.pair
        self.current_config = controller.loaded.pairs_to_config[self.current_pair]
        self.session_id: int | None = None
        self.next_index = 0
        self.last_device_us: int | None = None
        self.last_host_ns: int | None = None
        self.red: list[int] = []
        self.ir: list[int] = []
        self.final_raw_packets: list[bytes] = []
        self.ring = SessionRingBuffer()
        self.converged = False
        self.measurement_start_index: int | None = None
        self.measurement_first_host_ns: int | None = None
        self.measurement_last_host_ns: int | None = None
        self.evaluation_transform = evaluation_transform
        self.evaluation_transform_used = False

    def reset_for_adjustment(self, pair: ExposurePair) -> None:
        if self.converged:
            raise Stage10PolicyError("configuration change invalidates window")
        if pair not in self.controller.loaded.pairs_to_config:
            raise Stage10PolicyError("UNALLOWED_PAIR_REQUIRED")
        self.current_pair = pair
        self.current_config = self.controller.loaded.pairs_to_config[pair]
        self.session_id = None
        self.next_index = 0
        self.last_device_us = None
        self.last_host_ns = None
        self.red = []
        self.ir = []
        self.final_raw_packets = []
        self.ring.reset()

    def ingest(self, raw: bytes, *, host_monotonic_ns: int) -> Decision | None:
        try:
            packet = decode_data_packet(raw)
        except Exception as exc:
            raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE") from exc
        if (
            f"{packet.hardware_uid:016X}" != self.hardware_uid
            or packet.sensor_config_id != self.current_config
            or packet.effective_sample_rate_hz != 100
            or (self.session_id is not None and packet.sensor_session_id != self.session_id)
            or (self.session_id is not None and packet.flags & int(Flags.SESSION_RESET))
            or packet.first_sample_index != self.next_index
            or (self.last_device_us is not None and packet.device_monotonic_timestamp_us <= self.last_device_us)
            # prototype mode: host-clock jitter alone is recorded, not fatal (device index/timestamps still enforced)
            or (self.last_host_ns is not None and host_monotonic_ns <= self.last_host_ns and not _proto.enabled())
        ):
            raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE")
        if self.last_host_ns is not None and host_monotonic_ns <= self.last_host_ns:
            self.host_clock_jitter_events = getattr(self, "host_clock_jitter_events", 0) + 1
        if packet.protocol_version != 3 or packet.acquisition_uncertainty or packet.software_overflow_count:
            raise Stage10PolicyError("ACQUISITION_CONTINUITY_UNCERTAIN")
        if packet.flags & int(Flags.FIFO_OVERFLOW_SINCE_LAST) or packet.fifo_overflow_count:
            raise Stage10PolicyError("FIFO_OVERFLOW")
        if packet.flags & int(Flags.CLIPPED_SAMPLE_PRESENT) or any(
            value >= 262143 for value in packet.red + packet.ir
        ):
            raise Stage10PolicyError("CLIPPING")
        self.session_id = packet.sensor_session_id
        self.next_index += packet.sample_count
        self.last_device_us = packet.device_monotonic_timestamp_us
        self.last_host_ns = host_monotonic_ns
        self.red.extend(packet.red)
        self.ir.extend(packet.ir)
        self.ring.ingest(packet)
        self.final_raw_packets.append(bytes(raw))
        if self.converged:
            if packet.last_sample_index >= self.measurement_start_index:  # type: ignore[operator]
                if self.measurement_first_host_ns is None:
                    self.measurement_first_host_ns = host_monotonic_ns
                self.measurement_last_host_ns = host_monotonic_ns
            return None
        if len(self.red) < 100:
            return None
        eval_red, eval_ir = self.red[:100], self.ir[:100]
        if self.evaluation_transform is not None and not self.evaluation_transform_used:
            eval_red, eval_ir = self.evaluation_transform(list(eval_red), list(eval_ir))
            self.evaluation_transform_used = True
        decision = self.controller.evaluate(eval_red, eval_ir)
        if decision.status == "CONVERGED":
            self.converged = True
            self.measurement_start_index = 100
            # Retain/forward the entire final-session packet stream.  The
            # downstream ring cuts at index 100, so a notification straddling
            # the boundary is preserved byte-for-byte without contaminating
            # the measurement window or restarting the sensor.
        return decision

    @property
    def measurement_complete(self) -> bool:
        return bool(
            self.converged
            and self.measurement_start_index is not None
            and self.next_index >= self.measurement_start_index + 3000
        )

    def measurement_window(self) -> AssembledWindow:
        if not self.measurement_complete or self.measurement_start_index is None:
            raise Stage10PolicyError("measurement window incomplete")
        window = self.ring.try_cut_window(self.measurement_start_index)
        if window is None or not window.integrity.is_complete:
            raise Stage10PolicyError("IDENTITY_CONFIG_SESSION_RATE_OR_TIMING_FAILURE")
        if window.sensor_config_id != self.current_config:
            raise Stage10PolicyError("configuration change invalidates window")
        return window


async def deliver_verified_window(stream: Stage10PacketStream, orchestrator) -> None:
    """Continue through the existing SQI/inference/dashboard output path."""
    import time
    offset = time.time() - time.monotonic_ns() / 1e9
    orchestrator.baseline_interval = (
        offset + stream.measurement_first_host_ns / 1e9 if stream.measurement_first_host_ns is not None else None,
        offset + stream.measurement_last_host_ns / 1e9 if stream.measurement_last_host_ns is not None else None,
    )
    await orchestrator.handle_stage10_verified_window(
        stream.measurement_window(),
        policy_id=stream.controller.loaded.payload["policy_id"],
        policy_sha256=stream.controller.loaded.policy_sha256,
    )
