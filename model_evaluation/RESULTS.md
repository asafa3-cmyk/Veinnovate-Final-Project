# Frozen Gate-7 single-point-reference MIMIC ensemble comparison

This document reports only the final fair ensemble-to-ensemble comparison:

- **ResNet-34 Ensemble**: arithmetic mean of three independently trained complete backbones.
- **V6PRO+SSL PersonalDelta 3-Head Ensemble**: one shared frozen encoder and three independently trained delta heads.

No single-model, individual-component, validation-only, V6PRO-noSSL, no-reference, VitalDB numerical, or rolling-recalibration result is part of the public comparison.

## Cohort and evaluation identity

| Property | Frozen value |
|:--|--:|
| Unseen MIMIC-BP test subjects | 214 |
| Scored strictly-later readings | 6,196 |
| Coverage | 100% |
| Rows/SQI/coverage | Identical for both candidates |
| Cycle reference | Identical single-point anchor; anchor row excluded |
| Ensemble size | Three independently trained components per candidate |

V6PRO+SSL PersonalDelta is intermittently cuff-calibrated and cuff-free between calibration rounds. One real cuff measurement establishes the SBP/DBP reference for a calibration cycle; the system then produces repeated PPG-only SBP/DBP estimates without further cuff inflation until the next scheduled recalibration.

The PersonalDelta output is `ΔSBP, ΔDBP`. After a real cuff round, inference uses only MAX30102 PPG, the stored cuff reference, and synchronized baseline PPG. Hundreds of PPG estimates may be produced between cuff rounds. The previous model prediction is never a cuff anchor; a new real cuff measurement resets cuff SBP/DBP, baseline PPG, and elapsed time. In this retrospective evaluation, the frozen reference BP at the first accepted row emulates one cycle anchor:

```text
cycle-reference-corrected BP(t) = anchor BP + prediction(t) − prediction(anchor)
```

## Primary results

| Model | SBP MAE | DBP MAE | SBP bias | DBP bias | SBP error SD | DBP error SD |
|:--|--:|--:|--:|--:|--:|--:|
| ResNet-34 Ensemble | 14.04 | 8.25 | +0.76 | +0.87 | 18.16 | 11.02 |
| **PersonalDelta Ensemble** | **11.74** | **6.80** | −0.85 | −0.76 | **14.90** | **9.00** |

All values are mmHg. PersonalDelta reduced MAE by **16.4% for SBP** and **17.6% for DBP**, using the displayed two-decimal MAEs. Error SD fell by **18.0% for SBP** and **18.3% for DBP**.

<div align="center">
  <img src="../assets/gate7_mae_comparison.png" width="820" alt="Single-point-reference MIMIC MAE comparison"/>
</div>

<div align="center">
  <img src="../assets/gate7_relative_mae_improvement.png" width="760" alt="Relative MAE improvement"/>
</div>

<div align="center">
  <img src="../assets/gate7_error_sd_comparison.png" width="820" alt="Error standard deviation comparison"/>
</div>

## Paired patient-level uncertainty

The bootstrap resampled the 214 subjects as paired clusters, preserving each subject’s rows for both models. It used seed 42 and 10,000 resamples. Negative differences favor PersonalDelta.

| PersonalDelta minus ResNet | Point difference | 95% CI |
|:--|--:|--:|
| SBP MAE | **−2.30 mmHg** | **[−3.03, −1.59]** |
| DBP MAE | **−1.46 mmHg** | **[−1.98, −0.96]** |

Both intervals exclude zero. This is evidence of a paired retrospective difference on the frozen cohort; it is not a prospective clinical-effect estimate.

<div align="center">
  <img src="../assets/gate7_bootstrap_forest.png" width="840" alt="Paired subject-level bootstrap forest plot"/>
</div>

## Efficiency and export

| Model | Mean latency | Parameters |
|:--|--:|--:|
| ResNet-34 Ensemble | 10.73 ms | 21.66M |
| **PersonalDelta Ensemble** | **4.84 ms** | **1.75M** |
| **PersonalDelta reduction** | **54.9%** | **91.9%** |

Latency was measured at batch size 1 in the same process/session on an NVIDIA RTX PRO 4500 Blackwell, after 100 warm-up iterations and across 500 timed iterations. Under this controlled benchmark PersonalDelta was **2.22× faster** and saved **5.89 ms per inference**. Its complete ensemble was **12.4× smaller**. These are controlled relative results, not universal or Jetson latency claims.

<div align="center">
  <img src="../assets/gate7_speed_advantage.png" width="820" alt="Controlled same-session batch-1 GPU speed comparison"/>
</div>

<div align="center">
  <img src="../assets/gate7_parameter_efficiency.png" width="820" alt="Complete-ensemble parameter efficiency comparison"/>
</div>

The PersonalDelta ensemble was exported at **ONNX opset 17**. On eight non-sealed validation parity rows, the maximum absolute difference from the PyTorch reference was **0.001068 mmHg** (publicly rounded to **0.00107 mmHg**). This establishes numerical export parity only.

PersonalDelta executes one shared frozen encoder for all three independently trained heads. ResNet executes three complete independently trained backbones.

<div align="center">
  <img src="../assets/gate7_ensemble_scaling.png" width="900" alt="Shared-encoder versus triple-backbone ensemble scaling"/>
</div>

## Technical appendix: complete descriptive coverage

The following BHS-style percentages are preserved for scientific transparency. They are **not** used in the README, executive summary, badges, or headline figures. No BHS grade, AAMI/ISO compliance, or clinical-device accuracy is claimed.

| Model / target | Within ±5 mmHg | Within ±10 mmHg | Within ±15 mmHg |
|:--|--:|--:|--:|
| ResNet-34 Ensemble — SBP | 23.9% | 45.7% | 62.3% |
| PersonalDelta Ensemble — SBP | 26.8% | 50.9% | 69.7% |
| ResNet-34 Ensemble — DBP | 42.5% | 68.8% | 84.2% |
| PersonalDelta Ensemble — DBP | 47.6% | 77.3% | 90.6% |

<div align="center">
  <img src="../assets/gate7_technical_appendix_bhs_coverage.png" width="900" alt="Technical appendix absolute-error coverage"/>
</div>

These percentages are descriptive properties of the single-point-reference MIMIC retrospective error distribution. They do not establish a device grade or compliance result.

## Provenance

The public aggregate source is [gate7_calibrated_ensemble_v1.json](../figures/source_values/gate7_calibrated_ensemble_v1.json). It records full-precision values, formulas, candidate definitions, classifications, and SHA-256 identities for the frozen local source artifacts:

- `RETROSPECTIVE_REUSED_TEST_REPORT.json` — `85aec605ccf930489b019f40f4568796fa9eb5981a790e4a689e1affa259c665`
- `RETROSPECTIVE_BOOTSTRAP_CI.json` — `75b1ade12fe72024473c93e110895b5de5aaf90b12f62153041569285e12e8c5`
- `SHARED_LATENCY_BENCHMARK.json` — `c6fd2f13bf2d51696f5c3271c7f7252df7af5b8d71e0b91f77a00a552be2a7e7`
- `ONNX_EXPORT_PARITY_REPORT.json` — `4cf9ca568c154841c0d3372c0864906d8b063d037048f3ed9645a80a63319b59`

See [METHODOLOGY.md](METHODOLOGY.md) for the frozen evaluation rules and [LIMITATIONS.md](LIMITATIONS.md) for the boundary of the claims.
