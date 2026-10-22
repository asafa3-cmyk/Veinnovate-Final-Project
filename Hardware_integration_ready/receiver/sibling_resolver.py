"""sibling_resolver.py — discovers the sibling VeinNet_integration_ready
component through THAT bundle's own bundle_manifest.json, rather than
duplicating a hardcoded path/hash string in this bundle's config.

This is a metadata-only pre-flight check: it verifies the sibling's
declared bundle name/version and its manifest's declared model SHA-256/
opset against the pinned contract, and confirms the artifact file exists —
it never opens or hashes the (5.8MB) ONNX file itself. The AUTHORITATIVE
hash + I/O-contract verification of the actual file bytes is owned entirely
by the sibling's own `veinnet_runtime.onnx_loader` (invoked via
`VeinNetRuntime.load()` in veinnet_sibling_adapter.py) — Hardware does not
independently load the same ONNX through a second, competing runtime.

Bug fixed during the E2E hardening pass: an earlier draft of this resolver
looked for a `model_artifact` key with a `path` field. The sibling's actual
bundle_manifest.json (verified by reading it directly) uses a `model` key
with a `relative_path` field. Both are now accepted (the correct key
checked first), and this file list every field name transcribed exactly as
read from the real sibling artifact, not guessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

EXPECTED_SIBLING_BUNDLE_NAME = "VeinNet_integration_ready"
MODEL_ARTIFACT_BASENAME = "personaldelta_timeindependent_ensemble_42_43_44.onnx"

# Pinned contract, independently verified against the authoritative Gate 7
# research artifact (see config/inference_manifest.json for the full
# verification record: direct SHA-256 computation, ONNX export parity
# report, and exporter source reading).
EXPECTED_ONNX_SHA256 = "1f017653c5c4bba4ee1a96e97e18d4c97f672ce32f98c0e60f3bd31a5eb06e03"
EXPECTED_ONNX_OPSET = 17


class SiblingResolutionError(RuntimeError):
    """Raised when the sibling bundle cannot be located, or its manifest
    does not match the pinned model contract. Fatal at startup — never
    caught to fall back to a guessed or unverified path."""


@dataclass
class ResolvedModelArtifact:
    onnx_path: Path
    sha256: str
    opset: int | None
    sibling_bundle_name: str
    sibling_bundle_version: str
    sibling_root: Path


def load_sibling_paths_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise SiblingResolutionError(
            f"sibling paths config not found at {config_path} — copy "
            "config/sibling_paths.example.json to config/sibling_paths.json first"
        )
    return json.loads(config_path.read_text())


def resolve_veinnet_sibling_root(bundle_root: Path, sibling_config: dict) -> Path:
    """Resolves ../VeinNet_integration_ready (or the configured relative
    path) to an absolute path. Never reads an absolute path from config."""
    sibling_rel = sibling_config.get("veinnet_integration_ready")
    if not sibling_rel:
        raise SiblingResolutionError("sibling_paths config missing 'veinnet_integration_ready'")
    return (bundle_root / sibling_rel).resolve()


def resolve_model_artifact(bundle_root: Path, sibling_config: dict) -> ResolvedModelArtifact:
    """bundle_root: this bundle's own root (Hardware_integration_ready/),
    used only to resolve the sibling's *relative* path from config — never
    an absolute path is read from configuration."""
    sibling_root = resolve_veinnet_sibling_root(bundle_root, sibling_config)
    manifest_path = sibling_root / "bundle_manifest.json"
    if not manifest_path.exists():
        raise SiblingResolutionError(
            f"sibling bundle manifest not found at {manifest_path} — "
            "VeinNet_integration_ready must be present and built before production startup"
        )

    manifest = json.loads(manifest_path.read_text())

    bundle_name = manifest.get("bundle_name")
    bundle_version = manifest.get("bundle_version")
    if bundle_name != EXPECTED_SIBLING_BUNDLE_NAME:
        raise SiblingResolutionError(
            f"unexpected sibling bundle_name {bundle_name!r}, expected {EXPECTED_SIBLING_BUNDLE_NAME!r}"
        )
    if not bundle_version:
        raise SiblingResolutionError("sibling bundle manifest missing bundle_version")

    relative_path, sha256, opset = _find_model_entry(manifest, sibling_config)
    if relative_path is None:
        raise SiblingResolutionError(
            f"sibling bundle manifest does not reference {MODEL_ARTIFACT_BASENAME}"
        )

    onnx_path = (sibling_root / relative_path).resolve()
    if not onnx_path.exists():
        raise SiblingResolutionError(f"sibling manifest declares {onnx_path} but the file does not exist")

    if sha256 != EXPECTED_ONNX_SHA256:
        raise SiblingResolutionError(
            f"sibling manifest SHA-256 {sha256} does not match the pinned contract "
            f"{EXPECTED_ONNX_SHA256} — refusing to resolve an unverified artifact"
        )
    if opset is not None and opset != EXPECTED_ONNX_OPSET:
        raise SiblingResolutionError(
            f"sibling manifest opset {opset} does not match the pinned contract {EXPECTED_ONNX_OPSET}"
        )

    return ResolvedModelArtifact(
        onnx_path=onnx_path,
        sha256=sha256,
        opset=opset,
        sibling_bundle_name=bundle_name,
        sibling_bundle_version=bundle_version,
        sibling_root=sibling_root,
    )


def _find_model_entry(manifest: dict, sibling_config: dict) -> tuple[str | None, str | None, int | None]:
    # Preferred / actual sibling schema: a top-level "model" block with
    # relative_path/sha256/opset (confirmed by reading the real sibling's
    # bundle_manifest.json directly).
    model = manifest.get("model")
    if isinstance(model, dict) and "relative_path" in model and "sha256" in model:
        return model["relative_path"], model["sha256"], model.get("opset")

    # Back-compat: an explicit "model_artifact" block using "path" instead
    # of "relative_path" (an earlier draft's assumed shape).
    explicit = manifest.get("model_artifact")
    if isinstance(explicit, dict) and "path" in explicit and "sha256" in explicit:
        return explicit["path"], explicit["sha256"], explicit.get("opset")

    # Fallback: scan a generic files[] list for the known artifact basename.
    hint = sibling_config.get("veinnet_onnx_relative_path")
    for file_entry in manifest.get("files", []):
        path = file_entry.get("path", "")
        if path == hint or Path(path).name == MODEL_ARTIFACT_BASENAME:
            return path, file_entry.get("sha256"), file_entry.get("opset")

    return None, None, None
