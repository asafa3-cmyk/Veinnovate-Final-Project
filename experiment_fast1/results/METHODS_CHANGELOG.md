# Corrections and clarifications to the supplied methods draft

| Original issue | Revised explanation | Evidence |
|---|---|---|
| Implies a new result every 30 seconds throughout the session | Each slot contains a nominal 30-second recording; automatic slots are approximately three minutes start-to-start | FAST1 plan, driver and measured edge timestamps |
| Reports 19 slots without distinguishing completion | 19 planned; 18 captured; BPREC2 not captured | Raw manifest and sealed records |
| Does not define slot names clearly | B0 calibration; M rest; MOT hand motion; BPUP attempted BP change; BPREC recovery; PRE earlier comparison reference; AVG2 average of two cuff readings | Frozen plan and actual cuff events |
| Ambiguous meaning of starting training | No model training occurred. B0 calibrated the frozen model. The two maneuvers were separate physical BP-change attempts | Frozen model provenance, calibration and operator notes |
| First post-B0 check could be read as an extra slot | M01 is the first calibrated-result gate, not an additional twentieth slot | Driver logic and Gate2 log |
| Could imply three minutes of rest after each window | Three-minute spacing is measured from capture request to capture request | Driver schedule implementation |
| Claims or implies a passed short bench check | Startup hash/connectivity/calibration checks passed in the evening. Retained BENCH logs contain failed connection checks, not BENCH PASS | bench_check.log and start_fast1.log |
| Describes hand stillness without motion exceptions | Hand supported/still except in MOT1 and MOT2 | Plan and console instructions |
| “BP raised” implies a confirmed intervention | BPUP means attempted BP increase; method, intensity and precise overlap were not standardized/instrumented | Operator notes; mixed cuff/model deltas |
| Maneuver descriptions absent | BPUP1: seated thigh squeeze and mental arithmetic; BPUP2: thigh squeeze plus held heel lift; reported times and missing exact overlap stated | operator_note.txt and measured capture times |
| Does not distinguish announced versus measured timing | Countdown is guidance; measured edge window fields are used. Request/SAVE use Mac clock; no unverified cross-clock latency claimed | Driver code, events, sealed and live records |
| Cuff matching could imply proven overlap | Slot ID associates records but does not prove cuff measurement overlap; SAVE is entry time | cuff_events.jsonl schema |
| Calibration/reference role unclear | Only B0 cuff mean126/74 enters calibration; later cuffs score errors. B0 is excluded from BP evaluation | Calibration gate and analysis implementation |
| Repeat permission could imply unlimited attempts | One optional B0R repeat was allowed if B0 failed; it was not used | Plan and absence of B0R in sealed data |
| Late stop and recovered final window omitted | STOP during BPUP2; Jetson completed it; later mirror recovered it. No driver live latency for BPUP2 | Stop log, sealed record, operator note |
| Reference problems omitted | BPUP1 repeat SBP disagreement16 mmHg; BPUP2 late single reference; retained and sensitivity-analyzed | Original cuff events and operator note |
| Analysis appears to have run immediately after a completed protocol | Offline analysis occurred on retained/mirrored data; no claim of a completed19-slot run or instantaneous final analysis | Run termination and report provenance |
| Uses only MAE<=5 as if it were the attachment requirement | Distinguishes the attachment8-10/5-7 targets from the separate stricter FAST1 target | Supplied two-page requirements image and runbook |
| “Previous pipeline” lacks definition | Strict is offline processing of identical recordings with selected quality blocks active; relaxed preserves integrity/model/baseline checks | Prototype-mode source and replay |

This clean methods chapter describes the experiment actually recorded. It is not a new physical protocol or authorization to repeat the maneuvers. Original documents and evidence remain unchanged.
