# FAST1 figure captions

## F01 | What was actually recorded

Thirty-second windows were scheduled at roughly three-minute start-to-start intervals; challenge slots required manual GO. Total nominal sampled time is 9 min. Blank intervals are unsampled scheduled gaps, not demonstrated packet loss. BPREC2 is missing; B0 is calibration with no live BP.

## F02 | BP estimates versus cuff over the session

B0 excluded from evaluation; n=17. Lines connect intermittent observations, not continuous BP measurements. BPUP1 uses a disagreeing cuff pair; BPUP2 (open diamond) uses a late single reading. Separate reference-quality sensitivity tables are provided.

## F03 | Paired BP agreement

The diagonal is perfect agreement. Colors: rest blue, motion orange, challenge red, recovery green. Late BPUP2 is a black-edged diamond. B0 is excluded; no regression fit or post-hoc offset is applied.

## F04 | Descriptive bias and limits of agreement

Bias +/-1.96 sample SD; BP units mmHg, HR units BPM. Limits describe these repeated windows from one participant and are not population limits or clinical validation. BPUP2 late reference retained and marked; BP baseline excluded.

## F05 | Error distributions against the exact attached targets

Required cumulative fractions are >=40%, >=65%, >=85% within 5,10,15 mmHg. Full-cohort DBP reaches 64.7%, not65%, within10mmHg. Strict curves have a different denominator (11 vs17); they do not represent an improvement on the same complete cohort. No BHS grade is assigned.

## F06 | Quality-gate trade-off and the fixed-baseline comparator

Same frozen model; selected quality blocks differ. On the 11 accepted windows, strict and relaxed predictions are identical. A constant B0 comparator outperforms VeinNet even on that same subset. It is a benchmark, not a current BP measurement. Integrity/model/baseline gates are retained in relaxed mode.

## F07 | HR agreement across rest, motion and challenge slots

All18 recordings, including B0: MAE3.38 BPM, 15/18 within5 BPM and17/18 within10 BPM. Green error bars indicate HIGH; orange ARTIFACT. BPUP2 is the late-reference outlier. The attachment does not define its85% agreement tolerance; both bands are shown, not chosen to manufacture a pass.

## F08 | Six HR methods on both channels, plus the tiered ensemble

Exploratory comparison on the same retained data. HIGH is the fixed IR-ensemble mask shared across algorithms, not a validated RED-specific gate. Gated n=14; ungated n=18 except Elgendi RED n=17. Production strict HR has0/18 outputs and undefined error. No model was trained or algorithm selected for deployment using these labels.

## F09 | Direction and magnitude of attempted BP changes

*BPUP1 reference averages disagreeing readings; **BPUP2 cuff is late/single. PRE is a prior rest slot, not an immediately preceding synchronized reading. BPREC1 uses M06 by protocol. BPREC2 is missing. The attempted maneuvers do not establish controlled or standardized BP increases.

## F10 | Acquisition and processing latency are different measurements

Post-acquisition latency is edge emitted_at minus edge acquisition_end: p50/p95/max0.212/0.274/0.280s. BPUP2 has no live latency row. The attachment target30-120s includes processing/display; command-to-render and UI rendering were not measured, so this figure cannot certify that endpoint requirement.

## F11 | Experimental oxygen output - accuracy NOT EVALUABLE

UNCALIBRATED_MAXIM_LINEAR: 18/18 captured outputs, range64.8-95.9%, median90.0%. No independent oxygen reference exists. This is a formula-output distribution, not evidence of hypoxemia, accuracy, or fulfillment of the attached +/-3/4-point requirements. No normal-range band is implied.

## F12a | Every retained optical window (1-9)

Raw RED/IR samples, normalized only for display as100*(sample/median-1); no smoothing, pulse-band filtering, or waveform selection. Each panel uses its own y-scale, so compare morphology within panels, not apparent height across panels. This visual check is not an independent pulse/SQI validation. All18 windows are retained.

## F12b | Every retained optical window (10-18)

Raw RED/IR samples, normalized only for display as100*(sample/median-1); no smoothing, pulse-band filtering, or waveform selection. Each panel uses its own y-scale, so compare morphology within panels, not apparent height across panels. This visual check is not an independent pulse/SQI validation. All18 windows are retained.

## F13 | Exploratory estimator disagreement versus BP error

n=17 post-baseline windows; estimator ranges rounded to 0.01 BPM as in the original analyzer. Correlation is descriptive with ties handled correctly; no BP acceptance threshold was fitted. Good HR agreement does not by itself guarantee accurate BP. This addresses the objective linking signal/HR quality with BP while preserving their distinct validation requirements.