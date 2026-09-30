from __future__ import annotations

from fastapi import APIRouter, Query

from app.impact.schemas import (
    ActivityCreate,
    ComputeRequest,
    ErrataCreate,
    FormulaCreate,
    FormulaVersionCreate,
    ObservationEventCreate,
    ParticipationBatchCreate,
    ReportAction,
    SurveySummaryCreate,
)
from app.impact.service import ImpactService

router = APIRouter(prefix="/api/impact", tags=["活动影响指标"])


def service() -> ImpactService:
    return ImpactService()


# ---------- 活动 ----------

@router.post("/activities", status_code=201)
def create_activity(payload: ActivityCreate):
    return service().create_activity(payload.model_dump())


@router.get("/activities")
def list_activities(category: str | None = None):
    return {"items": service().list_activities(category)}


@router.get("/activities/{activity_code}")
def get_activity(activity_code: str):
    return service().get_activity(activity_code)


@router.get("/activities/{activity_code}/compare")
def compare_activity(activity_code: str):
    return service().compare_activity(activity_code)


# ---------- 数据接收（同一编码重复上传幂等） ----------

@router.post("/participation-batches", status_code=201)
def ingest_batch(payload: ParticipationBatchCreate):
    record, deduplicated = service().ingest_batch(payload.model_dump())
    return {**record, "deduplicated": deduplicated}


@router.get("/participation-batches")
def list_batches(activity_code: str | None = None):
    return {"items": service().list_records("batches", activity_code)}


@router.post("/observation-events", status_code=201)
def ingest_event(payload: ObservationEventCreate):
    record, deduplicated = service().ingest_event(payload.model_dump())
    return {**record, "deduplicated": deduplicated}


@router.get("/observation-events")
def list_events(activity_code: str | None = None):
    return {"items": service().list_records("events", activity_code)}


@router.post("/survey-summaries", status_code=201)
def ingest_survey(payload: SurveySummaryCreate):
    record, deduplicated = service().ingest_survey(payload.model_dump())
    return {**record, "deduplicated": deduplicated}


@router.get("/survey-summaries")
def list_surveys(activity_code: str | None = None):
    return {"items": service().list_records("surveys", activity_code)}


# ---------- 指标口径版本 ----------

@router.post("/formulas", status_code=201)
def create_formula(payload: FormulaCreate):
    return service().create_formula(payload.model_dump())


@router.post("/formulas/{code}/versions", status_code=201)
def create_formula_version(code: str, payload: FormulaVersionCreate):
    return service().create_formula_version(code, payload.model_dump())


@router.get("/formulas")
def list_formulas(code: str | None = None):
    return {"items": service().list_formulas(code)}


# ---------- 指标计算与报告 ----------

@router.post("/reports/compute", status_code=201)
def compute_report(payload: ComputeRequest):
    report, reused = service().compute_report(payload.model_dump())
    return {**report, "reused": reused}


@router.get("/reports")
def list_reports(
    activity_code: str | None = None,
    formula_code: str | None = None,
    formula_version: int | None = Query(default=None, ge=1),
    status: str | None = Query(default=None, pattern="^(computed|published|locked)$"),
):
    return {"items": service().list_reports(activity_code=activity_code, formula_code=formula_code, formula_version=formula_version, status=status)}


@router.get("/reports/{report_id}")
def get_report(report_id: int):
    return service().get_report(report_id)


@router.post("/reports/{report_id}/publish")
def publish_report(report_id: int, payload: ReportAction):
    return service().publish_report(report_id, payload.actor)


@router.post("/reports/{report_id}/lock")
def lock_report(report_id: int, payload: ReportAction):
    return service().lock_report(report_id, payload.actor)


@router.post("/reports/{report_id}/errata", status_code=201)
def add_errata(report_id: int, payload: ErrataCreate):
    return service().add_errata(report_id, payload.model_dump())
