# Limitations and claim boundary

## Calibration anchor

V6PRO+SSL PersonalDelta is intermittently cuff-calibrated and cuff-free between calibration rounds. A real cuff measurement begins each cycle; repeated inference then uses only MAX30102 PPG, the stored cuff SBP/DBP reference, and synchronized baseline PPG. Hundreds of PPG estimates may be produced between rounds. A prediction is never reused as a cuff anchor, and the next real cuff measurement resets cuff SBP/DBP, baseline PPG, and elapsed time.

In the MIMIC-BP evaluation, the first accepted reference BP only emulates one cycle anchor. This is not evidence that an actual cuff, wearable sensor, arterial line, and model behave interchangeably in prospective use.

## No reliable MIMIC duration analysis

The frozen official MIMIC-BP rows preserve subject and row order but not the exact WFDB record path, starting sample, and verified timestamp sidecar needed to recover elapsed hours. Consequently:

- no 4 h or 6 h MIMIC analysis was run;
- the proposed 4–6-hour recalibration schedule is an operational prototype policy aligned with common periodic vital-sign observation workflows and remains to be validated prospectively;
- row index is not treated as time;
- MIMIC did not validate 4-hour, 6-hour, 12-hour, or 24-hour monitoring duration.

## Reference and deployment domain

MIMIC-BP supplies ICU PPG and invasive arterial-pressure-derived BP targets. It is not a prospective wrist/finger wearable study, a home-monitoring cohort, or a MAX30102 validation. Sensor placement, skin tone, contact pressure, motion, temperature, posture, medication, and ambulatory behavior can alter performance.

## No standards or clinical claim

The reported MAE, bias, SD, and descriptive absolute-error coverage are retrospective statistical results. They do not establish:

- AAMI/ISO compliance;
- a BHS device grade;
- clinical validation or diagnostic accuracy;
- regulatory clearance;
- safety or effectiveness of a medical device.

The complete ±5/±10/±15 percentages are retained only in the [technical appendix](RESULTS.md#technical-appendix-complete-descriptive-coverage).

## Ensemble asymmetry

The ResNet candidate contains three full backbones. The PersonalDelta candidate contains one shared frozen encoder and three heads. Both are three-component ensembles, but their component granularity differs. This is deliberate and is the engineering contribution behind the 91.9% parameter reduction; readers should not interpret parameter count as a comparison of single backbones.

## Latency scope

Latency was measured under one controlled GPU configuration at batch size 1. Absolute timing can change with hardware, drivers, framework versions, thermal state, batching, and deployment runtime. The fair claim is the same-session relative comparison, not a universal 4.84 ms device guarantee.

## ONNX parity scope

The opset-17 parity test used eight non-sealed validation rows. A maximum absolute difference of 0.00107 mmHg supports numerical agreement between the exported graph and PyTorch reference on those rows. It does not validate ONNX accuracy on the MIMIC test set, a wearable device, or clinical hardware.

## External evidence

A separate VitalDB analysis is post-hoc sensitivity evidence. It is not part of the primary MIMIC result, and its numerical tables, plots, rolling recalibration outputs, and absolute MAEs are intentionally excluded from this public presentation.

## Generalizability and prospective work

The 214-subject frozen cohort is sufficient for a paired retrospective comparison but does not establish performance across institutions, BP distributions, clinical states, ages, skin tones, or sensor types. Future clinical work requires a prespecified prospective protocol with real cuff anchors, timestamped follow-up, external cohorts, wearable PPG, and explicit 4 h/6 h recalibration endpoints.
