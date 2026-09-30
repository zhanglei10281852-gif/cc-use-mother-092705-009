from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.errors import ValidationError
from app.impact.schemas import (
    ActivityCreate,
    DefinitionCreate,
    ErratumCreate,
    ObservationRecordUpload,
    ParticipationBatchUpload,
    ReportCompute,
    ReportLock,
    ReportPublish,
    SurveySummaryUpload,
    dump,
)
from app.impact.service import ImpactService

router = APIRouter(prefix="/api/impact", tags=["活动影响指标"])


def service() -> ImpactService:
    return ImpactService()


@router.post("/activities", status_code=201)
def create_activity(payload: ActivityCreate):
    return service().create_activity(dump(payload), actor=payload.actor)


@router.get("/activities")
def list_activities():
    return {"items": service().list_activities()}


@router.post("/participation-batches", status_code=201)
def upload_batch(payload: ParticipationBatchUpload):
    return service().upload_batch(dump(payload), actor=payload.actor)


@router.get("/participation-batches")
def list_batches(activity_code: str | None = None):
    return {"items": service().list_batches(activity_code)}


@router.post("/observation-records", status_code=201)
def upload_observation(payload: ObservationRecordUpload):
    return service().upload_observation(dump(payload), actor=payload.actor)


@router.get("/observation-records")
def list_observations(batch_key: str | None = None):
    return {"items": service().list_observations(batch_key)}


@router.post("/survey-summaries", status_code=201)
def upload_survey(payload: SurveySummaryUpload):
    return service().upload_survey(dump(payload), actor=payload.actor)


@router.get("/survey-summaries")
def list_surveys(batch_key: str | None = None):
    return {"items": service().list_surveys(batch_key)}


@router.post("/definitions", status_code=201)
def create_definition(payload: DefinitionCreate):
    return service().create_definition(dump(payload), actor=payload.actor)


@router.get("/definitions")
def list_definitions(code: str | None = None):
    return {"items": service().list_definitions(code)}


@router.post("/reports/compute", status_code=201)
def compute_report(payload: ReportCompute):
    return service().compute_report(dump(payload), actor=payload.actor)


@router.get("/reports")
def list_reports(
    scope_type: str | None = None,
    scope_key: str | None = None,
    definition_code: str | None = None,
    status: str | None = None,
):
    return {"items": service().list_reports(scope_type=scope_type, scope_key=scope_key, definition_code=definition_code, status=status)}


@router.get("/reports/compare")
def compare_reports(report_ids: str = Query(..., min_length=1)):
    try:
        ids = [int(part) for part in report_ids.split(",") if part.strip()]
    except ValueError as exc:
        raise ValidationError("report_ids 必须是逗号分隔的整数列表") from exc
    if not ids:
        raise ValidationError("report_ids 不能为空")
    return service().compare_reports(ids)


@router.get("/reports/{report_id}")
def get_report(report_id: int):
    return service().get_report(report_id)


@router.post("/reports/{report_id}/publish")
def publish_report(report_id: int, payload: ReportPublish):
    return service().publish_report(report_id, actor=payload.actor)


@router.post("/reports/{report_id}/lock")
def lock_report(report_id: int, payload: ReportLock):
    return service().lock_report(report_id, actor=payload.actor, reason=payload.reason)


@router.post("/reports/{report_id}/errata", status_code=201)
def append_erratum(report_id: int, payload: ErratumCreate):
    return service().append_erratum(report_id, dump(payload), actor=payload.actor)


@router.get("/reports/{report_id}/errata")
def list_errata(report_id: int):
    return {"items": service().list_errata(report_id)}
