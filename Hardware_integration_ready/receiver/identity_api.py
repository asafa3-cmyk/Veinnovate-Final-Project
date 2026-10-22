"""identity_api.py — backend API surface for the identity/assignment POC
(AGENTS.md item 6: "Add backend APIs and a minimal Dashboard pairing
flow"). A separate `APIRouter` rather than new routes bolted directly onto
`http_service.py`, so the frozen dashboard-result HTTP/WebSocket boundary
in that module stays untouched except for one optional mount point (see
`build_identity_router()` usage in `http_service.create_app()`).

Every route returns adapter-inventory/assignment identifiers only — never
clinical data (vitals/BP stay on the separate result schema and WebSocket).

Identity contract 1.1.0 (2026-09-29, owner-authorized POC scope change —
see AGENTS.md "Pairing-time patient identity and demographics"):
`/pairing/start` additionally requires the patient's name, national ID,
age and sex (optional department/bed). These fields appear ONLY in this
router's requests/responses and in the patient-assignment store; they are
never added to jetson-result.schema.json or the `/ws/v1` stream, and this
module never logs a request body. Pre-1.1.0 the API accepted `patient_id`
only and returned no personal identifiers.
"""

from __future__ import annotations
import math
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from assignment_gate import AssignmentRejected
from identity_service import IdentityService
from patient_profile import ProfileInvalid, validate_profile

IDENTITY_CONTRACT_VERSION = "2.0.0"


class StartPairingRequest(BaseModel):
    """Values are typed loosely on purpose: validation happens in
    patient_profile.validate_profile() so every failure maps to a named
    reason code + Hebrew message, not a generic 422."""

    model_config = ConfigDict(extra="allow")
    first_name: str | None = None
    last_name: str | None = None
    national_id: str | None = None
    age_years: float | None = None
    sex: str | None = None
    department: str | None = None
    bed: str | None = None


class AssignRequest(BaseModel):
    window_id: str
    short_code: str

class UiObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    short_code: str
    event_detected_at_utc: float
    ui_rendered_at_utc: float


# Maps assignment_gate.RejectReason values to plain Hebrew operator
# messages (AGENTS.md: "plain Hebrew labels... no raw exception text... as
# the primary instruction"). Kept here (API layer) rather than in
# assignment_gate.py so that module stays UI/locale-free.
HEBREW_REJECTION_MESSAGES = {
    "CLIENT_PATIENT_ID_FORBIDDEN": "מזהה המטופל הפנימי מנוהל אוטומטית במערכת. אין להזין אותו בטופס.",
    "INTAKE_FIELD_UNSUPPORTED": "הטופס כולל שדה שאינו נתמך. פתחו שוב את היישום ונסו פעם אחת.",
    "PATIENT_RECORD_CONFLICT": "נמצאו רשומות מטופל לא תואמות. השיוך נחסם; פנו לצוות הטכני.",
    "PATIENT_RECORD_UNAVAILABLE": "רשומת המטופל אינה זמינה. השיוך נחסם; פנו לצוות הטכני.",
    "CODE_NOT_FOUND": "הקוד שהוזן אינו רשום במלאי המתאמים.",
    "ADAPTER_CONSUMED": "המתאם הזה כבר נוצל ואינו זמין לשימוש חוזר.",
    "ADAPTER_PENDING_PROVISIONING": "הכנת המתאם טרם הושלמה ואומתה. השיוך נחסם.",
    "ADAPTER_RETIRED": "המתאם הזה הוצא משימוש ואינו מורשה לשיוך.",
    "ADAPTER_ALREADY_ASSIGNED": "המתאם הזה כבר משויך למטופל/ת אחר/ת פעיל/ה.",
    "MISSING_LIVE_CANDIDATE": "לא זוהה מתאם פעיל עם קוד זה. ודאו שהמתאם דלוק ובטווח.",
    "STALE_LIVE_CANDIDATE": "האיתות מהמתאם אינו עדכני. קרבו את המתאם ונסו שוב.",
    "DUPLICATE_LIVE_IDENTITY": "זוהו שני מכשירים המשדרים את אותו קוד בו-זמנית. אין להמשיך — פנו לצוות טכני.",
    "UID_CODE_MISMATCH": "זהות המתאם שהתחבר אינה תואמת לקוד שנבחר. השיוך נחסם.",
    "IDENTITY_VERIFICATION_UNAVAILABLE": "לא ניתן לאמת את זהות המתאם המחובר. השיוך נחסם.",
    "IDENTITY_VERIFICATION_FAILED": "אימות זהות המתאם נכשל. השיוך נחסם.",
    "CONCURRENT_ASSIGNMENT_CONFLICT": "מתאם זה שויך הרגע במקביל על ידי פעולה אחרת. נסו שוב.",
    "ASSIGNMENT_STORAGE_UNAVAILABLE": "מסד נתוני השיוכים אינו זמין. השיוך נחסם.",
    "PAIRING_WINDOW_NOT_OPEN": "חלון השיוך אינו פעיל. התחילו שוב באמצעות הוספת מתאם.",
    "SESSION_ALREADY_ACTIVE": "כבר קיימת מדידה פעילה למתאם זה. השיוך נחסם.",
    "CALIBRATION_INVALIDATION_UNAVAILABLE": "לא ניתן לסיים בבטחה: ביטול הכיול עדיין אינו מחובר למערכת.",
    "CALIBRATION_INVALIDATION_FAILED": "ביטול הכיול נכשל. המדידה נשארה פעילה ופנו לצוות טכני.",
    "END_SESSION_STATE_MISMATCH": "רשומות המתאם והמדידה אינן תואמות. הסיום נחסם ופנו לצוות טכני.",
    "END_SESSION_STORAGE_FAILED": "סיום המדידה לא הושלם. המתאם חסום לשימוש חוזר ופנו לצוות טכני.",
    # Identity contract 1.1.0
    "PROFILE_FIELD_MISSING": "חסר שדה חובה בפרטי המטופל/ת (שם פרטי, שם משפחה, תעודת זהות, גיל או מין).",
    "PROFILE_FIELD_INVALID": "אחד מפרטי המטופל/ת אינו תקין. בדקו את הערכים ונסו שוב.",
    "NATIONAL_ID_INVALID": "מספר תעודת הזהות אינו תקין (ספרת ביקורת שגויה).",
    "AGE_OUT_OF_RANGE": "הגיל שהוזן מחוץ לטווח המותר (0–130).",
    "SEX_INVALID": "יש לבחור מין: נקבה או זכר.",
    "PAIRING_PROFILE_UNAVAILABLE": "לא נמצאו פרטי מטופל/ת מהשיוך עבור מדידה זו. הכיול נחסם.",
    "DEMOGRAPHICS_MISMATCH": "הגיל או המין שנשלחו בכיול אינם תואמים לפרטי השיוך. הכיול נחסם.",
}


def rejection_to_http(exc: AssignmentRejected) -> HTTPException:
    message = HEBREW_REJECTION_MESSAGES.get(exc.reason_code, "השיוך נחסם מסיבות בטיחות.")
    return HTTPException(
        status_code=409,
        detail={"reason_code": exc.reason_code, "message_he": message, "message": exc.message},
    )


def build_identity_router(identity_service: IdentityService, *, dependencies: list | None = None) -> APIRouter:
    router = APIRouter(prefix="/v1/identity", tags=["identity"], dependencies=dependencies or [])

    @router.get("/inventory")
    async def list_inventory():
        return {"adapters": identity_service.list_inventory()}

    @router.get("/live")
    async def list_live():
        return {"live": identity_service.list_live_waiting()}

    @router.get("/diagnostics")
    async def diagnostics():
        return {"contract_version": "1.0.0", "assignment": identity_service.assignment_diagnostic,
                "ui_delivery": identity_service.ui_delivery}

    @router.post("/ui-observation")
    async def ui_observation(req: UiObservation):
        record = identity_service.inventory.get_by_code(req.short_code)
        detected = identity_service.discovery_detected_at.get(record.hardware_uid) if record else None
        delta = req.ui_rendered_at_utc - req.event_detected_at_utc
        if (detected != req.event_detected_at_utc or not math.isfinite(delta) or not 0 <= delta <= 60
                or req.ui_rendered_at_utc > time.time() + 1):
            raise HTTPException(409, detail={"reason_code":"UI_TIMING_EVENT_UNAVAILABLE"})
        identity_service.ui_delivery = {**req.model_dump(), "detection_to_ui_ms": round(delta*1000,2),
                                        "method":"JETSON_CALLBACK_TO_TWO_ANIMATION_FRAMES"}
        return {"recorded": True}

    @router.post("/pairing/start")
    async def start_pairing(req: StartPairingRequest):
        if "patient_id" in (req.model_extra or {}):
            raise rejection_to_http(AssignmentRejected("CLIENT_PATIENT_ID_FORBIDDEN", "patient_id is backend-managed"))
        if req.model_extra:
            raise rejection_to_http(AssignmentRejected("INTAKE_FIELD_UNSUPPORTED", "unsupported intake field"))
        try:
            profile = validate_profile(
                first_name=req.first_name,
                last_name=req.last_name,
                national_id=req.national_id,
                age_years=req.age_years,
                sex=req.sex,
                department=req.department,
                bed=req.bed,
            )
        except ProfileInvalid as exc:
            raise rejection_to_http(AssignmentRejected(exc.reason_code, f"invalid pairing profile field {exc.field}"))
        try:
            window = identity_service.start_pairing_for_profile(profile=profile)
        except AssignmentRejected as exc:
            raise rejection_to_http(exc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "contract_version": IDENTITY_CONTRACT_VERSION,
            "window_id": window.window_id,
            "patient_id": window.patient_id,
            "opened_at_utc": window.opened_at_utc,
            "expires_at_utc": window.expires_at_utc,
            "state": window.state,
        }

    @router.get("/pairing/{window_id}")
    async def pairing_status(window_id: str):
        try:
            return identity_service.pairing_status(window_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/pairing/{window_id}/cancel")
    async def cancel_pairing(window_id: str):
        try:
            window = identity_service.cancel_pairing(window_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"window_id": window.window_id, "state": window.state}

    @router.post("/assign")
    async def assign(req: AssignRequest):
        try:
            return await identity_service.assign_v2(window_id=req.window_id, short_code=req.short_code)
        except AssignmentRejected as exc:
            identity_service.assignment_diagnostic.update(reason_code=exc.reason_code)
            raise rejection_to_http(exc)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="pairing window not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions/active")
    async def list_active_sessions():
        return {"contract_version": IDENTITY_CONTRACT_VERSION, "sessions": identity_service.list_active_sessions()}

    @router.post("/sessions/{session_id}/end")
    async def end_session(session_id: str):
        try:
            return await identity_service.end_session_v2(session_id)
        except AssignmentRejected as exc:
            raise rejection_to_http(exc)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
