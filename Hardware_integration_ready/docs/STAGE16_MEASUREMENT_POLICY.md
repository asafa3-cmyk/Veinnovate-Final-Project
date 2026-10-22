# Stage 16 Alternative 2 implementation

Owner approval: Alternative 2 only, on 2026-09-28. Alternatives 1 and 3 and TinyML are not implemented. Scope is the Z23 engineering POC, not a clinical monitoring-interval recommendation.

## Contract and ownership

Hardware owns `receiver/measurement_policy.py`, the timing contract in `config/measurement_policy_v1.json` and `config/measurement_policy.schema.json`, lifecycle integration, tests and this document. The reviewed Stage 10 policy, frozen model, preprocessing, morphology, calibration thresholds and Dashboard result schemas are unchanged. The new operations-only nurse-capture endpoint does not change the Dashboard result contract; Stage 17 may consume it.

Hardware startup defaults to `STAGE16-ALTERNATIVE2-v1`: scheduled interval 1,800 seconds and initial response target 40 seconds. `--measurement-policy <path>` accepts a versioned configuration override with the same contract. Unknown keys/versions, non-finite timings, a capture budget below 35 seconds, or interval at/below the capture budget fail startup. The interval is owner-approved engineering policy based on calibration freshness and the idle battery budget, not a clinically sourced number. Calibration warning/expiry remain 14,400/21,600 seconds.

## State machine and obligations

`OFF -> BOOT -> ADVERTISING -> PAIRED -> IDLE/CALIBRATION_DUE -> SCHEDULED_CAPTURE or NURSE_FORCED_CAPTURE -> TRANSMIT -> IDLE/CALIBRATION_DUE`. Failures enter `FAILURE`; session end enters `OFF`. These are worker/policy lifecycle states; startup/end states do not prove physical board power. Actual power remains an operator-confirmed fact. Firmware already advertises indefinitely while unconnected and resumes after disconnect. When paired, the host keeps the verified BLE connection and STATUS subscription; connecting no longer starts optical acquisition under this policy.

Each assigned session owns its scheduler, queue, controller and result pipeline. The first scheduled deadline is one interval after worker creation. Subsequent deadlines use a fixed monotonic cadence, not completion times. Nurse requests are queued per active session through `POST /v1/sessions/{session_id}/capture`; authentication and schema-version checks use the existing operations boundary. Response: schema version `1.0.0`, policy ID `STAGE16-ALTERNATIVE2-v1`, status `QUEUED`. Missing/ended sessions and duplicate queued requests return 409. A forced request never changes the scheduled deadline, replaces a scheduled slot, interrupts an accepted window, or compares readings for similarity. If both are pending, an already-due scheduled slot runs first, then the queued forced request. Late scheduled slots still attempt capture and record lateness; they are not silently coalesced or skipped. Each failed slot produces a non-VALID null-BP result; no automatic replacement capture is added.

## Exposure, integrity and calibration

The receiver uses the existing hash-verified `BoundedExposureController` and `Stage10PacketStream`: independent RED/IR adjustment within the immutable allowed pairs, bounded five-second preflight, frozen 3,000-pair measurement and concurrent BLE transfer. STATUS readback is requested using TIME_SYNC after a freshness fence before START. Unexpected identity/config/session, malformed data, overflow, clipping, loss/order failure, missing contact, timeout and disconnect remain fail-closed. Only verified windows enter the existing SQI/calibration/VeinNet result path. Failed requests clear the raw calibration window and emit null BP, never a prior value. Existing warning, recalibration, inference-error, disconnect and SQI result rules remain authoritative.

The retained same-pair optical calibration currently supports only VN-001. Other identities fail closed rather than borrowing VN-001's calibration; scheduler objects remain isolated per session. On reconnect the transport still verifies the GATT identity and issues NEW_SESSION, invalidating stale calibration through the existing gate. Per-capture START does not gratuitously issue NEW_SESSION. SET_PROFILE is omitted only when current STATUS already identifies the exact required profile; a changed profile increments the firmware session and is not silently rebound to old calibration. This START/index-reset behavior is based on the existing Stage 9/10 experimental firmware and still needs target verification before a full live claim. No firmware was changed or flashed in Stage 16.

## Response and power evidence

The 40-second target comprises frozen five-second preflight plus a 30-second window and a five-second engineering placeholder for downstream work. Preflight and each missing-DATA wait are bounded; a progressing fixed-count window and inference on its complete data are not canceled to satisfy the overall response target. The worker records actual response/downstream duration and raises its effective response ceiling to at least the observed total and `35 + downstream` when exceeded. This is operational timing evidence, not rewriting the versioned configuration or hiding an overrun. Record the observed value before any deployment-config revision.

Reuse Stage 15 owner-reported idle current 9.42–10.03 mA as designated; radio state was not independently observed with that reading. Active draw, charging and storage are explicitly deferred to the post-Stage-20 bundle, and protected-cell/connector verification is owner-declined. The source calculations retain unknown efficiencies and radio duty. No measured active power or actual runtime is invented.

`../runtime-venv/bin/python scripts/stage16_simulate.py` produces a finite two-scheduled/one-forced timing scenario and 20/30/40 mA active-current sensitivity scenarios, clearly theoretical. Runtime estimates use nominal 220 mAh capacity and omit unmeasured battery/regulator losses; they are not guaranteed battery-life claims. State tests also cover collisions, late slots, downstream overruns, expiry/profile/session invalidation, disconnect, queue failure, identity mismatch and cancellation/STOP.

## Verification and exit boundary

### Target identity freshness correction (2026-09-29)

The target diagnostic reproduced a discovery/GATT timing race: a genuine VN-001
advertisement aged from 1.24 to 5.47 seconds while matching GATT verification
took 4.23 seconds. The unchanged five-second gate correctly rejected the aged
entry. A single source was observed; RSSI was used only as diagnostic evidence.

The shared hardware verifier now requests a bounded real-advertisement refresh
after GATT disconnect when supplied with the live registry. A matching UID and
opaque radio source retain the already verified connection handle across BlueZ
object replacement; distinct sources remain separate duplicate candidates. No
advertisement timestamp is fabricated, freshness threshold extended, or identity
fallback introduced. The production runtime and external Stage 16 runner both
use this path. Regression checks retain rejection of missing advertisements and
duplicate sources. This scoped target correction does not change the optical
policy, timing contract, model, schemas, or authorize any capture.

Run the complete Hardware suite and strict Dashboard schema drift. The Stage 16 software verdict requires those checks and the retained simulation. A full Stage 16 physical/performance PASS requires target evidence for complete scheduled/forced cycles, actual state power and response timing; Stage 15's active-power deferral does not make a theoretical number measured. No hardware connection, placement, capture or deployment is initiated by these software checks. Keep the historical Stage 15 closure and failures intact.
