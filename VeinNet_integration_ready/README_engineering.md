# VeinNet_integration_ready

Minimal, self-contained, deployment-only VeinNet blood-pressure inference runtime. Meant to be moved as a sibling folder into `Veinnovate_Prototype/`, alongside `Hardware_integration_ready/` and `VeinoGate_Dashboard_integration_ready/`.

## Purpose

Wraps the frozen `personaldelta_ensemble_42_43_44.onnx` model (3-seed ensemble, PPG + single-point cuff calibration -> personal-delta absolute BP) with the exact frozen signal preprocessing, morphology extraction, and demographics transform it was trained against, behind one stable Python entry point (`veinnet_runtime.VeinNetRuntime`) that the Hardware/Jetson component can call after integration. No training code, no PyTorch, no research data, no PHI.

## Installation

Requires **Python >= 3.10** (`veinnet_runtime` uses PEP 604 union type hints, e.g. `str | Path`; the package must not be installed under an older interpreter).

### For `Hardware_integration_ready` (recommended): editable install of the sibling folder

This is the integration mechanism `Hardware_integration_ready` should use instead of manipulating `sys.path` itself:

```bash
cd Hardware_integration_ready        # or wherever the consuming project's venv lives
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ../VeinNet_integration_ready
```

After this, `from veinnet_runtime import VeinNetRuntime, BLEPacket` works from any working directory — the package resolves its own `contracts/`, `model/`, and `golden/` resources relative to its real on-disk location (`Path(__file__).resolve().parents[1]`), not relative to the caller's cwd, and not via any absolute path written into the bundle.

**Only editable installation (`pip install -e`) is currently supported.** A normal (non-editable) wheel build is *not* currently supported — see `pyproject.toml`'s installation-contract comment and "Known limitations" below for why, and do not attempt a wheel install until that is explicitly fixed and re-tested.

### For standalone/manual use: plain dependency install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
```

See `contracts/dependency_lock.json` — the pinned versions (`candidate_runtime_lock`) are verified on two development machines (see Provenance below); Jetson/aarch64 wheel availability for these exact pins is **pending verification** (`jetson_platform_verification: "pending"`) and must be confirmed on the actual target device before shipping (see Known limitations).

## Runtime invocation

```python
from veinnet_runtime import VeinNetRuntime, BLEPacket

runtime = VeinNetRuntime.load()  # strict hash + opset + I/O verification, fail-closed

# 1. Raw MAX30102 packet windows -> ppg/morphology (or an ABSTAIN reason)
calibration = runtime.assemble_and_preprocess([...calibration BLEPacket objects...])
current     = runtime.assemble_and_preprocess([...current BLEPacket objects...])
assert calibration["accepted"] and current["accepted"]

# 2. Frozen demographics transform
demographics = runtime.demographics(age=58.0, sex="F")  # sex must be exactly "F", "M", or None

# 3. Elapsed time -> elapsed_segments unit (pure conversion; NO expiry policy enforced here)
elapsed = runtime.elapsed_segments(elapsed_seconds=1800.0)

# 4. Inference on the 7 preprocessed inputs -> personal_bp [B,2] = [SBP, DBP]
import numpy as np
output = runtime.infer(
    ppg=current["ppg"][None, ...],
    morphology=current["morphology"][None, ...],
    baseline_ppg=calibration["ppg"][None, ...],
    baseline_morphology=calibration["morphology"][None, ...],
    demographics=demographics[None, ...],
    baseline_cuff=np.array([[120.0, 80.0]], dtype=np.float32),
    elapsed_segments=np.array([[elapsed]], dtype=np.float32),
)
```

## Stable interface exposed to the Jetson/Hardware component

`veinnet_runtime.VeinNetRuntime` exposes each pipeline stage as a separate, independently tested operation:

| Method | Purpose |
|---|---|
| `VeinNetRuntime.load(model_path=None, providers=(...))` | Strict model load: SHA-256 verification (fail-closed), then loaded I/O names/ranks/static-shapes/dtypes checked against `contracts/model_contract.json`. |
| `.assemble_and_preprocess(packets: list[BLEPacket])` | Raw MAX30102 packet windows -> `{"accepted": True, "ppg", "morphology", "sqi"}` or `{"accepted": False, "reason": ...}`. |
| `VeinNetRuntime.demographics(age, sex)` | Frozen `(age, sex) -> [age_z, is_female, age_missing, sex_missing]` transform. `sex` must be exactly `"F"`, `"M"`, or `None`. |
| `VeinNetRuntime.demographics_batch(rows)` | Deterministic batch form of the above. |
| `VeinNetRuntime.elapsed_segments(elapsed_seconds)` | `elapsed_seconds / 10.0`, with input validation only — no expiry policy. |
| `.infer(ppg, morphology, baseline_ppg, baseline_morphology, demographics, baseline_cuff, elapsed_segments)` | Validates all 7 inputs as float32 with exact names/ranks/shapes, runs the ONNX ensemble, validates the output is finite float32 `[B,2]`, returns `personal_bp`. |

Once the three `*_integration_ready` folders are siblings, `import veinnet_runtime` works from this folder alone — nothing in this package imports from the original research repository.

## Exact I/O and preprocessing contracts

Machine-readable, in `contracts/`:

- `model_contract.json` — ONNX opset (17), exact input/output names/shapes/dtypes, output semantics (`personal_bp = baseline_cuff + mean(3-seed deltas)`, absolute `[SBP, DBP]`), `elapsed_segments` unit definition.
- `preprocessing_contract.json` — frozen MAX30102 gating constants (`target_hz=100, window_seconds=30, bandpass_hz=[0.5,8.0]`, packet-loss/clipping/contact/correlation/pulsatility thresholds), source file + hash, extraction notes, and a documented (not silently fixed) quirk — see Known limitations.
- `morphology_contract.json` — 13-feature morphology transform constants and source hash (verbatim copy).
- `demographics_contract.json` — demographics transform constants, canonical sex values, and the explicit note that `demographics_transform()` is a new direct implementation of the frozen manifest contract, not verbatim training source.
- `dependency_lock.json` — Jetson/aarch64 candidate runtime dependency lock (`candidate_runtime_lock`), `python_requires: ">=3.10"`, with `jetson_platform_verification: "pending"` clearly separated from `export_provenance.json`.
- `export_provenance.json` — the ORIGINAL export/parity environment and PyTorch-vs-ONNX parity numbers, kept separate from the deployment lock.

### Model I/O (opset 17)

| Input | Shape | Dtype |
|---|---|---|
| `ppg` | `[B, 3, 1000]` | float32 |
| `morphology` | `[B, 13]` | float32 |
| `baseline_ppg` | `[B, 3, 1000]` | float32 |
| `baseline_morphology` | `[B, 13]` | float32 |
| `demographics` | `[B, 4]` | float32 |
| `baseline_cuff` | `[B, 2]` | float32 |
| `elapsed_segments` | `[B, 1]` | float32 |

Output `personal_bp`: `[B, 2]` float32, absolute `[SBP, DBP]`.

## Provenance and frozen hashes

- **Model**: `model/personaldelta_ensemble_42_43_44.onnx`, SHA-256 `cb320a28b0abd48c2f6e34828c83b50ecce3c1f0c30f7fc8402b964cd74c8cd0` — copied byte-for-byte, verified unchanged after copy, unmodified.
- **Preprocessing source**: `gate7_max30102_personaldelta/max30102.py`, SHA-256 `bcd384980b4de75d3bf9462fbf7c6c8494296f526c3f69f0bd9cd925b04b8db0` (see `contracts/preprocessing_contract.json`).
- **Morphology source**: `gate5_v6pro/morphology.py`, SHA-256 `0f88ef58da58d7ceb563853f59425b947b43eb0dd229630efe6664c8bb8add13` — copied verbatim (see `contracts/morphology_contract.json`).
- **Demographics manifest**: `gate7_max30102_personaldelta/gemini_stage/demographics/MANIFEST.json`, SHA-256 `fef6d1e62dcdf75bd55b5eee9222ba555b85d3db48320cc588313f58d4f21e24` — `demographics_transform()` is a new direct implementation of this manifest's contract, approved 2026-09-01 (see `contracts/demographics_contract.json`).
- **PyTorch-vs-ONNX parity** (authoritative, from `gate7_max30102_personaldelta/results/ONNX_EXPORT_PARITY_REPORT.json`): max abs diff `~0.001068`, mean abs diff `~0.000276`, 8 non-sealed validation rows, opset 17. Recorded in `contracts/export_provenance.json` without reproducing real input values.
- **Golden fixtures** (`golden/*.npz`): 100% synthetic, seeded, non-PHI. These are **regression** fixtures (this code vs. itself), separate from the parity evidence above — see `golden/GOLDEN_FIXTURES_PROVENANCE.json`.

## Test commands

Runtime and test dependencies are locked in **two separate files** — `requirements.lock.txt` never includes pytest, and test/reproducibility instructions never claim otherwise:

```bash
python -m pip install -r requirements.lock.txt -r requirements-test.lock.txt
python -m pytest tests/ -q
```

**71 tests** covering: hash verification, fail-closed ONNX loading and contract-mismatch rejection, MAX30102 characterization (packet loss, clipping, contact, correlation, pulsatility, resampling, window/nonfinite rejection, output shape, morphology generation), morphology unit tests, demographics unit tests (female/male/missing-age/missing-sex/unknown-sex-rejected/complete/batched), elapsed-seconds-to-segments boundary tests, strict input/output shape-and-dtype validation at the `infer()` boundary, the two golden-regression fixtures, an end-to-end entry-point smoke test, and an editable-install/import test (`tests/test_editable_install.py`) that installs the package with `pip install -e` into a fresh subprocess and imports it from a directory unrelated to the bundle.

For the exact Python/pytest versions this suite was last verified against, and the full reproducibility report (regenerated manifest, relocated-copy install, golden inference, no-external-imports/no-absolute-paths/no-symlinks confirmation), see **"Reproducibility report"** below.

Note: running `test_editable_install.py` performs a real `pip install -e .` of this bundle, which causes `setuptools` to write a `veinnet_runtime.egg-info/` directory into the bundle root as a normal side effect. This is build/install metadata, not a shipped runtime file — delete it (`rm -rf veinnet_runtime.egg-info`) before regenerating `bundle_manifest.json` or transferring the bundle.

### Portability test

To confirm the bundle has no hidden dependency on the source repository:

```bash
TMP=$(mktemp -d)
cp -R VeinNet_integration_ready "$TMP"/
cd "$TMP"/VeinNet_integration_ready
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.lock.txt -r requirements-test.lock.txt
python -m pytest tests/ -q
grep -rIl "/Users/" . || echo "no absolute paths found"
find . -type l          # should print nothing (no symlinks, excluding the venv you just created)
```

## Known limitations

- **`sqi.clipped_fraction` is always `0.0` from `preprocess()`.** The frozen source (`gate7_max30102_personaldelta/max30102.py`) computes a local `clipped = 0.0` that is never updated before being returned — a real clipping check already happens earlier, in `assemble_packets()`. This is a pre-existing quirk in the frozen implementation, preserved exactly (not silently fixed) and documented in `contracts/preprocessing_contract.json`. Flagged for review upstream.
- **Jetson/aarch64 dependency wheel availability is unverified.** `contracts/dependency_lock.json`'s `candidate_runtime_lock` pins the exact versions used in the original export/parity run on an x86_64 CUDA workstation, re-verified on a second machine (macOS arm64); whether those wheels build/install on the target JetPack release has not been checked. Marked `"jetson_platform_verification": "pending"`, not assumed compatible. This may remain pending until the target Jetson device is available.
- **Only editable installation is supported, not a normal wheel.** `setuptools` (per `pyproject.toml`) only packages the `veinnet_runtime` Python module tree into a wheel — it does not bundle `contracts/`, `model/`, or `golden/` as package data. A wheel built and installed from this bundle today would fail closed at `VeinNetRuntime.load()` (hash/contract files not found) rather than silently doing the wrong thing, but it is not usable. Fixing this (declaring package-data, or restructuring resource resolution to use `importlib.resources`) is out of scope for this packaging pass. Use `pip install -e ../VeinNet_integration_ready` only.
- **No PyTorch checkpoint fallback.** This bundle is ONNX Runtime-only by design; if the ONNX graph ever needs re-export, that must happen from the original research repository, not from this bundle.
- **Calibration-expiry policy is not enforced here.** `elapsed_seconds_to_segments()` performs only the frozen unit conversion (`/10.0`); deciding whether a given elapsed time still permits inference (warning at 14400s/4h, hard expiry at 21600s/6h) is `Hardware_integration_ready`'s responsibility.

## Condensed deployment history

- **2026-08-27**: Gate-7 `personaldelta_ensemble_42_43_44.onnx` exported (opset 17) and PyTorch-vs-ONNXRuntime parity-checked on 8 non-sealed validation rows (`ONNX_EXPORT_PARITY_REPORT.json`).
- **2026-09-01**: Read-only audit of the source repository performed; runtime dependency graph, frozen hashes, and exclusion list confirmed against source files (see conversation record / `AUDIT.md` in the source repository).
- **2026-09-01**: `VeinNet_integration_ready/` built from the audited files: ONNX copied byte-for-byte and hash-reverified; `assemble_packets()`/`preprocess()` extracted with behavior unchanged; `morphology.py` copied verbatim; a new `demographics_transform()` written and approved as a direct implementation of the frozen manifest contract; `recalibration_seconds` (86400s) demoted to non-active provenance per instruction, with the 4h/6h operational policy assigned to `Hardware_integration_ready`; export-time provenance and Jetson deployment lock separated into distinct contract files; 70 tests written and passing (characterization, golden-regression, contract-validation, entry-point smoke).
- **2026-09-01 (packaging pass)**: `python_requires` corrected to `>=3.10` everywhere (PEP 604 unions in the source require it, even though `from __future__ import annotations` defers their evaluation); added `pyproject.toml` declaring editable-install-only support with an explicit rationale for why a normal wheel is not yet usable; added `requirements-test.lock.txt` pinning the exact pytest version used to verify the suite, separate from `requirements.lock.txt`; renamed `dependency_lock.json`'s pins to `candidate_runtime_lock` with `jetson_platform_verification: "pending"` so they are never described as a validated Jetson lock; added `tests/test_editable_install.py`, exercising `pip install -e` and import from an unrelated directory in a real subprocess (71 tests total); re-ran the full mandatory portability procedure and regenerated `bundle_manifest.json` — see "Reproducibility report" below for exact versions and results.

## Reproducibility report (packaging pass, 2026-09-01)

Performed after the `python_requires` correction, `pyproject.toml` addition, and lock-file split described above, using a venv built from **only** `requirements.lock.txt` + `requirements-test.lock.txt` (no reuse of any pre-existing environment):

- **Interpreter**: CPython **3.12.5** (macOS arm64; JetPack's interpreter version must still be confirmed separately — see `contracts/dependency_lock.json`).
- **Installed via lock files**: `numpy==2.4.6`, `scipy==1.17.1`, `onnxruntime==1.29.0` (from `requirements.lock.txt`), `pytest==9.1.1` (from `requirements-test.lock.txt`) — all four resolved and installed with no other packages requested.
- **Test suite**: `python -m pytest tests/ -q` → **71 passed**, 0 failed, 0 skipped (includes `tests/test_editable_install.py`, which performs its own real `pip install -e` + subprocess import from an unrelated directory as part of the run).
- **`bundle_manifest.json`**: regenerated after the above run and after deleting the `veinnet_runtime.egg-info/` directory the editable install left behind (build metadata, not a shipped file — see "Test commands" above).
- **Separately, a manual relocated-copy verification** (not just the automated `test_editable_install.py`) was also performed end to end: copied `VeinNet_integration_ready/` to a directory outside the source repository, `cd`'d into the copy, created a fresh venv, ran `pip install -e .` from inside the copy, `cd`'d to a third, fully unrelated directory, and from there ran `python -c "from veinnet_runtime import VeinNetRuntime, BLEPacket; rt = VeinNetRuntime.load(); ..."` — import succeeded, `VeinNetRuntime.load()` succeeded (SHA-256 hash-verified the bundled ONNX and validated its input/output contract before returning), and a golden-regression inference call (`tests/test_onnx_golden_regression.py`'s fixture, run manually against the loaded runtime) matched the frozen fixture output. The temporary copy was deleted afterward.
- **No external imports, absolute paths, or symlinks**: confirmed by grepping every shipped `*.py`/`*.json`/`*.md` file for `/Users/` (none found outside the illustrative `grep` command in this README's own "Portability test" section) and for `find . -type l` (no symlinks in the bundle content; only the throwaway test venvs created during verification contain the usual Python interpreter symlinks, and those venvs are not part of the bundle).

## Categories intentionally excluded from this runtime bundle

- All training code (`campaign.py`, `data.py`/`Gate7Dataset`, `models.py`, `ensemble.py`, `teacher_cache.py`, `corrective_retry.py`, `latency_bench.py`, SSL/GRL modules).
- `MAX30102Runtime` (PyTorch-checkpoint-based inference class) and every `.pt` checkpoint anywhere in the source tree — the ONNX graph contains the frozen weights; PyTorch is not a runtime dependency.
- `personaldelta_seed42_primary.onnx` (single-seed variant; the ensemble is the selected production artifact).
- All training/evaluation diagnostics (`RETROSPECTIVE_*`, `VITALDB_*`, `ROLLING_RECALIBRATION_*`, plots, logs, `CAMPAIGN_REPORT.json`, `history.json`).
- `external_sealed/`, `external_manifests/`, `retrieved/`, `retrieved_upstream/` — VitalDB/MIMIC-derived data and upstream checkpoints.
- Real per-subject demographic arrays (`gemini_stage/demographics/{train,validation,test}/*.npy`, 1006/175/214 subjects) — only the two scalar normalization constants were extracted.
- ResNet-34 comparison code, older K=32/125Hz/9-demographic/`reference_bp` VeinNet implementations, and export-experiment code.
- The nested public GitHub repository (`Veinnovate_VeinNet_Vs_ResNet34/`) in its entirety.
- All Markdown documents from the source tree except this one file.
