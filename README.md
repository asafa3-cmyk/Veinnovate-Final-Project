# Veinnovate — Cuffless Continuous Blood-Pressure Monitoring (Final Project Code)

> **עברית בקצרה:** מאגר זה מכיל את הקוד הרלוונטי של פרויקט הגמר Veinnovate — קושחת החיישן, מקלט הקצה (Jetson), סביבת הריצה של מודל VeinNet, ניסוי FAST1 וניתוח התוצאות. המשקולות המאומנות של המודל, קוד האימון ונתוני המשתתפים הגולמיים אינם נכללים (קניין רוחני ופרטיות). הקישורים בהמשך מובילים ישירות לקטעי הקוד המרכזיים.

Afeka College of Engineering — Biomedical Engineering final project.
Author: Asaf Asnin.

---

## 1. System overview

```
MAX30102 PPG sensor ──I²C──► XIAO nRF52840 firmware ──BLE (protocol V3)──► Jetson edge receiver
                                                                              │
                                30-s window (3000 samples @100 Hz) ◄──────────┘
                                              │
                         Signal-quality gate (SQI) ──fail──► ABSTAIN (no number shown)
                                              │ pass
                       Preprocessing + 13 morphology features
                                              │
          VeinNet PersonalDelta ONNX model (hash-verified, fail-closed) + cuff calibration
                                              │
                         SBP / DBP / HR ──► VeinnoGate dashboard
```

| Folder | Contents |
|---|---|
| [`Hardware_integration_ready/firmware/`](Hardware_integration_ready/firmware) | Arduino firmware for the wearable adapter (XIAO nRF52840 + MAX30102) |
| [`Hardware_integration_ready/receiver/`](Hardware_integration_ready/receiver) | Python edge service on the Jetson: BLE, decoding, window assembly, quality gating, calibration, inference orchestration, HTTP/WebSocket API |
| [`Hardware_integration_ready/config/`](Hardware_integration_ready/config), [`schemas/`](Hardware_integration_ready/schemas), [`docs/`](Hardware_integration_ready/docs) | Thresholds, measurement policy, JSON schemas, BLE/QA specifications |
| [`VeinNet_integration_ready/`](VeinNet_integration_ready) | VeinNet inference runtime: frozen preprocessing, morphology, ONNX loading, contracts |
| [`experiment_fast1/`](experiment_fast1) | FAST1 human-subject experiment: run controller, analysis, result tables and figures |
| [`model_evaluation/`](model_evaluation) | VeinNet vs. ResNet-34 evaluation on MIMIC-BP: results, methodology, limitations, figures |

Dashboard (separate public repository): <https://github.com/Veinnovate-Project/VeinnoGate_dashboard_monitor_project>

---

## 2. Central code snippets (for the examination committee)

### Signal acquisition and transport
| Code | Role and importance |
|---|---|
| [`veinnet_sensor_v1.ino` — `setup()` / `loop()`](Hardware_integration_ready/firmware/veinnet_sensor_v1/veinnet_sensor_v1.ino#L814) | Firmware main loop: reads the MAX30102 FIFO at 100 Hz, packs RED/IR samples and streams them over BLE. The source of every measurement. |
| [`applyAndVerifyProfile()`](Hardware_integration_ready/firmware/veinnet_sensor_v1/veinnet_sensor_v1.ino#L164) | Writes LED current / mode to the sensor and reads it back, so a wrong optical configuration is detected instead of silently producing bad data. |
| [`common/protocol.h`](Hardware_integration_ready/firmware/veinnet_sensor_v1/common/protocol.h#L29) | BLE protocol V3 definition (service UUIDs, packed header, flags for FIFO overflow / clipping / low battery). Shared contract between firmware and receiver. |
| [`protocol_decoder.py` — `crc16_ccitt()` / `decode_data_packet()`](Hardware_integration_ready/receiver/protocol_decoder.py#L92) | Decodes each BLE packet and verifies its CRC; corrupted packets are rejected before they reach the model. |
| [`window_assembler.py` — `SessionRingBuffer`](Hardware_integration_ready/receiver/window_assembler.py#L74) | Reassembles packets into exact 30-s windows (3000 samples) using sample indices and reports gaps, overflow and loss fraction. |

### Signal quality and physiology algorithms
| Code | Role and importance |
|---|---|
| [`sqi_gate.py` — `evaluate_window()`](Hardware_integration_ready/receiver/sqi_gate.py#L52) | Signal-quality gate. Rejects a window if packet loss > 2 %, clipping > 1 %, contact DC < 1000, RED–IR correlation < 0.2 or RED pulsatility < 1e-4. A failed window yields **ABSTAIN** rather than an unreliable BP value — the core safety principle. |
| [`max30102.py` — `assemble_packets()`](VeinNet_integration_ready/veinnet_runtime/preprocessing/max30102.py#L49) | Model-side integrity checks (ordering, loss, saturation, clipping, contact, motion). |
| [`max30102.py` — `preprocess()`](VeinNet_integration_ready/veinnet_runtime/preprocessing/max30102.py#L96) | Frozen preprocessing: detrend → 4th-order Butterworth band-pass 0.5–8 Hz → SQI → resample → robust (median/MAD) normalisation → 3×1000 PPG tensor. Must match training exactly. |
| [`morphology.py` — `compute_morphology()`](VeinNet_integration_ready/veinnet_runtime/preprocessing/morphology.py#L38) | Deterministic extraction of 13 pulse-morphology features per window (second model input). |
| [`pulse_rate.py` — `estimate_pulse_rate()`](Hardware_integration_ready/receiver/pulse_rate.py#L26) | Heart-rate estimation (dual-channel peak-interval algorithm) — only on SQI-accepted windows. |
| [`prototype_continuous.py` — `tiered_hr()`](Hardware_integration_ready/receiver/prototype_continuous.py#L257) | Tiered HR estimator (MSPTD / Elgendi / Welch / autocorrelation) and experimental [`spo2()` ratio-of-ratios](Hardware_integration_ready/receiver/prototype_continuous.py#L310) (SpO₂ not clinically validated). |

### Calibration and inference
| Code | Role and importance |
|---|---|
| [`calibration_store.py` — `CalibrationStore.capture()` / `status()`](Hardware_integration_ready/receiver/calibration_store.py#L145) | Per-patient cuff calibration (baseline SBP/DBP + baseline PPG). Warning after 4 h, hard expiry after 6 h. PersonalDelta predicts the *change* from this baseline. |
| [`entry_point.py` — `VeinNetRuntime`](VeinNet_integration_ready/veinnet_runtime/entry_point.py#L36) | Public runtime API: `load → assemble_and_preprocess → demographics → infer`, returning personal SBP/DBP with input/output validation. |
| [`onnx_loader.py` — `load_verified_session()`](VeinNet_integration_ready/veinnet_runtime/onnx_loader.py#L33) + [`hash_verify.py`](VeinNet_integration_ready/veinnet_runtime/hash_verify.py#L16) | Loads the model only if its SHA-256 matches the contract and its I/O shapes/dtypes match — **fail-closed** against a wrong or tampered model. |
| [`contracts/model_contract.json`](VeinNet_integration_ready/contracts/model_contract.json) / [`preprocessing_contract.json`](VeinNet_integration_ready/contracts/preprocessing_contract.json) | Frozen interface: 6 inputs (PPG, morphology, baseline PPG/morphology, demographics, baseline cuff), output = absolute [SBP, DBP]. |
| [`model_input_builder.py`](Hardware_integration_ready/receiver/model_input_builder.py#L90) | Interface that builds the model inputs from a raw window; refuses test-only builders in production. |
| [`veinnet_edge_service.py` — `EdgeOrchestrator`](Hardware_integration_ready/receiver/veinnet_edge_service.py#L74) | End-to-end orchestration on the Jetson: BLE → window → SQI → calibration → inference → dashboard API. |

### Experiment and evaluation
| Code | Role and importance |
|---|---|
| [`experiment_fast1/fast1_run.py`](experiment_fast1/fast1_run.py#L123) | Controller of the FAST1 protocol (timed slots, cuff reference entry, quality gating). |
| [`experiment_fast1/analyze_fast1.py` — `analyze()` / `bland_altman()`](experiment_fast1/analyze_fast1.py#L183) | Offline replay and statistics: MAE, error distribution, Bland–Altman, change tracking. |
| [`experiment_fast1/analysis/build_results.py`](experiment_fast1/analysis/build_results.py#L28) | Builds all result tables (`results/tables`) and figures (`results/figures`) from the run package. |
| [`model_evaluation/bp_threshold_analysis.py`](model_evaluation/bp_threshold_analysis.py) | ROC / PR analysis of hypertension-threshold detection. |

---

## 3. Key results

| Evaluation | Result |
|---|---|
| MIMIC-BP test set — VeinNet PersonalDelta ensemble vs. ResNet-34 | SBP/DBP MAE **11.74 / 6.80** vs. 14.04 / 8.25 mmHg; 1.75 M vs. 21.66 M parameters ([details](model_evaluation/RESULTS.md)) |
| FAST1 (single participant, prototype) — BP | MAE 13.36 / 7.87 mmHg (n = 17); strict-gate subset 9.29 / 6.27 mmHg (11/17) |
| FAST1 — HR | MAE 3.38 BPM (n = 18) |
| FAST1 — latency | p50 / p95 = 0.212 / 0.274 s |

FAST1 is a single-participant engineering feasibility study and does **not** establish clinical accuracy (a constant-baseline predictor scored 4.53 / 3.62 mmHg). See [`experiment_fast1/results`](experiment_fast1/results) and [`model_evaluation/LIMITATIONS.md`](model_evaluation/LIMITATIONS.md).

---

## 4. What is intentionally excluded (intellectual property and privacy)

- Trained model weights (`*.onnx`, `*.pt`) and golden regression fixtures.
- Model architecture and training code.
- Raw participant signals and the subject registry.
- Raw bench-recording evidence and the automated test suites that depend on it.

The code is therefore provided for **review**; the complete pipeline runs only with the withheld model file (the loader deliberately refuses to start without it).

## 5. Running the parts that do not need the model

```bash
pip install -r Hardware_integration_ready/requirements.lock.txt
pip install -e VeinNet_integration_ready
cd Hardware_integration_ready && ./scripts/run_simulator.sh   # simulated BLE source → receiver
```

## License

© Asaf Asnin. All rights reserved. Shared for academic evaluation only — see [LICENSE](LICENSE).
The bundled SparkFun `MAX30105` driver keeps its original BSD license header.
