# Stage 7 — QA Capture Record Format Specification

> Legacy metric contract v1.0.0. It remains authoritative for replaying
> historical artifacts only. New Stage 9 decisions are blocked pending
> independent review of `STAGE9_METRIC_RECORDING_CONTRACT_V2.md`; do not use
> this version's pSNR or raw RED/IR correlation for new Stage 9 acceptance.

Status: `SPECIFICATION + TESTS`. This document and its companion schema/tests
define a **non-clinical, PHI-free** engineering capture format used for
bench validation, regression testing, and manual signal-quality review of
the raw acquisition pipeline. It is a separate artifact from
`schemas/jetson-result.schema.json` (the clinical dashboard-facing result)
and must never be sent to VeinoGate, stored with patient identifiers, or
used as a substitute for the SQI gate (`receiver/sqi_gate.py`) that governs
whether a window may reach the model.

## 1. Scope and non-goals

- In scope: one record per assembled 30-second acquisition window,
  containing raw RED/IR samples, acquisition/register metadata, integrity
  counters, and derived signal-quality metrics, plus the pass/fail result of
  applying this document's validation rules.
- Out of scope / explicitly not changed by this document: VeinNet frozen
  preprocessing, morphology, the ONNX model, golden fixtures, and the
  clinical `sqi_gate.py` acceptance thresholds. Where this spec reuses a
  threshold already defined in `sqi_gate.py`, it imports the constant rather
  than redefining the number, so the two can never silently drift apart.
- A QA capture record carries no `patient_id`, no cuff BP, and no
  demographics. `hardware_uid` and `short_code` (`VN-NNN`) identify the
  physical adapter only, per `AGENTS.md`'s adapter-identity rules — they are
  not, by themselves, PHI.

## 2. Locked acquisition contract

Frozen for this stage; a change requires a versioned update to this
document, `config/hardware_profiles.json`, and full revalidation:

| Parameter | Value |
|---|---|
| Physical sample rate | 400 samples/s |
| Hardware sample averaging | 4 |
| Effective (paired) sample rate | 100 Hz |
| LED pulse width | 411 µs |
| Capture window duration | 30 s |
| Samples per complete window | 3000 (`WINDOW_SAMPLE_COUNT`, `receiver/window_assembler.py`) |

These values are the same ones already enforced by
`config/hardware_profiles.json` (`sample_rate_sps`, `sample_avg`,
`effective_paired_sample_rate_hz`, `pulse_width_us`) and
`receiver/window_assembler.py` (`WINDOW_SAMPLE_COUNT`,
`EFFECTIVE_RATE_HZ`). This document does not introduce a second source of
truth for them — `receiver/qa_capture_builder.py` reads the register values
from `config/hardware_profiles.json` and the window length from
`window_assembler.WINDOW_SAMPLE_COUNT`.

## 3. Record fields

A QA capture record is a single JSON object. See
`schemas/qa-capture-record.schema.json` for the authoritative,
machine-checked shape; this table is a human-readable index into it.

| Group | Field | Meaning |
|---|---|---|
| top-level | `schema_version` | Semver of this record shape (`1.0.0`) |
| top-level | `record_type` | Always `"qa_capture_record"` |
| top-level | `capture_id` | Opaque record identifier (UUID or test-run id), PHI-free |
| `device` | `hardware_uid` | Immutable 64-bit nRF52840 FICR UID, hex string, as stored by `adapter_inventory.py` |
| `device` | `short_code` | Human-facing `VN-NNN` code |
| `session` | `sensor_session_id` | BLE session id the window was assembled from |
| `session` | `sensor_config_id` | Numeric sensor config id (`protocol_decoder.SensorConfigId`) |
| `session` | `sensor_config_name` | `FINGERTIP`, `DORSAL`, or `UNVERIFIED` |
| `register_settings` | `sample_rate_sps` | Physical MAX30102 sample rate (400) |
| `register_settings` | `sample_avg` | Hardware averaging factor (4) |
| `register_settings` | `effective_sample_rate_hz` | Paired effective rate (100) |
| `register_settings` | `pulse_width_us` | LED pulse width (411) |
| `register_settings` | `led_mode` | MAX30102 `LED_MODE` register value |
| `register_settings` | `led_brightness_hex` | MAX30102 `LEDx_PA` register value, as configured hex string |
| `register_settings` | `adc_range_na` | MAX30102 ADC full-scale range, nA |
| `timing` | `device_monotonic_timestamp_start_us` | Device-clock monotonic timestamp of the window's first sample (never wall-clock; immune to RTC absence/drift) |
| `device_monotonic_timestamp_end_us` | timing | Device-clock monotonic timestamp of the window's last sample |
| `timing` | `capture_start_utc` | Jetson-side UTC wall-clock timestamp (ISO-8601) assigned when the first packet of the window was received |
| `timing` | `capture_end_utc` | Jetson-side UTC wall-clock timestamp assigned when the window was cut |
| `timing` | `window_duration_s` | Nominal window duration (30); informational, derived from the locked contract, not measured from the UTC pair (BLE jitter means `capture_end_utc - capture_start_utc` is expected to differ slightly and is not a validation input) |
| `samples` | `first_sample_index` / `last_sample_index` | Device-assigned monotonic sample-index range requested for this window |
| `samples` | `count` | Number of RED/IR sample pairs actually present in `red`/`ir` (`== len(red) == len(ir)`; may be `< 3000` when `integrity.lost_samples > 0`) |
| `samples` | `red`, `ir` | Raw, unprocessed ADC counts in device sample order. No filtering, resampling, or normalization — matches `window_assembler.py`'s "never applies model preprocessing" invariant |
| `integrity` | `expected_samples` | Always 3000 for this contract |
| `integrity` | `received_samples`, `lost_samples`, `packet_loss_fraction` | From `WindowIntegrity` |
| `integrity` | `duplicate_samples`, `out_of_order_packets` | From `WindowIntegrity` |
| `integrity` | `fifo_overflow_count` | Cumulative FIFO-overflow counter delta observed while buffering |
| `integrity` | `session_changed`, `config_changed` | Whether the session or sensor config changed mid-buffer |
| `quality_metrics` | `red`, `ir` (each an object) | See §4: `dc`, `ac_rms`, `pulsatility`, `clipped_fraction`, `psnr_db` |
| `quality_metrics` | `red_ir_correlation` | Pearson correlation between RED and IR channels over the window |
| `validation` | `valid` | `true` only if every rule in §5 passes |
| `validation` | `violated_rules` | Ordered list of rule codes from §5 that failed (empty when `valid=true`) |

## 4. Derived signal-quality metrics

Computed in `receiver/capture_quality_metrics.py`, pure Python (no NumPy/SciPy
dependency — Hardware intentionally has no duplicate pin for those; see
README "Component layout"), deterministic, and operating only on the raw
RED/IR integers already present in the window (no resampling or filtering
that would encroach on VeinNet's frozen preprocessing).

For a channel `x` of length `N` (RED or IR):

- **DC level**: `dc = mean(x)` — average raw ADC count over the window.
- **AC RMS**: detrend by the window mean, then
  `ac_rms = sqrt(mean((x - dc)^2))` — the RMS of the fluctuation around DC.
- **Pulsatility**: `ac_rms / dc` when `dc > 0`, else `0.0` — a
  perfusion-index-style ratio matching `sqi_gate.py`'s existing RED
  pulsatility calculation, generalized to either channel.
- **Clipping**: fraction of samples in the channel at or above
  `ADC_18BIT_MAX = 2^18 - 1` (the MAX30102 18-bit ADC full scale).
- **pSNR (pulsatile signal-to-noise ratio)**: a bench-review proxy, not a
  clinical or diagnostic measurement. Computed as:
  1. Detrend: `d[n] = x[n] - dc`.
  2. Smooth with a centered moving average of window length
     `W = max(3, round(effective_sample_rate_hz / 8))` (13 samples at
     100 Hz, ≈130 ms) — attenuates content above roughly the pulsatile band
     while passing the cardiac waveform.
  3. `signal[n] = moving_average(d, W)` (the assumed pulsatile component);
     `noise[n] = d[n] - signal[n]` (the assumed residual/motion/electrical
     noise).
  4. `psnr_db = 10 * log10(var(signal) / var(noise))`, clamped to
     `[-120.0, 120.0] dB`. If `var(signal) == 0` (no pulsatile component at
     all, e.g. a flat-line window), `psnr_db = -120.0` — checked before the
     noise case so a flat-line window is never reported as a false "great
     SNR" reading. Otherwise, if `var(noise) == 0`, `psnr_db = 120.0`.

This formula is intentionally simple and dependency-free; it is a
QA/bench-review signal, not a substitute for `sqi_gate.py`'s clinical
acceptance rules, and must not be wired into the clinical accept/reject path
without a separate, explicit contract change.

## 5. Validation rules

A record is `valid` only if none of the following are true. Thresholds
marked "(shared)" are imported directly from `receiver/sqi_gate.py` so the
two never diverge; thresholds marked "(new)" are introduced by this
document for QA-record review only.

| Rule code | Condition (violation) | Threshold |
|---|---|---|
| `SCHEMA_INVALID` | Record fails `schemas/qa-capture-record.schema.json` | — |
| `SAMPLE_COUNT_MISMATCH` | `count != len(red)` or `count != len(ir)` or `len(red) != len(ir)` | — |
| `INCOMPLETE_WINDOW` | `received_samples != expected_samples` (i.e. `expected_samples=3000` but fewer were assembled) | (shared: `WINDOW_SAMPLE_COUNT`) |
| `PACKET_LOSS` | `packet_loss_fraction > 0.01` | (shared: `MAX_PACKET_LOSS_FRACTION`) |
| `DUPLICATE_SAMPLES` | `duplicate_samples > 0` | (shared) |
| `OUT_OF_ORDER` | `out_of_order_packets > 0` | (shared) |
| `FIFO_OVERFLOW` | `fifo_overflow_count > 0` | (shared) |
| `SESSION_OR_CONFIG_CHANGED` | `session_changed` or `config_changed` is `true` | (shared) |
| `CLIPPING` | `clipped_fraction > 0.005` for either channel | (shared: `MAX_CLIPPED_FRACTION`) |
| `POOR_CONTACT` | `ir.dc < 1000` | (shared: `MIN_CONTACT_DC`) |
| `LOW_RED_IR_CORRELATION` | `red_ir_correlation < 0.2` | (shared: `MIN_RED_IR_CORRELATION`) |
| `LOW_PULSATILITY` | `red.pulsatility < 0.0001` | (shared: `MIN_RED_PULSATILITY`) |
| `FLATLINE` | every sample in a channel is identical | (shared) |
| `LOW_PSNR` | `red.psnr_db < 3.0` or `ir.psnr_db < 3.0` | (new: `MIN_PSNR_DB`) |
| `NONFINITE_SAMPLE` | any RED/IR sample is not a finite integer | — |

`violated_rules` lists every rule that fails, not just the first; a record
with zero violations has `valid=true`.

## 6. Synthetic fixtures

All fixtures live in `tests/fixtures/qa_capture/` and are generated by
`tests/fixtures/generate_qa_capture_fixtures.py` (deterministic,
`random.Random(seed)`-based, mirroring `receiver/simulated_ble_source.py`'s
approach — no wall clock in generated values). Regenerate with:

```bash
python3 tests/fixtures/generate_qa_capture_fixtures.py
```

| Fixture file | Intent | Expected `violated_rules` |
|---|---|---|
| `valid_dorsal.json` | Clean, complete 3000-sample DORSAL window | (empty) |
| `valid_fingertip.json` | Clean, complete 3000-sample FINGERTIP window | (empty) |
| `corrupted_packet_loss.json` | Window with >1% dropped samples, force-cut past the gap | `PACKET_LOSS`, `INCOMPLETE_WINDOW`, `DUPLICATE_SAMPLES`, `OUT_OF_ORDER` (the force-cut tail packets resend already-seen boundary indices) |
| `corrupted_duplicates.json` | Duplicate sample indices ingested | `DUPLICATE_SAMPLES`, `OUT_OF_ORDER` (an exact-duplicate resend's `first_sample_index` is behind the already-advanced head) |
| `corrupted_out_of_order.json` | Reordered packet sequence | `OUT_OF_ORDER` |
| `corrupted_fifo_overflow.json` | Nonzero FIFO-overflow delta | `FIFO_OVERFLOW` |
| `corrupted_clipping.json` | >0.5% of samples at ADC full-scale | `CLIPPING` |
| `corrupted_flatline.json` | Constant RED/IR (no perfusion) | `FLATLINE`, `LOW_PULSATILITY`, `LOW_PSNR`, `LOW_RED_IR_CORRELATION` (two zero-variance channels correlate as `0.0`) |
| `corrupted_poor_contact.json` | IR DC below contact threshold | `POOR_CONTACT` |
| `corrupted_low_psnr.json` | High-amplitude uncorrelated noise swamping a small pulsatile component | `LOW_PSNR`, `LOW_RED_IR_CORRELATION` |
| `corrupted_schema_missing_field.json` | Structurally invalid: required field removed | `SCHEMA_INVALID` |

Non-PHI guarantee: every fixture's `device.hardware_uid`/`short_code` are
synthetic bench-unit identities (`DEADBEEFCAFE0001`.., `VN-901`..), never a
provisioned inventory UID, and no field carries a `patient_id`, cuff BP, or
demographic value.
