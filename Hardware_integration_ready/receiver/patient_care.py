"""Versioned bedside care: protected append-only events, never vitals/logs.

CUFF-PPG-HOST-ASSOCIATION-v1 permits a cuff timestamp within 120 seconds
of a verified baseline's host receipt interval. This engineering association
rule is not physiological validation. Untimed/old/different bindings fail closed.
"""
from __future__ import annotations

import json
import math
import time
import uuid

from fastapi import APIRouter, HTTPException

import prototype_continuous as _proto
from pydantic import BaseModel, ConfigDict

CONTRACT_VERSION = "1.0.0"
ASSOCIATION_RULE = "CUFF-PPG-HOST-ASSOCIATION-v1"
ASSOCIATION_MARGIN_S = 120


def _association_margin_s() -> float:
    return _proto.CONFIG["association_margin_s"] if _proto.enabled() else ASSOCIATION_MARGIN_S


class CareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Staff(CareInput):
    operator_name: str
    shift: str
    nurse_in_charge: str

    def declaration(self):
        if self.shift not in ("morning", "afternoon", "night") or any(
            not v.strip() or len(v) > 120 for v in (self.operator_name, self.nurse_in_charge)
        ):
            raise HTTPException(422, detail={"reason_code": "STAFF_DECLARATION_REQUIRED"})
        return {**self.model_dump(), "assurance": "OPERATOR_DECLARED"}


class Cuff(CareInput):
    sbp: float
    dbp: float
    measured_at_utc: float
    # Prototype-continuous mode: optional second cuff reading (averaged) and an
    # optional reference oximeter value that anchors the session SpO2.
    repeat_sbp: float | None = None
    repeat_dbp: float | None = None
    spo2_reference_pct: float | None = None
    baseline_reading_id: str | None = None
    staff: Staff


class Location(CareInput):
    department: str | None = None
    bed: str | None = None
    staff: Staff


class PatientCare:
    def __init__(self, identity, runtime):
        self.identity = identity
        self.runtime = runtime
        self.store = identity.assignment_store
        with self.store._lock:
            self.store._conn.execute("CREATE TABLE IF NOT EXISTS care_events (record_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, payload TEXT NOT NULL)")
            self.store._conn.commit()
        self.baselines = {}
        runtime.baseline_listeners.append(self.on_baseline)

    def binding(self, session_id, *, active=True):
        session = self.identity.session_store.get(session_id)
        assignment = self.store.get(session.assignment_id) if session else None
        if (not session or not assignment or session.patient_id != assignment.patient_id
            or session.hardware_uid != assignment.hardware_uid
            or (active and (not session.active or not assignment.active))):
            raise HTTPException(404, detail={"reason_code": "PATIENT_SESSION_UNAVAILABLE"})
        return session, assignment

    def history(self, session_id):
        self.binding(session_id, active=False)
        with self.store._lock:
            return [json.loads(row[0]) for row in self.store._conn.execute(
                "SELECT payload FROM care_events WHERE session_id=? ORDER BY rowid", (session_id,))]

    def append(self, session_id, event):
        event = {"record_id": str(uuid.uuid4()), "recorded_at_utc": time.time(), **event}
        with self.store._lock:
            self.store._conn.execute("INSERT INTO care_events VALUES(?,?,?)", (event["record_id"], session_id, json.dumps(event)))
            self.store._conn.commit()
        return event

    def selected(self, session_id):
        self.binding(session_id)
        selected = self.runtime.for_session(session_id)
        if selected is None:
            raise HTTPException(409, detail={"reason_code": "RUNTIME_UNAVAILABLE"})
        return selected

    def resolve(self, session_id, cuff):
        o = self.selected(session_id)
        baseline = self.baselines.get(session_id)
        if baseline is None:
            if cuff.get("baseline_reading_id"):
                return "REJECTED", "BASELINE_NOT_AVAILABLE", None
            return "PENDING", "BASELINE_REQUIRED", None
        if cuff.get("baseline_reading_id") and cuff["baseline_reading_id"] != baseline["reading_id"]:
            return "REJECTED", "BASELINE_NOT_AVAILABLE", None
        window = baseline["window"]
        if (baseline["patient_id"] != o.patient_id or baseline["device_id"] != o.calibration_device_key()
            or window is not o.last_window or window.sensor_session_id != o._current_session_id
            or not window.integrity.is_complete or o.connection_status != "CONNECTED"):
            return "REJECTED", "BASELINE_BINDING_CHANGED", None
        status = o.last_status
        if (status is None or getattr(status,"sensor_session_id",window.sensor_session_id) != window.sensor_session_id
            or (status.sensor_config_id != window.sensor_config_id and not (window is o.final_verified_window and status.sensor_config_id == 0))):
            return "REJECTED", "BASELINE_CONFIGURATION_CHANGED", None
        start, end = baseline["start"], baseline["end"]
        if start is None or end is None or not start - _association_margin_s() <= cuff["measured_at_utc"] <= end + _association_margin_s():
            return "REJECTED", "CUFF_BASELINE_TIME_MISMATCH", None
        if o.model_adapter is None:
            return "PENDING", "BASELINE_PROCESSING_UNAVAILABLE", None
        try:
            if _proto.enabled():
                try:
                    processed = o.model_adapter.preprocess_raw_window(window, role="baseline")
                except TypeError:  # builders without the prototype role parameter (e.g. test mocks)
                    processed = o.model_adapter.preprocess_raw_window(window)
            else:
                processed = o.model_adapter.preprocess_raw_window(window)
        except Exception:
            return "REJECTED", "BASELINE_PROCESSING_FAILED", None
        if not processed.accepted:
            return "REJECTED", "BASELINE_QUALITY_REJECTED", None
        profile = self.identity.profile_for_session(session_id)
        if profile is None:
            return "REJECTED", "PAIRING_PROFILE_UNAVAILABLE", None
        try:
            o.model_adapter.demographics(profile.age_years, profile.sex)
            record = o.calibration_store.capture(
                patient_id=o.patient_id, device_id=o.calibration_device_key(),
                sensor_session_id=window.sensor_session_id, sensor_config_id=window.sensor_config_id,
                baseline_cuff_sbp=cuff["sbp"], baseline_cuff_dbp=cuff["dbp"],
                baseline_ppg_red=window.red, baseline_ppg_ir=window.ir,
                baseline_ppg_processed=processed.ppg_flat, baseline_morphology=processed.morphology,
                age_years=profile.age_years, sex=profile.sex,
                firmware_version="{}.{}.{}".format(*status.firmware_version),
                preprocessing_version=o.model_adapter.preprocessing_version, now=time.time(),
            )
        except Exception:
            return "REJECTED", "CALIBRATION_STORAGE_FAILED", None
        if _proto.enabled() and cuff.get("spo2_reference_pct") is not None:
            r0 = _proto.ratio_of_ratios(window.red, window.ir)
            if r0 is not None:
                o.calibration_store.set_spo2_reference(o.patient_id, o.calibration_device_key(),
                                                       float(cuff["spo2_reference_pct"]), r0)
        return "ACTIVE", None, record.calibration_id

    def cuff(self, session_id, req):
        self.selected(session_id)
        now = time.time()
        staff = req.staff.declaration()
        if (not all(math.isfinite(v) for v in (req.sbp, req.dbp, req.measured_at_utc))
            or not 0 < req.dbp < req.sbp or req.measured_at_utc <= 0 or req.measured_at_utc > now):
            raise HTTPException(422, detail={"reason_code": "CUFF_VALUES_OR_TIME_INVALID"})
        # Existing baseline selection must be explicit. An early cuff is only
        # considered for the next baseline in this exact logical session.
        if session_id in self.baselines and not req.baseline_reading_id:
            raise HTTPException(409, detail={"reason_code": "BASELINE_SELECTION_REQUIRED"})
        data = {"sbp": req.sbp, "dbp": req.dbp, "measured_at_utc": req.measured_at_utc,
                "entered_at_utc": now, "baseline_reading_id": req.baseline_reading_id, "staff": staff}
        if _proto.enabled():
            if req.repeat_sbp is not None and req.repeat_dbp is not None:
                if not all(math.isfinite(v) for v in (req.repeat_sbp, req.repeat_dbp)) or not 0 < req.repeat_dbp < req.repeat_sbp:
                    raise HTTPException(422, detail={"reason_code": "CUFF_VALUES_OR_TIME_INVALID"})
                sbp, dbp, flag = _proto.average_cuff(req.sbp, req.dbp, req.repeat_sbp, req.repeat_dbp)
                data.update({"sbp": sbp, "dbp": dbp, "first_reading": [req.sbp, req.dbp],
                             "repeat_reading": [req.repeat_sbp, req.repeat_dbp], "repeat_flag": flag})
            if req.spo2_reference_pct is not None:
                if not (math.isfinite(req.spo2_reference_pct) and 50 <= req.spo2_reference_pct <= 100):
                    raise HTTPException(422, detail={"reason_code": "SPO2_REFERENCE_INVALID"})
                data["spo2_reference_pct"] = req.spo2_reference_pct
        elif req.repeat_sbp is not None or req.repeat_dbp is not None or req.spo2_reference_pct is not None:
            raise HTTPException(422, detail={"reason_code": "PROTOTYPE_FIELDS_REQUIRE_PROTOTYPE_MODE"})
        # Persist the reference first. Activation/outcomes are separate events;
        # no correction overwrites a prior cuff or calibration record.
        event = self.append(session_id, {"type": "CUFF_ENTERED", **data,
            "outcome": "PENDING", "reason_code": "BASELINE_REQUIRED", "calibration_id": None,
            "association_rule": ASSOCIATION_RULE})
        outcome, reason, calibration_id = self.resolve(session_id, data)
        if outcome != "PENDING" or reason != "BASELINE_REQUIRED":
            self.record_outcome(session_id, event, outcome, reason, calibration_id)
        return {"contract_version": CONTRACT_VERSION, **data, "record_id": event["record_id"],
                "outcome": outcome, "reason_code": reason, "calibration_id": calibration_id}

    def record_outcome(self, session_id, cuff, outcome, reason, calibration_id):
        try:
            self.append(session_id, {"type": "CALIBRATION_OUTCOME", "cuff_record_id": cuff["record_id"],
                "baseline_reading_id": self.baselines.get(session_id, {}).get("reading_id"),
                "outcome": outcome, "reason_code": reason, "calibration_id": calibration_id, "staff": cuff["staff"]})
        except Exception:
            if calibration_id:
                o = self.selected(session_id)
                o.calibration_store.invalidate(o.patient_id, o.calibration_device_key(), "CARE_HISTORY_STORAGE_FAILED")
            raise HTTPException(503, detail={"reason_code": "CARE_HISTORY_STORAGE_FAILED"})

    async def on_baseline(self, o, reading_id, window, start, end):
        session_id = o.logical_session_id
        self.baselines[session_id] = {"reading_id": reading_id, "window": window, "start": start, "end": end,
                                      "patient_id": o.patient_id, "device_id": o.calibration_device_key()}
        history = self.history(session_id)
        completed = {x.get("cuff_record_id") for x in history if x["type"] == "CALIBRATION_OUTCOME" and x["outcome"] != "PENDING"}
        cuffs = [x for x in history if x["type"] == "CUFF_ENTERED" and x["record_id"] not in completed]
        # A correction supersedes pending references but keeps their history.
        for superseded in cuffs[:-1]:
            self.record_outcome(session_id, superseded, "REJECTED", "SUPERSEDED_CUFF_REFERENCE", None)
        if cuffs:
            cuff = cuffs[-1]
            outcome, reason, cal_id = self.resolve(session_id, cuff)
            self.record_outcome(session_id, cuff, outcome, reason, cal_id)

    def location(self, session_id, req):
        _, assignment = self.binding(session_id)
        staff = req.staff.declaration()
        if any(v is not None and (not v.strip() or len(v) > 120) for v in (req.department, req.bed)):
            raise HTTPException(422, detail={"reason_code": "LOCATION_INVALID"})
        profile = assignment.profile
        if profile is None:
            raise HTTPException(409, detail={"reason_code": "PAIRING_PROFILE_UNAVAILABLE"})
        event = {"record_id": str(uuid.uuid4()), "type": "LOCATION_UPDATED", "recorded_at_utc": time.time(), "staff": staff,
                 "old": {"department": profile.department, "bed": profile.bed},
                 "new": {"department": req.department, "bed": req.bed}}
        with self.store._lock:
            try:
                self.store._conn.execute("BEGIN IMMEDIATE")
                current = self.store._conn.execute("SELECT active,department,bed FROM assignments WHERE assignment_id=?", (assignment.assignment_id,)).fetchone()
                if current is None or not current[0]:
                    raise HTTPException(409, detail={"reason_code": "PATIENT_SESSION_UNAVAILABLE"})
                event["old"] = {"department": current[1], "bed": current[2]}
                self.store._conn.execute("UPDATE assignments SET department=?,bed=? WHERE assignment_id=?", (req.department, req.bed, assignment.assignment_id))
                self.store._conn.execute("INSERT INTO care_events VALUES(?,?,?)", (event["record_id"], session_id, json.dumps(event)))
                self.store._conn.commit()
            except Exception:
                self.store._conn.rollback()
                raise
        return {"contract_version": CONTRACT_VERSION, "record_id": event["record_id"],
                "entered_at_utc": event["recorded_at_utc"], **event["new"]}

    def status(self, session_id):
        o = self.selected(session_id)
        snapshot = next(x for x in self.runtime.status_snapshot()["sessions"] if x["session_id"] == session_id)
        cal = o.calibration_store.status(o.patient_id, o.calibration_device_key(),
            current_sensor_session_id=getattr(o.last_status,"sensor_session_id",o._current_session_id),
            current_sensor_config_id=(o.last_window.sensor_config_id if o.last_window is o.final_verified_window and o.last_window is not None and getattr(o.last_status,"sensor_config_id",None) == 0
                else getattr(o.last_status,"sensor_config_id",None)))
        record = o.calibration_store.get(o.patient_id, o.calibration_device_key())
        events = self.history(session_id)
        outcomes = [x for x in events if x["type"] in ("CUFF_ENTERED", "CALIBRATION_OUTCOME")]
        return {"contract_version": CONTRACT_VERSION, "session_id": session_id,
                "calibration_status": cal.value, "calibrated_at_utc": record.captured_at_utc if record else None, "age_seconds": max(0,time.time()-record.captured_at_utc) if record else None, "calibration_outcome": outcomes[-1]["outcome"] if outcomes else "NONE", "calibration_reason_code": outcomes[-1].get("reason_code") if outcomes else None,
                "baseline_reading_id": self.baselines.get(session_id, {}).get("reading_id") if o.last_window is not None and self.baselines.get(session_id, {}).get("window") is o.last_window else None,
                **{k: snapshot.get(k) for k in ("last_communication_at_utc", "next_measurement_at_utc", "scheduled_interval_s", "schedule_enabled", "connection_status", "acquisition_state")},
                "capture_available": o.connection_status == "CONNECTED" and snapshot["acquisition_state"] in ("IDLE", "FAILURE", "UNAVAILABLE"),
                "warning_after_s": o.calibration_store.warning_after_s,
                "hard_expiry_s": o.calibration_store.hard_expiry_s}


def care_router(care, dependencies):
    router = APIRouter(prefix="/v1/identity/care", dependencies=dependencies)
    @router.get("/sessions/{session_id}/status")
    async def status(session_id: str):
        return care.status(session_id)
    @router.get("/sessions/{session_id}/history")
    async def history(session_id: str):
        return {"contract_version": CONTRACT_VERSION, "records": care.history(session_id)}
    @router.post("/sessions/{session_id}/cuff")
    async def cuff(session_id: str, req: Cuff):
        return care.cuff(session_id, req)
    @router.patch("/sessions/{session_id}/location")
    async def location(session_id: str, req: Location):
        return care.location(session_id, req)
    return router
