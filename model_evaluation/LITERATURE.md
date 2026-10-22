# Literature context and novelty analysis

This review separates intermittently cuff-calibrated tracking from absolute-BP estimation without a subject-specific reference. It does not construct a cross-study leaderboard: datasets, split units, reference inputs, targets, reference methods, hardware, and aggregation differ too much for reported numbers to be ranked directly.

## Primary prior art

### Calibration and paired PPG

[PPG2BP-Net (Scientific Reports, 2023)](https://www.nature.com/articles/s41598-023-35492-y) is direct prior art for subject-specific calibration with paired PPG. Its comparative network receives calibration PPG plus calibration SBP/DBP and a target PPG; testing uses the first two subject segments as calibration inputs. This establishes that paired reference/target PPG and subject-specific BP calibration are not novel here. Its VitalDB cohort, two-reference protocol, architecture, and evaluation differ from this project’s one-anchor MIMIC comparison.

### Direct BP-change prediction

[ΔBP-Net (IEEE Journal of Biomedical and Health Informatics, 2024)](https://pmc.ncbi.nlm.nih.gov/articles/PMC11969577/) directly models PPG-based change in SBP. It evaluates both over-threshold change classification and ΔSBP regression over 40-second PulseDB intervals, including unseen-subject and cross-domain tests. This establishes prior art for direct PPG-to-ΔBP learning. It does not report the same ΔSBP+ΔDBP-to-final-cuff formulation or the shared-head ensemble evaluated here.

### Lightweight deployment

[Burrello et al. (IEEE BioCAS, 2024)](https://doi.org/10.1109/BioCAS61083.2024.10798404) use hardware-aware neural architecture search and quantization for PPG BP networks deployed on the GAP8 ultra-low-power SoC. Lightweight BP inference and deployment-aware optimization are therefore not novel. Their hardware, quantization, datasets, and task differ from this repository’s unquantized same-session GPU comparison, so latency or energy values should not be compared directly.

### Teacher–student, knowledge distillation, and cross-modal learning

- [Kyung et al. (IEEE EMBC, 2023)](https://pubmed.ncbi.nlm.nih.gov/38082962/) distill a multimodal teacher using PPG, ECG, and subject attributes into a PPG-only student. It is direct prior art for privileged multimodal information during training and PPG-only inference.
- [Tang et al. (Cardiovascular Engineering and Technology, 2024)](https://pubmed.ncbi.nlm.nih.gov/38191807/) apply teacher–student knowledge distillation to PPG/ECG BP estimation. It confirms that KD and ECG/PPG BP learning are established ingredients.
- [Region-Disentangled Diffusion Model (AAAI, 2024)](https://ojs.aaai.org/index.php/AAAI/article/view/29422) learns PPG-to-ECG translation and evaluates downstream tasks including BP estimation. It is relevant cross-modal representation/translation prior art, although its objective is not the same PersonalDelta ensemble.

The bounded primary-source review found strong BP-specific teacher–student, KD, and PPG-to-ECG cross-modal overlap. It did not establish that the exact frozen-ECG-teacher pretraining recipe plus three shared-encoder PersonalDelta heads has already been published. That absence is a search result, not proof of first use.

## Protocol families

| Protocol family | Subject-specific reference at inference? | Typical output | Honest relation to this work |
|:--|:--:|:--|:--|
| Absolute BP without subject-specific reference | No | Absolute SBP/DBP | Different task; do not rank against the cycle-referenced result |
| Subject-referenced absolute BP | Yes | Absolute SBP/DBP | Related, but reference count/timing and cohort must match |
| Direct BP-change tracking | Sometimes | ΔSBP or change class | Strong conceptual overlap; target and horizon may differ |
| This work | One real cuff anchor per prototype cycle | ΔSBP + ΔDBP, then final SBP/DBP | Directly comparable only to ResNet under the identical frozen retrospective rule |

Metrics are genuinely comparable only when target definitions, subject separation, calibration inputs, cohort, row aggregation, and reference methods align. Parameter counts require complete-candidate accounting. Latency additionally requires identical hardware, runtime, batch size, warm-up, and measurement code.

## Novelty matrix

`NR` means not reported in the cited primary source; it is not evidence that the feature is absent.

| Work | Predicted output | Calibration/reference input | PPG-only inference | ECG privileged during training only | Ensemble encoder design | Heads / backbones | Parameters / latency / target | Subject-separated evaluation | Patient-level uncertainty |
|:--|:--|:--|:--:|:--:|:--|:--|:--|:--:|:--:|
| PPG2BP-Net (2023) | Final SBP+DBP | Two calibration PPGs + SBP/DBP | Yes, plus calibration values | No | Paired weight-sharing CNN branches; not an ensemble | Paired CNN feature paths | NR / NR / calibrated monitoring | Yes | Not patient-bootstrap CI |
| ΔBP-Net (2024) | ΔSBP regression and SBP-change class | Paired temporal PPG interval; no absolute cuff anchor for its primary change task | Yes | No | Single SCM model | Single model | NR / NR / change monitoring | Yes | NR |
| Kyung et al. (2023) | Absolute SBP+DBP | No single cuff reference described | Yes, student | Yes: ECG and attributes in teacher | Teacher and student are separate during training | One deployable student | NR / NR / PPG-only inference | Not established here | NR |
| Tang et al. (2024) | Absolute SBP+DBP | No single cuff reference described | Not established from abstract | PPG+ECG KD overlap | Teacher–student | One student at deployment | NR / NR / compact inference | Reported cohort; split details require full-protocol review | NR |
| Burrello et al. (2024) | Absolute BP or reconstructed BP waveform | Dataset/model dependent | Yes for optimized PPG networks | No | Single optimized networks | Single deployed network | Architecture-dependent / GAP8 / low-power wearable SoC | Dataset-dependent | NR |
| **This work** | **ΔSBP+ΔDBP; final SBP+DBP = cuff + delta** | **One real cuff anchor per cycle; retrospective emulation excluded from scoring** | **Yes, between cuff rounds** | **Yes: ECG informs frozen SSL encoder training only** | **One shared frozen encoder across ensemble heads** | **3 heads / 1 encoder versus 3 ResNet backbones** | **1.75M / 4.84 ms controlled GPU / future edge prototype** | **Yes: 214 unseen test subjects** | **Yes: paired 10,000-resample patient bootstrap** |

## Paper hypothesis

> An amortized shared frozen V6PRO+SSL encoder with three independently trained PersonalDelta heads provides statistically significant single-point-per-cycle SBP/DBP improvement while avoiding the triple-backbone cost of a conventional ensemble.

### Verified contribution

- Identical 214-subject cohort, 6,196 later readings, SQI, coverage, and one-point anchor for both complete candidates.
- Lower SBP/DBP MAE and error SD for PersonalDelta; both paired patient-bootstrap MAE confidence intervals exclude zero.
- One encoder execution for three heads versus three complete ResNet backbone executions.
- 1.75M versus 21.66M parameters and 4.84 versus 10.73 ms in the controlled same-session batch-1 GPU benchmark.
- Verified ONNX numerical parity for the complete PersonalDelta ensemble.

### Apparently underexplored combination

The literature supports each ingredient separately. The potentially publishable combination is the amortization pattern: one frozen ECG-informed PPG encoder, three independently trained dual-delta heads, single-point-per-cycle final-BP reconstruction, subject-separated paired evaluation, and complete-candidate accuracy/latency/parameter accounting. This should be presented as apparently underexplored pending a formal systematic search—not as first-ever.

### Known prior-art overlap

- Subject-specific calibration and paired reference/target PPG: PPG2BP-Net and earlier calibration literature.
- Direct BP-change learning: ΔBP-Net.
- ECG/PPG privileged-information transfer and KD: Kyung et al., Tang et al., and cross-modal translation work.
- Ensembles and independent heads: established machine-learning practice.
- Lightweight and hardware-aware BP inference: Burrello et al. and related deployment literature.

### Experiments required before claiming architectural novelty

1. **Encoder sharing ablation:** three full V6PRO+SSL encoders + three heads versus one shared encoder + three heads, with identical training seeds and ensemble members.
2. **Ensemble-value ablation:** one head, two heads, and three heads; report MAE, error SD, calibration, diversity, parameters, and latency.
3. **SSL/privileged-information ablation:** random initialization, PPG-only SSL, ECG-teacher SSL, frozen versus fine-tuned encoder.
4. **Target ablation:** absolute BP, ΔSBP only, independent ΔSBP+ΔDBP, and final cuff-plus-delta reconstruction.
5. **Calibration ablation:** no anchor, one anchor, and two anchors; predeclare timing and exclude all anchors from scoring.
6. **Baseline-strength ablation:** compare shared-head PersonalDelta with parameter-matched and latency-matched non-ResNet baselines, not only a large ResNet ensemble.
7. **Generalization:** locked external subject-separated cohort and a prospective timestamped real-cuff/wearable study.
8. **Deployment:** ONNX/TensorRT measurements on the actual target device, including memory, energy, thermal behavior, and numerical parity; no Jetson speed claim before this test.

## Recommended paper case

**Title:** *V6PRO+SSL PersonalDelta: A Lightweight Calibrated Ensemble for Retrospective Blood-Pressure Tracking from PPG.*

**Contribution statements:**

1. We formulate final SBP/DBP as a single-point cuff reference per cycle plus jointly predicted ΔSBP/ΔDBP.
2. We amortize one frozen ECG-informed PPG encoder across three independently trained PersonalDelta heads.
3. We compare the complete shared-encoder ensemble with a three-backbone ResNet-34 ensemble on identical subjects, rows, SQI, coverage, and anchors.
4. We quantify paired patient-level uncertainty and show both SBP and DBP MAE intervals exclude zero.
5. We report complete-candidate parameters, controlled batch-1 GPU latency, and ONNX parity alongside accuracy.

**Strongest category:** retrospective methods/engineering paper. The current evidence is stronger for algorithm design, paired evaluation, and efficiency than for clinical performance.

**Potential venues, subject to scope and completed ablations:** IEEE Journal of Biomedical and Health Informatics; IEEE Transactions on Biomedical Circuits and Systems; Biomedical Signal Processing and Control; Physiological Measurement; IEEE EMBC; IEEE BioCAS. JBHI is the strongest thematic target if the ablation and external-validation package is expanded; BioCAS is a natural engineering venue after real target-hardware measurements.

## Clinical and standards boundary

The complete prototype requires intermittent cuff measurements: one real cuff measurement starts each calibration cycle, followed by cuff-free monitoring in which inference uses MAX30102 PPG, the stored cuff reference, and synchronized baseline PPG. Hundreds of PPG estimates may be produced before the next round; a prediction is never a cuff anchor, and each new cuff measurement resets cuff SBP/DBP, baseline PPG, and elapsed time.

The proposed 4–6-hour recalibration schedule is an operational prototype policy aligned with common periodic vital-sign observation workflows; it remains to be validated prospectively. MIMIC did not validate any 4-hour, 6-hour, 12-hour, or 24-hour duration. A future clinical paper requires real cuff anchors, a production wearable sensor, verified elapsed time, prespecified endpoints, representative BP/demographic ranges, motion/posture testing, external sites, and a standards-appropriate reference protocol. The [standards scope](STANDARDS.md) lists the excluded claims.
