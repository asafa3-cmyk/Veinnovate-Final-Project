"""http_service.py — the real Jetson network boundary: versioned HTTP +
WebSocket endpoints exposed to VeinoGate_Dashboard_integration_ready.

Endpoint paths match the dashboard's real consumer code exactly (read
directly from VeinoGate_Dashboard_integration_ready/src/api/jetsonClient.ts,
src/api/jetsonSocket.ts, and tests/mockServer/server.mjs during the E2E
hardening pass — not guessed):
    GET  {base}/sessions/{session_id}/snapshot   (jetsonClient.ts)
    WS   {ws_base}                                (jetsonSocket.ts; server-side path "/ws/v1")
The dashboard's runtime-config.example.json bakes the API version into the
base URL itself (".../api/v1"), so this module serves the snapshot route
under /api/v1 and the socket under /ws/v1 to match that convention exactly.

Ops-only endpoints (/v1/health, /v1/config, /v1/calibration) are NOT part
of the dashboard's consumer contract — the dashboard never calls them — but
remain for operational use (health checks, manual calibration capture).

Production defaults:
  - Binds to 127.0.0.1 unless --http-host explicitly overrides it (main()
    in veinnet_edge_service.py wires this through; never bind 0.0.0.0 by
    default).
  - No clinical payload logging: uvicorn's access log is disabled
    (calibration requests carry cuff BP; results carry BP) and this module
    never itself logs a request/response body.
  - No hardcoded tokens/secrets: an optional bearer token is read from the
    VEINNET_API_TOKEN environment variable at request time. If unset, the
    service runs without auth — a deployment decision to make explicitly
    for clinical use, documented in README, not a baked-in default
    credential.
  - Fails visibly (409) on a client-declared incompatible schema version
    via the optional X-Client-Schema-Version request header, rather than
    silently proceeding.
  - No mock/demo fallback: the snapshot endpoint is 404 until a real result
    exists; there is no synthetic "example" ever served.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, field_validator

from calibration_store import CalibrationCaptureError
from dashboard_api import RESULT_SCHEMA_VERSION
from battery_telemetry import battery_snapshot, load_validations

BUNDLE_ROOT = Path(__file__).resolve().parent.parent


def _check_schema_version(x_client_schema_version: str | None = Header(default=None)) -> None:
    if x_client_schema_version is not None and x_client_schema_version != RESULT_SCHEMA_VERSION:
        raise HTTPException(
            status_code=409,
            detail=f"unsupported schema_version {x_client_schema_version!r}; server supports {RESULT_SCHEMA_VERSION}",
        )


def _check_auth(authorization: str | None = Header(default=None)) -> None:
    token = os.environ.get("VEINNET_API_TOKEN")
    if token is None:
        return  # no auth configured for this run — a deployment decision, not a hardcoded default
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="unauthorized")


class CalibrationRequest(BaseModel):
    """Identity (patient_id/device_id) is deliberately NOT part of this
    request: it is bound at production startup / taken from the actively
    connected protocol device, never accepted from the caller — see
    endpoint below."""

    baseline_cuff_sbp: float
    baseline_cuff_dbp: float
    age_years: float | None = None
    sex: str | None = None  # exactly "F", "M", or null
    session_id: str | None = None

    @field_validator("sex")
    @classmethod
    def _validate_sex(cls, v: str | None) -> str | None:
        if v is not None and v not in ("F", "M"):
            raise ValueError('sex must be exactly "F", "M", or null')
        return v


def create_app(orchestrator, identity_service=None, multi_runtime=None) -> FastAPI:
    app = FastAPI(title="VeinNet Jetson Edge Service", version=RESULT_SCHEMA_VERSION)

    @app.middleware("http")
    async def no_clinical_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response
    start_time = time.time()
    battery_validations = load_validations()

    if identity_service is not None:
        # Software-only POC identity/assignment layer (AGENTS.md items 2-6).
        # Fully optional and additive: omitting identity_service leaves this
        # app byte-for-byte the same as before — see test_http_service.py,
        # which never passes it.
        from identity_api import build_identity_router

        app.include_router(build_identity_router(identity_service, dependencies=[Depends(_check_auth)]))
        if multi_runtime is not None and hasattr(multi_runtime, "baseline_listeners"):
            from patient_care import PatientCare, care_router
            from fastapi import Request
            async def care_access(request: Request):
                # Bearer authenticates API access, never the declared nurse.
                if os.environ.get("VEINNET_API_TOKEN") is None and (
                    request.client is None or request.client.host not in ("127.0.0.1", "::1", "localhost")
                ):
                    raise HTTPException(403, detail={"reason_code": "LOCAL_CARE_ACCESS_REQUIRED"})
            app.state.patient_care = PatientCare(identity_service, multi_runtime)
            app.include_router(care_router(app.state.patient_care, [Depends(_check_auth), Depends(care_access)]))

    @app.get("/v1/health", dependencies=[Depends(_check_auth)])
    async def health():
        battery = battery_snapshot(orchestrator, battery_validations, now=time.time())
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "uptime_s": time.time() - start_time,
            "patient_id": orchestrator.patient_id,
            "device_id": orchestrator._device_id,
            "connection_status": orchestrator.connection_status,
            "model_adapter_loaded": orchestrator.model_adapter is not None,
            "battery": battery,
        }

    @app.get("/v1/config", dependencies=[Depends(_check_auth)])
    async def config():
        import json

        hw_profiles_path = BUNDLE_ROOT / "config" / "hardware_profiles.json"
        cal_thresholds_path = BUNDLE_ROOT / "config" / "calibration_thresholds.json"
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "protocol_version": 3,
            "hardware_profiles": json.loads(hw_profiles_path.read_text()) if hw_profiles_path.exists() else None,
            "calibration_thresholds": json.loads(cal_thresholds_path.read_text()) if cal_thresholds_path.exists() else None,
        }

    @app.get(
        "/api/v1/sessions/{session_id}/snapshot",
        dependencies=[Depends(_check_auth), Depends(_check_schema_version)],
    )
    async def snapshot(session_id: str):
        selected = multi_runtime.for_session(session_id) if multi_runtime is not None else orchestrator
        if selected is None or selected.latest_result is None:
            raise HTTPException(status_code=404, detail="no result produced yet")
        if selected.latest_result.session_id != session_id:
            raise HTTPException(status_code=404, detail=f"no result for session_id {session_id!r}")
        return selected.latest_result.to_dict()

    @app.get("/v1/runtime/status", dependencies=[Depends(_check_auth)])
    async def runtime_status():
        if multi_runtime is None:
            raise HTTPException(status_code=409, detail="multi-session runtime unavailable")
        return multi_runtime.status_snapshot()

    @app.post(
        "/v1/sessions/{session_id}/capture",
        dependencies=[Depends(_check_auth), Depends(_check_schema_version)],
    )
    async def nurse_capture(session_id: str):
        from measurement_policy import PolicyError
        if multi_runtime is None:
            raise HTTPException(status_code=409, detail="scheduled measurement runtime unavailable")
        try:
            multi_runtime.request_capture(session_id)
        except PolicyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        policy_id = "FIXED-PROFILE-25-DIAGNOSTIC-v1" if getattr(multi_runtime, "fixed25_diagnostic", False) is True else "STAGE16-ALTERNATIVE2-v1"
        return {"schema_version": "1.0.0", "policy_id": policy_id, "status": "QUEUED"}

    @app.post(
        "/v1/calibration",
        dependencies=[Depends(_check_auth), Depends(_check_schema_version)],
    )
    async def calibration(req: CalibrationRequest):
        selected = orchestrator
        if multi_runtime is not None:
            if req.session_id is None:
                raise HTTPException(status_code=400, detail="session_id is required for multi-device calibration")
            selected = multi_runtime.for_session(req.session_id)
            if selected is None:
                raise HTTPException(status_code=404, detail="no active measurement session")
        age_years, sex = req.age_years, req.sex
        if multi_runtime is not None:
            # Identity contract 1.1.0: the pairing profile is the single source
            # of truth for age/sex. A caller value is only accepted if it
            # matches; otherwise (or with no profile at all) fail closed.
            from assignment_gate import AssignmentRejected, RejectReason
            from identity_api import rejection_to_http

            profile = identity_service.profile_for_session(req.session_id) if identity_service is not None else None
            if profile is None:
                raise rejection_to_http(AssignmentRejected(
                    RejectReason.PAIRING_PROFILE_UNAVAILABLE, "no pairing profile for this session"))
            if (age_years is not None and float(age_years) != profile.age_years) or (sex is not None and sex != profile.sex):
                raise rejection_to_http(AssignmentRejected(
                    RejectReason.DEMOGRAPHICS_MISMATCH, "calibration demographics differ from the pairing profile"))
            age_years, sex = profile.age_years, profile.sex
            raise HTTPException(409, detail={"reason_code": "USE_SESSION_CARE_CUFF_API"})
        if selected.model_adapter is None:
            raise HTTPException(status_code=409, detail="no production model adapter loaded; cannot calibrate")

        device_id = selected._device_id
        if device_id is None:
            raise HTTPException(status_code=409, detail="no connected device — cannot bind a calibration to an unknown device_id")

        window = selected.last_window
        if window is None:
            raise HTTPException(status_code=409, detail="no completed raw window available yet to bind a calibration baseline to")


        try:
            demographics_vec = selected.model_adapter.demographics(age_years, sex)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid demographics: {exc}") from exc
        del demographics_vec  # validation only — the raw (age_years, sex) is what's persisted; the
                               # transformed vector is recomputed fresh at every inference (build()).

        preprocessed = selected.model_adapter.preprocess_raw_window(window)
        if not preprocessed.accepted:
            raise HTTPException(
                status_code=422,
                detail=f"baseline window failed frozen preprocessing/SQI: {preprocessed.reason_code}",
            )

        # Bind the baseline to the live sensor session/config, and record the
        # firmware version the identity-checked device reported in STATUS.
        # Never fake continuity or invent a version: fail closed instead.
        if not window.integrity.is_complete:
            raise HTTPException(status_code=409, detail="baseline window lacks complete acquisition integrity (loss/uncertainty)")
        if selected._current_session_id is not None and window.sensor_session_id != selected._current_session_id:
            raise HTTPException(status_code=409, detail="baseline window belongs to a superseded sensor session")
        status = selected.last_status
        if status is None:
            raise HTTPException(status_code=409, detail="no device STATUS yet; firmware version and config are unverified")
        if status.sensor_config_id != window.sensor_config_id and window is not getattr(selected, "final_verified_window", None):
            raise HTTPException(status_code=409, detail="device config changed since the baseline window")
        firmware_version = "{}.{}.{}".format(*status.firmware_version)

        try:
            record = selected.calibration_store.capture(
                patient_id=selected.patient_id,
                device_id=selected.calibration_device_key(),
                sensor_session_id=window.sensor_session_id,
                sensor_config_id=window.sensor_config_id,
                baseline_cuff_sbp=req.baseline_cuff_sbp,
                baseline_cuff_dbp=req.baseline_cuff_dbp,
                baseline_ppg_red=window.red,
                baseline_ppg_ir=window.ir,
                baseline_ppg_processed=preprocessed.ppg_flat,
                baseline_morphology=preprocessed.morphology,
                age_years=age_years,
                sex=sex,
                firmware_version=firmware_version,
                preprocessing_version=selected.model_adapter.preprocessing_version,
            )
        except CalibrationCaptureError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Never log record contents (cuff BP, raw/processed PPG) — return only
        # non-sensitive identifiers/metadata.
        return {
            "calibration_id": record.calibration_id,
            "captured_at_utc": record.captured_at_utc,
            "sensor_session_id": record.sensor_session_id,
            "sensor_config_id": record.sensor_config_id,
        }

    @app.websocket("/ws/v1")
    async def ws_results(websocket: WebSocket):
        token = os.environ.get("VEINNET_API_TOKEN")
        if token is not None:
            auth = websocket.headers.get("authorization")
            if auth != f"Bearer {token}":
                await websocket.close(code=1008)
                return

        await websocket.accept()

        async def _listener(result) -> None:
            try:
                await websocket.send_text(result.to_json())
            except Exception:
                pass  # best-effort; disconnect is handled by the receive loop below

        listener_owner = multi_runtime if multi_runtime is not None else orchestrator
        listener_owner.result_listeners.append(_listener)
        try:
            while True:
                await websocket.receive_text()  # keep the connection alive; client sends no meaningful data
        except WebSocketDisconnect:
            pass
        finally:
            if _listener in listener_owner.result_listeners:
                listener_owner.result_listeners.remove(_listener)

    return app


async def run_http_service(
    orchestrator, host: str = "127.0.0.1", port: int = 8080, identity_service=None, multi_runtime=None
) -> None:
    import uvicorn

    app = create_app(orchestrator, identity_service=identity_service, multi_runtime=multi_runtime)
    config = uvicorn.Config(app, host=host, port=port, access_log=False, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
