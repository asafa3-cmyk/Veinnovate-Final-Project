# Hardware_integration_ready

Relocatable hardware-edge bundle for the VeinNet clinical-prototype pipeline. It contains XIAO nRF52840/MAX30102 firmware, BLE acquisition, calibration, the real VeinNet sibling adapter, and the HTTP/WebSocket producer consumed by VeinoGate Dashboard. It does not ship the ONNX model or install numerical runtimes.

This is a clinical-grade prototype architecture, not a validated or approved medical device.

## Component layout

The three bundles must be siblings:

```text
integration-root/
├── Hardware_integration_ready/
├── VeinNet_integration_ready/
└── VeinoGate_Dashboard_integration_ready/
```

`config/sibling_paths.example.json` is the default configuration and contains relative paths only. Supply `--sibling-config <file>` if custom relative locations are needed.

The model component owns NumPy, SciPy, and ONNX Runtime. Hardware intentionally has no duplicate pins for them.

## Python setup

Python requirement: 3.10 or newer.

```bash
python3 -m venv ../runtime-venv
source ../runtime-venv/bin/activate
python -m pip install -e ../VeinNet_integration_ready
python -m pip install -r requirements.lock.txt
python -m pip check
```

The lock includes `wsproto==1.2.0` for Uvicorn's live WebSocket endpoint; FastAPI TestClient does not verify that network upgrade path.

The locked BLE dependency is `bleak==3.0.2`. Its package metadata requires Python `>=3.10`. Protocol V2 scans all matching service-data advertisements, retains the exact discovered device object as an internal connection handle, and verifies its read-only identity characteristic before streaming. It never selects identity by BLE name/address/RSSI/order. Physical BlueZ/Jetson verification remains required.

## Production and simulation

Real BLE production mode always requires the verified model pipeline and fails before BLE starts if the sibling adapter/model cannot load:

```bash
scripts/run_edge_service.sh --patient-id <patient-id>
```

Hardware-free protocol simulation:

```bash
scripts/run_simulator.sh --patient-id synthetic-patient --no-http
```

Hardware-free full production-model pipeline:

```bash
scripts/run_simulator.sh --production --patient-id synthetic-patient --no-http
```

Production defaults to `veinnet_sibling_adapter:VeinNetSiblingAdapter`. It is `is_test_only=False`, loads `veinnet_runtime.VeinNetRuntime`, uses the sibling's frozen preprocessing, morphology, demographics, and elapsed-segment transforms, and invokes the real ONNX model. Production never substitutes a mock.

## Model contract

- Artifact: `VeinNet_integration_ready/model/personaldelta_ensemble_42_43_44.onnx`
- SHA-256: `cb320a28b0abd48c2f6e34828c83b50ecce3c1f0c30f7fc8402b964cd74c8cd0`
- ONNX opset: 17
- Inputs: `ppg [B,3,1000]`, `morphology [B,13]`, `baseline_ppg [B,3,1000]`, `baseline_morphology [B,13]`, `demographics [B,4]`, `baseline_cuff [B,2]`, `elapsed_segments [B,1]`; all float32
- Output: absolute float32 `[B,2] = [SBP, DBP]`
- Preprocessing provenance: `veinnet-max30102-preprocessing-contract-1.0.0`
- Morphology provenance: `veinnet-morphology-contract-1.0.0`

The sibling runtime verifies the real artifact hash and loaded ONNX I/O contract. Any mismatch fails closed.

## Dashboard boundary

- Snapshot: `GET /api/v1/sessions/{session_id}/snapshot`
- WebSocket: `/ws/v1`
- Operations: `GET /v1/health`, `GET /v1/config`, `POST /v1/calibration`
- Default bind: `127.0.0.1:8080`
- Optional bearer token: `VEINNET_API_TOKEN`
- Canonical schema version: `1.0.0`

Canonical schema hashes:

- `schemas/jetson-result.schema.json`: `ee165bf6851a7950754c4fa5fdf679f26ec4b38b480eaf6999205ebb6f667b1f`
- `schemas/jetson-ws-envelope.schema.json`: `11047685bcece0ef79a224175501ddf3ae70676cef2958a65548e92e6f63c444`

Both files are byte-identical to the Dashboard consumer copies. Hardware emits schema-valid `VALID`, `ABSTAIN`, `RECALIBRATE`, `INFERENCE_ERROR`, and `DEVICE_DISCONNECTED` results.

## Calibration and identity

- `--patient-id` is mandatory.
- Device identity is the canonical 64-bit nRF52840 FICR UID carried by BLE Protocol V2 advertising and re-read from GATT before assignment and every reconnect; see `docs/BLE_PROTOCOL_V2_IDENTITY.md`.
- V2 discovery uses a bounded, platform-unfiltered BLE scan followed by strict application filtering on the exact complete 128-bit service-data identity. This supports CoreBluetooth and BlueZ without fallback to name, address, RSSI, order, or partial identity.
- Calibration binds patient, device, sensor session, and sensor configuration.
- Warning threshold: 14,400 seconds; hard expiry: 21,600 seconds.
- Expired or mismatched calibration yields `RECALIBRATE`.
- Calibration stores cuff BP and baseline PPG with file mode `0600` in a `0700` directory.

## Firmware

Pinned toolchain:

- Seeed nRF52 core `1.1.9`
- XIAO nRF52840 FQBN `Seeeduino:nrf52:xiaonRF52840`
- SparkFun MAX3010x library `1.1.2` — **overridden by a sketch-local patched copy, see below**
- Bluefruit52Lib `0.21.0`, bundled with the Seeed core

Install only if the pinned packages are absent:

```bash
arduino-cli core install Seeeduino:nrf52@1.1.9 --additional-urls https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
arduino-cli lib install "SparkFun MAX3010x Pulse and Proximity Sensor Library"@1.1.2
```

`firmware/veinnet_sensor_v1/MAX30105.h`/`MAX30105.cpp` are a vendored, patched copy of
that same 1.1.2 library, not the globally installed one. `veinnet_sensor_v1.ino` includes
`MAX30105.h` with a quoted (not angle-bracket) `#include`, so arduino-cli resolves it from
the sketch directory first — the global `arduino-cli lib install` above is still needed
(other sketches use it), but this sketch always compiles against the local, patched copy.
The patch fixes a stale-sample bug: `clearFIFO()` previously reset only the sensor's
hardware FIFO pointers, not the library's own software mirror of the FIFO
(`sense.head`/`sense.tail`/`sense.red`/`IR`/`green[]`), so the first sample read after a
profile/config change could silently be leftover data from the *previous* recording. See
the header comment in `MAX30105.h` for the full mechanism, and
`STAGE9_METRIC_CORRECTION_HANDOFF.md` for the original report. Do not delete these two
files expecting the global library install to cover the sketch — that reintroduces the bug.

Compile:

```bash
arduino-cli compile --fqbn Seeeduino:nrf52:xiaonRF52840 firmware/veinnet_sensor_v1
```

This generic build intentionally keeps `VEINNET_SHORT_CODE_NUMBER=0`; it advertises immutable FICR identity but
is unprovisioned and cannot pass the production assignment gate. Build a reserved physical unit without editing
the sketch:

```bash
python3 scripts/build_provisioned_firmware.py \
  --uid 0123456789ABCDEF \
  --short-code VN-001 \
  --output-dir /private/tmp/veinnet-VN-001-0123456789ABCDEF
```

The output directory must be new and itself be named for the UID/VN pair. The tool verifies the pinned core,
compiles with only the nonzero VN suffix, and writes a UID/VN-named PHI-free record binding and hashing every
generated artifact. Standard Arduino artifact names are retained so `arduino-cli upload --input-dir` works. UID is never compiled in;
firmware reads it from `NRF_FICR->DEVICEID[0..1]`. Inventory uses two phases: atomic
`PENDING_PROVISIONING` reservation, then `PROVISIONED` only after the flashed production GATT identity reports
the same UID and VN code. Pending records cannot be assigned or auto-bound.

BLE identity advertising and its read-only GATT characteristic start even if MAX30102 is absent. In that state
the status path reports `sensor_config_id=0`, START remains disabled, and no DATA is emitted. Sensor-present
acquisition retains the frozen 400-sps/average-4/effective-100-Hz contract.

The historical Stage 1 size is not an acceptance value after Protocol V2 changes. Record exact compile output and artifact hashes for each approved build.

Flash after identifying the port:

```bash
arduino-cli board list
scripts/flash_firmware.sh <port>
```

## Stage 16 measurement policy

Hardware runtime uses owner-approved Alternative 2: a verified BLE keep-alive,
scheduled captures every 1,800 seconds, and session-bound nurse overrides.
`config/measurement_policy_v1.json` is the versioned timing configuration;
`--measurement-policy <path>` supplies an explicit override. Every due slot
attempts the frozen Z23 preflight/window path; no similarity-based skipping or
synthetic BP fallback is permitted. The initial 40-second response target does
not truncate completed-window processing. See
[the policy and verification boundary](docs/STAGE16_MEASUREMENT_POLICY.md).

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests -q
```

Current complete Hardware suite: 403 passed. The run includes real frozen preprocessing and real ONNX inference when the editable VeinNet sibling is installed. Dashboard `npm run check:schema-drift:strict` passes against the relocated Hardware sibling without skipping.

## Stage 7 QA capture recorder

`receiver/qa_capture_recorder.py` records one PHI-free engineering window
through the existing Protocol V2 decoder, window assembler, and QA capture
builder. It rejects truncated, reordered, identity/config-mismatched, or
otherwise invalid streams and publishes `record.json`, raw packet JSONL,
analysis-ready sample CSV, and a SHA-256 manifest with an atomic directory
rename. Experimental output is required to be outside all three production
bundle paths and is not sent to VeinoGate.

Replay a timestamped raw-notification stream into an explicit external
output directory:

```bash
python3 scripts/record_qa_capture.py \
  --packets-jsonl /external/bench-input/packets.jsonl \
  --hardware-uid 0123456789ABCDEF --short-code VN-001 \
  --sensor-config-id 2 --output-root /external/bench-captures
```

The input JSONL has one object per notification with `raw_base64`,
`host_monotonic_ns`, and UTC `host_utc`. See
`docs/QA_CAPTURE_FORMAT_SPEC.md`; these QA artifacts are non-clinical and
must never contain patient identifiers, cuff values, or demographics.

## Stage 8 MAX30102 bench probe

`firmware/stage8_max30102_probe/` is identification-first diagnostic firmware,
not production firmware. At boot it performs no sensor writes and enables no
LED drive. The exact `IDENTIFY` command probes I2C address `0x57` and reads
Revision ID `0xFE` plus Part ID `0xFF`; `START_FIFO` remains locked until the
Part ID is exactly `0x15`. Its bounded fingertip configuration is 3.3 V,
100 kHz I2C, RED/IR pulse amplitudes `0x1F`, 4096 nA ADC range, 400 physical
samples/s, averaging 4, 100 effective pairs/s, and 411 us pulse width. Mode is
enabled only after register readback and is disabled on completion or error.

Compile without changing the production sketch:

```bash
arduino-cli compile --fqbn Seeeduino:nrf52:xiaonRF52840 \
  firmware/stage8_max30102_probe
```

`scripts/stage8_serial_command.py` sends one allow-listed, bounded command and
can atomically retain PHI-free JSONL in an explicit experimental output path.
Stage 8 evidence is engineering-only and must not be interpreted as a clinical
measurement or as dorsal-hand validation. Restore the unit's verified
UID/VN-specific production artifact after bench testing.

## Stage 9 dorsal-hand engineering sweep

The frozen Stage 9 plan is `config/stage9_dorsal_sweep.json`; the complete
physical method and stop rules are in `docs/STAGE9_DORSAL_CAPTURE_PROCEDURE.md`.
`scripts/build_stage9_firmware.py` builds one VN-001-only engineering image
whose allowlisted config IDs 16..23 preserve Protocol V2 and the locked
400/4/100 Hz, 411 us acquisition settings. It is not a production image.
`scripts/stage9_capture.py` performs one authorized next-slot capture at a
time, re-verifies BLE/GATT identity and config status, feeds raw notifications
through the Stage 7 recorder, and advances an external checksummed ledger only
after atomic persistence and replay verification. The fixed OAAT design has
eight unique combinations and three 30-second repetitions each (24 captures).
Experimental capture roots must remain outside every production bundle and
must not be committed.

Current Stage 9 gate status is
`STAGE9B_EXPOSURE_BATCH_AUTHORIZED_THRESHOLDS_PENDING`. Contract schema `2.2.0`
authorizes only the hash-bound 18-attempt plan in
`config/stage9b_exposure_batch_v2_2.json`: zones `Z23`/`Z34`/`Z45`, configs
16/17/18, and ADC range 16384 nA. Every attempt retains authenticated raw
RED/IR, including integrity failures and zero-data attempts. Numeric v1/v2
quality metrics do not accept or reject attempts in this exposure-only batch;
clipping, FIFO overflow, transport, identity, configuration, timing, and
manifest failures remain fail-closed. Thresholds remain null, Stage 9
completion is not authorized, and Stage 10 remains blocked. The prior
single-site OAAT sweep and its ledger are retained unchanged as historical
evidence. Bootstrap, reference-device, multi-subject, and threshold-fitting
work remains later validation and does not block this exposure-only batch.
Manual optical-window placement does not wait for the printed adapter. The corrected
transport was physically verified for config 16 across three complete
3000-pair windows within the 45-second deadline, each with exactly one START,
no packet rejection, and firmware completion observed. All three windows were
rejected by the unchanged Stage 7 QA gate for `LOW_PSNR`; the logging-enabled
final retry measured IR pSNR 1.9704 dB against the fixed 3.0 dB minimum. No
accepted config-16 capture bundle or batch ledger was produced. By explicit
operator authorization, these three lowest-current rejections remain an
informational baseline. The existing runner can technically continue at config
17 only through
the checksummed disposition in
`config/stage9_config16_informational_baseline.json`. The runner verifies all
three external diagnostics before advancing and never records them as accepted
captures. See `var/stage9-evidence/vn001_dorsal_gate.json`. Stage 9 remains in
progress, but that continuation is suspended by the Stage 9 recovery plan and
Stage 10 is not authorized.

A focused software corrective pass replaced the bare STATUS event with an
explicit one-shot commit gate. Its first revision incorrectly required an
unsolicited baseline STATUS immediately after subscription; physical retry
failed closed before `SET_PROFILE` because Protocol V2 makes no such delivery
guarantee. The current recorder instead awaits subscription completion, writes
`SET_PROFILE`, establishes a post-write monotonic freshness fence, and sends the
protocol-supported `TIME_SYNC` request. Firmware answers that request
immediately with current STATUS. Pre-fence/stale/malformed/mismatched/late
STATUS is rejected, and exactly one `START` is permitted only after the matching
fresh response is stored. Timeout and disconnect paths publish no capture or
ledger. Software and compile gates pass; physical config-16 revalidation remains
pending, so the original Stage 9 failure decision is not cleared.

A focused transport correction now constrains every Stage 9 DATA packet to one
negotiated `ATT_MTU-3` notification and retains a pending packet until
Bluefruit reports that the complete notification was queued. Stage 9 sample
indices and the 3000-pair completion counter advance only after that success;
queue backpressure can no longer be silently counted as delivered DATA. The
firmware emits an immediate STATUS after its final accepted notification.

Every new Stage 9 attempt also publishes a checksummed PHI-free diagnostic
directory under the external batch root, even when the attempt fails. Its
atomic `diagnostic.json` records notification/packet/pair counts, rejection
reason codes, host timestamps, packet/sample-index range, disconnect state,
firmware-completion observation, final state, and publication/ledger booleans.
This diagnostic is evidence only and can never substitute for or publish a
Stage 7 QA capture. Physical validation of the corrected transport remains
pending.

## Physical and Jetson validation still required

- Real XIAO/MAX30102 sensor contact and waveform acquisition
- Real BLE radio, BlueZ, negotiated MTU, packet loss, reconnect, and notification behavior on Jetson Orin Nano
- Jetson/aarch64 installation of NumPy, SciPy, ONNX Runtime, Bleak, FastAPI, and Uvicorn
- Battery ADC measurement and battery-threshold validation
- Charge-current polarity and FIFO-overflow behavior on hardware
- Automatic LED exposure tuning
- HR implementation/validation
- Validated SpO₂ algorithm

Until those checks are complete, battery reports `HARDWARE_VALIDATION_REQUIRED`, HR is unavailable, and SpO₂ is unavailable.

## Backend-managed patient identity (owner decision 2026-10-07)

Identity contract 2.0.0 removes client-supplied `patient_id` from pairing start. The nurse enters one National Patient ID field first, then the existing profile fields and physical code confirmation. The backend validates the profile, reuses a consistent patient record in the protected assignment store, or allocates a random opaque UUID identifier. National ID is never used or hashed as `patient_id`. Conflicting historical records fail closed. Personal fields remain confined to identity APIs and the protected assignment SQLite store. Research attribution `SUBJ-001` remains separate in existing research evidence; nurses do not enter it. Fresh electronic UID/code verification and atomic assignment remain mandatory. No capture is authorized.

## Bedside care integration (2026-10-07)

`SessionCareView` consumes the canonical `patient-care.schema.json` APIs at
`/v1/identity/care/sessions/{session_id}`: GET `status`, POST `cuff`,
GET `history`, and PATCH `location`. Identity API authorization and no-cache
restrictions apply. Nurse, shift and nurse-in-charge are operator-declared;
they are not authenticated staff identities. Personal fields stay in the
protected identity store and UI memory only.

Current backend `calibration_status` is authoritative: MISSING, VALID,
WARNING, EXPIRED or INVALIDATED. `calibration_outcome` separately reports
NONE, PENDING, ACTIVE or REJECTED. Previous VALID BP cannot establish current
calibration validity. Warning/expiry remain 14,400/21,600 seconds.

Cuff measurement and entry timestamps are separate. Early cuff entry is
pending. Activation requires an accepted baseline with patient/device/sensor
session/configuration binding. `CUFF-PPG-HOST-ASSOCIATION-v1` requires the cuff
measurement within 120 seconds of the verified baseline host-receipt interval.
An existing baseline requires explicit selection; an arbitrary old reading
cannot be attached. This is an engineering association rule.

History response is `{contract_version, session_id, records}`. Append-only
LOCATION_UPDATED records contain `record_id`, `type`, `recorded_at_utc`,
`staff`, `old:{department,bed}` and `new:{department,bed}`. CUFF_ENTERED
records retain measured/entered timestamps and values; CALIBRATION_OUTCOME
records join by `cuff_record_id`. History remains protected and accessible
after session closure. Location edits preserve pairing and calibration.

The configured interval is 1,800 seconds. The schedule remains unarmed until
the nurse explicitly requests the first measurement. Deployment and pairing
do not start capture. Fixed25 retains its 53-second acquisition timeout; the
configured 40-second response deadline is a target, not a measured duration.
Sensor idle and battery savings have not been physically verified.

Result contract 1.1.0 publishes eligible experimental HR independently of BP
readiness/model availability, using the existing receiver quality gate and
unchanged HR estimator. Failure publishes null with its reason; every HR is
bound to its window. Experimental HR is excluded from NEWS2. SpO2 stays
unavailable. No frozen model preprocessing or BP gate changed.

Optional cuff entry inside the pairing modal is deferred. Cuff entry immediately
after pairing, before or after PPG, is supported and never blocks pairing or
manual acquisition.

## Registered prototype reuse policy

Owner-authorized `PROTOTYPE-DEVELOPMENT-REUSE-v1` applies only to the existing
verified VN-001/VN-002/VN-003 UID/code pairs. Explicit End Session invalidates
calibration and ends assignment/session rows, then leaves those units reusable
and unassigned. Other inventory keeps the single-use default. Disconnect,
power-off, application closure and service shutdown do not end assignments.
Each subsequent pairing requires fresh electronic identity verification and a
new atomic assignment/session. An attempted pairing never terminates an active
binding. `scripts/apply_prototype_reuse_policy.py` is explicit maintenance only:
run with edge stopped, backed up and no active bindings. Policy registration,
registered-inventory restoration and consumed-state recovery are append-only
inventory audit events. Original consumed timestamps remain in those events;
ended sessions and calibration files are unchanged.

Care status includes the additive optional `calibration_reason_code` field in
wire contract 1.0.0. Rejected/active backend outcomes synchronize tile and modal.
Calibration validity starts at successful activation; early cuff entry and the
baseline window timestamp do not start the clock. Measurement time and entry
time remain distinct in protected history. WARNING is visibly due soon.

## Battery voltage telemetry

Firmware 3.0.2 adds a bounded read-only `BATTERY` USB diagnostic. Runtime status
1.1.0 adds per-session battery voltage and the backend-owned 3600..3900 mV POC
display scale. Voltage and percentage remain unavailable without per-UID
multimeter evidence and fresh identity-checked STATUS. See
[the interface, finite plan, and Claude handoff](docs/BATTERY_TELEMETRY_V1_1_HANDOFF.md).
