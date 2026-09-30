from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CODE_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"


class ActivityCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=CODE_PATTERN)
    name: str = Field(min_length=2, max_length=120)
    kind: str = Field(min_length=2, max_length=40, description="活动类型，如 导赏 / 自然观察")
    location: str = Field(default="", max_length=120)
    starts_on: date
    ends_on: date | None = None
    actor: str = Field(min_length=1, max_length=80)


class ParticipationBatchUpload(BaseModel):
    batch_key: str = Field(min_length=4, max_length=120)
    activity_code: str = Field(min_length=2, max_length=64)
    occurred_on: date
    participant_count: int = Field(ge=1, le=1000000)
    returning_count: int = Field(default=0, ge=0)
    channel: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=500)
    actor: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def check_returning(self) -> "ParticipationBatchUpload":
        if self.returning_count > self.participant_count:
            raise ValueError("重复参与人数不能超过批次总人数")
        return self


class ObservationRecordUpload(BaseModel):
    record_key: str = Field(min_length=4, max_length=120)
    batch_key: str = Field(min_length=4, max_length=120)
    observed_on: date
    observers: int = Field(default=0, ge=0, le=1000000)
    actions: int = Field(default=0, ge=0, le=1000000)
    minutes: int = Field(default=0, ge=0, le=1000000)
    actor: str = Field(min_length=1, max_length=80)


class SurveySummaryUpload(BaseModel):
    survey_key: str = Field(min_length=4, max_length=120)
    batch_key: str = Field(min_length=4, max_length=120)
    distributed: int = Field(default=0, ge=0, le=1000000)
    returned: int = Field(default=0, ge=0, le=1000000)
    follow_up_intents: int = Field(default=0, ge=0, le=1000000)
    follow_up_actions: int = Field(default=0, ge=0, le=1000000)
    actor: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def check_counts(self) -> "SurveySummaryUpload":
        if self.returned > self.distributed:
            raise ValueError("回收问卷数不能超过发放数")
        if self.follow_up_intents > self.returned:
            raise ValueError("后续行动意向数不能超过回收问卷数")
        if self.follow_up_actions > self.follow_up_intents:
            raise ValueError("已核实后续行动数不能超过行动意向数")
        return self


class DepthWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: float = Field(default=1.0, ge=0)
    minute: float = Field(default=0.0, ge=0)
    returning: float = Field(default=0.0, ge=0)


class DefinitionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depth_weights: DepthWeights = Field(default_factory=DepthWeights)
    min_coverage: float = Field(default=0.6, ge=0, le=1)
    min_returned: int = Field(default=1, ge=0, le=1000000)


class DefinitionCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=CODE_PATTERN)
    name: str = Field(min_length=2, max_length=120)
    params: DefinitionParams = Field(default_factory=DefinitionParams)
    actor: str = Field(min_length=1, max_length=80)


class ReportCompute(BaseModel):
    scope_type: Literal["activity", "period"]
    activity_code: str | None = Field(default=None, min_length=2, max_length=64)
    period_start: date | None = None
    period_end: date | None = None
    definition_code: str = Field(min_length=2, max_length=64)
    definition_version: int | None = Field(default=None, ge=1)
    actor: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def check_scope(self) -> "ReportCompute":
        if self.scope_type == "activity" and not self.activity_code:
            raise ValueError("活动口径统计必须提供 activity_code")
        if self.scope_type == "period":
            if self.period_start is None or self.period_end is None:
                raise ValueError("期间口径统计必须提供 period_start 和 period_end")
            if self.period_end < self.period_start:
                raise ValueError("统计结束日期不能早于开始日期")
        return self


class ReportPublish(BaseModel):
    actor: str = Field(min_length=1, max_length=80)


class ReportLock(BaseModel):
    actor: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=2, max_length=1000)


class ErratumCreate(BaseModel):
    actor: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=2, max_length=1000)
    corrections: dict[str, float | None] = Field(default_factory=dict)
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def check_content(self) -> "ErratumCreate":
        if not self.corrections and not self.note:
            raise ValueError("勘误至少需要更正指标或填写说明")
        return self


class ReportCompare(BaseModel):
    report_ids: list[int] = Field(min_length=1, max_length=50)


def dump(payload: BaseModel) -> dict[str, Any]:
    """以 JSON 模式导出，确保日期等类型序列化为字符串后再进入服务层。"""
    return payload.model_dump(mode="json")
