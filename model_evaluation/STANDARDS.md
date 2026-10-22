# Standards scope — no compliance claim

Nothing in this repository establishes AAMI, ISO, IEEE, ESH, or BHS compliance. The work is a retrospective algorithm comparison, not a prescribed clinical investigation of a finished device.

## Why the reported metrics are not a device validation

The repository reports MAE, signed bias, error SD, and descriptive percentages within fixed error thresholds because they are useful statistical summaries. A standards claim additionally depends on the reference method, participant distribution, measurement conditions, device type, calibration procedure, posture/change testing, protocol execution, and prospective study controls.

Those requirements are not satisfied by a frozen retrospective MIMIC-BP model evaluation.

## Relevant official framework

[ISO 81060-3:2022](https://www.iso.org/standard/71161.html) specifies clinical-investigation requirements for continuous automated non-invasive sphygmomanometers, including trending and absolute-accuracy device types. The PersonalDelta prototype is intended to track change relative to a real cuff anchor; a future study would need to classify the device correctly and prospectively test the required accuracy/stability/change behavior.

The 2025 ACC/AHA high-blood-pressure guideline notes that non-cuff BP technologies vary substantially in sensing, calibration, reference methods, measurement conditions, and populations, and that internationally accepted validation protocols are needed before clinical use. This project does not claim to meet such a protocol.

## BHS-style coverage

Percentages within ±5, ±10, and ±15 mmHg are retained only as descriptive error-distribution values in the [technical appendix](RESULTS.md#technical-appendix-complete-descriptive-coverage). The repository does not assign a BHS grade.

## Claims explicitly excluded

- No AAMI/ISO pass or compliance statement.
- No BHS grade.
- No clinical-device accuracy claim.
- No MAX30102 validation.
- No proven 4 h, 6 h, 12 h, or 24 h stability.
- No regulatory, diagnostic, treatment, or safety claim.

## What a future validation needs

At minimum, a prospective study should prespecify:

- the actual wearable sensor and production inference pipeline;
- an independent real-cuff calibration procedure;
- reference-device and observer procedures appropriate to the selected standard;
- participant demographics and BP-distribution requirements;
- posture, motion, treatment, and BP-change conditions;
- timestamped 4 h and 6 h endpoints, with recalibration failures reported;
- patient-level analysis and confidence intervals;
- external-site validation and locked software/model versions.

The proposed 4–6-hour recalibration schedule is an operational prototype policy aligned with common periodic vital-sign observation workflows; it remains to be validated prospectively. MIMIC did not validate a 4-hour, 6-hour, 12-hour, or 24-hour duration.

Primary sources:

- [ISO 81060-3:2022 — official standard page](https://www.iso.org/standard/71161.html)
- [2025 ACC/AHA high-blood-pressure guideline](https://doi.org/10.1161/HYP.0000000000000249)
- [Recommendations for evaluating PPG-based BP algorithms, Communications Medicine (2024)](https://www.nature.com/articles/s43856-024-00555-2)
