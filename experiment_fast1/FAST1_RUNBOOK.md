# POC-20261021-FAST1 — Runbook (owner protocol, prototype-continuous-1.1.0)
Prepared 2026-10-20 evening. Capture 2026-10-21 morning. "GO" = adapter fixed on the owner's dorsal hand (Z23), switch ON.

## Fixed facts (do not change)
- Pipeline: prototype-continuous-1.1.0 on the Jetson (veinnovate@192.168.55.1), VEINNET_PROTOTYPE_CONTINUOUS=1, model 1f017653c5c4bba4ee1a96e97e18d4c97f672ce32f98c0e60f3bd31a5eb06e03.
- Source of truth: output/prototype_continuous_20261020/SHA256SUMS.txt (manifest sha 35a4539c...). No other code. No threshold/CONFIG/model change, no HOLD, no bias correction.
- Outputs: BP + HR (raw IR) = main. SpO2 always displayed, UNCALIBRATED_MAXIM_LINEAR, no oximeter exists -> spo2 field empty, accuracy NOT_EVALUABLE.
- No dashboard needed. No automatic retries: every window is kept and labelled.

## T-0 preflight (automatic, <= 3 min, no operator action except switch ON)
1. Mac + Jetson manifest check; Jetson prototype_continuous.py sha == manifest; service active, flag in process env, health model 1f017653.
2. BLE connect to the adapter; first complete 30-s window arrives.
3. Live gate on the first window: sqi_version starts "prototype-continuous-1.1.0", hr.bpm + hr.reason present, spo2 present.
   Fail -> STOP and report (nothing else starts).

## Slot plan (continuous acquisition the whole time; slots = cuff-paired windows)
Cuff on the OTHER arm. Cuff entry in the panel: SBP/DBP/HR, or "SBP/DBP/HR + SBP/DBP/HR" (AVG2). Sensor hand still during every 30-s window.
| # | Slot | When | Cuff | Notes |
|---|---|---|---|---|
| 1 | B0 | after 60 s rest | AVG2 overlapping the window | calibration; must pass motion gate, else repeat B0 only (the only allowed repeat) |
| 2 | Live gate 2 | right after B0 | — | first calibrated window: BP present + 1.1.0 + HR/SpO2 -> continue, else STOP |
| 3-8 | M01-M06 | every 3 min | 1 reading overlapping | rest |
| 9 | MOT1 | after M06 | 1 reading | deliberate hand motion during the window (expect HR ARTIFACT + BP motion warning) |
| 10 | BPUP1 | owner's BP-raising method, window starts when the sensor hand is still | AVG2 if possible | note method + start/stop time; PRE = M06 |
| 11 | BPREC1 | 3 min after BPUP1 | 1 reading | recovery |
| 12-17 | M07-M12 | every 3 min | 1 reading | rest |
| 18 | MOT2 | after M12 | 1 reading | motion |
| 19 | BPUP2 | owner's method | AVG2 if possible | PRE = M12 |
| 20 | BPREC2 | 3 min later | 1 reading | end |
Expected duration ~55 min. The agent announces each slot: "cuff now" / "window start" / "window end" / "next slot in mm:ss".

## Stop conditions
Hash mismatch; flag off; sqi_version not 1.1.0; BLE drop > 2 min; owner discomfort; 3 consecutive integrity failures.
Rollback: restore *.v1_0_0 files or remove the prototype drop-in, restart edge.

## Analysis (start immediately after the last slot; offline on mirrored raw journals + sealed records)
Script: output/experiment_20261021_FAST1/analyze_fast1.py (prepared and tested the evening before on the 3 existing runs).
Per slot table: time, cuff SBP/DBP/HR, model SBP/DBP, HR + tier, SpO2 + label, warnings, beat consistency, latency_ms.
Metrics (committed requirements):
- BP: SBP/DBP MAE, bias (mean error), SD, % within 5/10/15 mmHg vs cuff; target MAE <= 5 mmHg reported as MET / NOT MET; same vs carry-forward-from-B0.
- BP change tracking (BPUP/BPREC vs PRE): cuff delta vs model delta, |error|; carry-forward delta = 0.
- HR: MAE vs cuff pulse, % within +/-5 BPM, per tier (HIGH/LOW/ARTIFACT); tier counts.
- SpO2: displayed on X/Y windows, distribution (min/median/max), label UNCALIBRATED_MAXIM_LINEAR, accuracy NOT_EVALUABLE.
- Continuity: windows produced vs expected, % windows with BP / HR / SpO2, gaps, BLE drops, exceptions.
- Latency: last sample -> emitted result, p50/p95/max (limit p95 <= 2 s).
- Like-for-like: replay the same raw journals offline in strict mode (flag unset); compare on the windows strict answered.
- Beat consistency vs |BP error| correlation (FAST1 data only, candidate BP quality label).
- Bland-Altman plots for SBP, DBP, HR.
Deliverables: FAST1_RESULTS.xlsx (per-slot + summary + charts), FAST1_RESULTS_HE.md (short Hebrew summary for the book chapter), all hashes.
