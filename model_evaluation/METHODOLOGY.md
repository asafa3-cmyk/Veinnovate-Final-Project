# Methodology — frozen single-point-reference MIMIC ensemble comparison

## Data and common evaluation rows

- Dataset: official MIMIC-BP retrospective test split.
- Subjects: 214 unseen test subjects.
- Accepted input rows: 6,410 before anchor exclusion.
- Evaluated rows: 6,196 strictly-later readings.
- Signals used by the trained pipeline: PPG plus frozen morphology/demographic inputs where defined by the candidate architecture.
- Reference targets: SBP/DBP derived from the invasive arterial-pressure waveform in MIMIC-BP.
- SQI: every row already admitted by the frozen Gate-4 simultaneous-signal/SQI preprocessing; no post-test threshold selection.
- Coverage: all eligible later rows were required; observed coverage was 100% for both candidates.

No row, subject, SQI threshold, or coverage rule differs between the two candidates.

## Candidates

### ResNet-34 Ensemble

Three independently trained complete ResNet-34 models. Their frozen predictions are averaged in float64 before applying the single-point cycle reference.

### V6PRO+SSL PersonalDelta 3-Head Ensemble

One shared frozen V6PRO+SSL encoder with three independently trained PersonalDelta heads. The three head outputs are averaged before applying the cycle reference. The candidate predicts `ΔSBP` and `ΔDBP`; final SBP/DBP are obtained by adding these deltas to the latest real cuff measurement.

V6PRO+SSL PersonalDelta is intermittently cuff-calibrated and cuff-free between calibration rounds. One real cuff measurement establishes the SBP/DBP reference for a calibration cycle; the system then produces repeated PPG-only SBP/DBP estimates without further cuff inflation until the next scheduled recalibration.

The candidate designs are intentionally different. The comparison is fair at the deployed-candidate level: each receives identical rows and an identical single-point cycle reference, while parameter count and latency reflect the actual ensemble construction.

## Single-point calibration per cycle

For each subject, the anchor was selected by the frozen outcome-independent rule:

```text
argmin(segment_id, original_row_index)
```

The anchor row was excluded. A row was eligible only when:

```text
segment_id > anchor segment_id
```

The retrospective single-point correction was applied identically:

```text
corrected_prediction(t)
  = anchor_reference_BP + prediction(t) − prediction(anchor)
```

The retrospective anchor reference emulates one calibration cycle. Deployment requires an actual real cuff value. After that measurement, repeated inference uses only MAX30102 PPG, the stored cuff SBP/DBP reference, and synchronized baseline PPG; hundreds of estimates may be produced without further cuff inflation. The previous model prediction is never accepted as a cuff anchor. A new real cuff measurement resets cuff SBP/DBP, baseline PPG, and elapsed time.

The MIMIC experiment is not a prospective cuff or duration validation.

## Metrics

- Mean absolute error (MAE), separately for SBP and DBP.
- Signed bias: mean of `prediction − target`.
- Error standard deviation: sample-level SD of signed errors.
- Descriptive percentages within ±5, ±10, and ±15 mmHg, retained only in the technical appendix without grade/compliance claims.
- Counts and coverage.
- Mean latency and parameter count for each complete ensemble.
- PyTorch-to-ONNX numerical parity for the PersonalDelta export.

Public relative MAE improvements follow the predeclared display convention and are calculated from the two-decimal headline MAEs:

```text
100 × (ResNet MAE − PersonalDelta MAE) / ResNet MAE
```

This yields 16.4% SBP and 17.6% DBP.

## Paired bootstrap

- Resampling unit: subject.
- Pairing: both models’ statistics remain paired within each resampled subject.
- Statistic: mean subject-level MAE difference, PersonalDelta minus ResNet.
- Resamples: 10,000.
- RNG seed: 42.
- Interval: percentile 2.5% to 97.5%.

Subject-level resampling prevents subjects with more rows from being treated as independent replicated patients.

## Efficiency protocol

- Hardware: NVIDIA RTX PRO 4500 Blackwell.
- Device: CUDA.
- Batch size: 1.
- Same process and session for all candidates.
- Warm-up iterations: 100.
- Timed iterations: 500.
- Reported statistic: mean latency.

The ResNet parameter count sums three full backbones. PersonalDelta counts one shared encoder plus three heads.

## ONNX parity

- Candidate: complete PersonalDelta 3-head ensemble.
- Opset: 17.
- Runtime: ONNX Runtime CPUExecutionProvider.
- Rows: eight non-sealed validation rows.
- Maximum absolute difference: 0.001068115234375 mmHg.

This test verifies numerical export agreement, not accuracy on an external device or dataset.

## Time and recalibration scope

The official MIMIC-BP row order does not include a verified timestamp/record-start sidecar for the frozen 214-subject cohort. Therefore:

- no 4 h or 6 h MIMIC rolling-recalibration result is reported;
- row index is not interpreted as elapsed time;
- consecutive 30-second segments are not assumed;
- no 12 h or 24 h stability claim is made.

The proposed 4–6-hour recalibration schedule is an operational prototype policy aligned with common periodic vital-sign observation workflows; it remains to be validated prospectively. MIMIC did not validate any 4-hour, 6-hour, 12-hour, or 24-hour monitoring duration.

## Reproducibility boundary

The public [aggregate source file](../figures/source_values/gate7_calibrated_ensemble_v1.json) and [figure generator](../figures/generate_gate7_calibrated_ensemble_figures.py) reproduce all displayed values and figures without protected patient data. Raw waveforms, derived arrays, predictions, targets, and checkpoints remain in the credentialed local research workspace under the PhysioNet data-use agreement.
