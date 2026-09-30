from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

_CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]+$"


class ActivityCreate(BaseModel):
    activity_code: str = Field(min_length=2, max_length=64, pattern=_CODE_PATTERN)
    title: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=40, description="活动类型，如 导赏 / 自然观察")
    location: str = Field(default="", max_length=120)
    starts_at: datetime
    ends_at: datetime
    created_by: str = Field(default="system", min_length=1, max_length=80)

    @model_validator(mode="after")
    def check_time_range(self) -> "ActivityCreate":
        if self.ends_at < self.starts_at:
            raise ValueError("活动结束时间不能早于开始时间")
        return self


class ParticipationBatchCreate(BaseModel):
    batch_code: str = Field(min_length=1, max_length=80)
    activity_code: str = Field(min_length=2, max_length=80)
    participant_count: int = Field(ge=0, le=1000000)
    new_participant_count: int = Field(default=0, ge=0, le=1000000)
    returning_participant_count: int = Field(default=0, ge=0, le=1000000)
    occurred_on: date
    source: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def check_breakdown(self) -> "ParticipationBatchCreate":
        if self.new_participant_count + self.returning_participant_count > self.participant_count:
            raise ValueError("首次与重复参与人数之和不能超过批次总人数")
        return self


class ObservationEventCreate(BaseModel):
    event_code: str = Field(min_length=1, max_length=80)
    activity_code: str = Field(min_length=2, max_length=80)
    batch_code: str = Field(default="", max_length=80)
    participant_alias: str = Field(default="", max_length=80, description="匿名参与者别名，可为空")
    behavior_type: str = Field(min_length=1, max_length=40, description="行为类型，如 物种记录 / 复访 / 志愿报名")
    quantity: int = Field(default=1, ge=0, le=1000000)
    observed_at: datetime


class SurveySummaryCreate(BaseModel):
    survey_code: str = Field(min_length=1, max_length=80)
    activity_code: str = Field(min_length=2, max_length=80)
    batch_code: str = Field(default="", max_length=80)
    distributed_count: int = Field(ge=0, le=1000000)
    returned_count: int = Field(ge=0, le=1000000)
    care_score_avg: float | None = Field(default=None, ge=0, le=100, description="关心环境意愿平均分")
    action_commit_count: int = Field(default=0, ge=0, le=1000000, description="承诺采取后续行动的人数")
    collected_at: datetime

    @model_validator(mode="after")
    def check_counts(self) -> "SurveySummaryCreate":
        if self.returned_count > self.distributed_count:
            raise ValueError("回收问卷数不能大于发放数")
        if self.action_commit_count > self.returned_count:
            raise ValueError("承诺后续行动人数不能大于回收问卷数")
        return self


class DepthConfig(BaseModel):
    observation_weight: float = Field(default=1.0, ge=0, le=1000, description="每条观察行为记录的权重")
    returning_weight: float = Field(default=2.0, ge=0, le=1000, description="每名重复参与者的权重")


class FollowUpConfig(BaseModel):
    action_types: list[str] = Field(min_length=1, max_length=50, description="计入后续行动的行为类型")
    window_days: int = Field(default=90, ge=1, le=3650, description="活动结束后统计后续行动的天数窗口")
    basis: Literal["survey", "behavior"] = Field(default="survey", description="后续行动率依据：问卷或行为记录")


class CoverageConfig(BaseModel):
    survey_min_rate: float = Field(default=0.6, gt=0, le=1, description="问卷回收人数占参与人数的最低覆盖率")


class FormulaDefinition(BaseModel):
    depth: DepthConfig = Field(default_factory=DepthConfig)
    follow_up: FollowUpConfig
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)


class FormulaCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    definition: FormulaDefinition
    created_by: str = Field(default="system", min_length=1, max_length=80)


class FormulaVersionCreate(BaseModel):
    definition: FormulaDefinition
    name: str | None = Field(default=None, min_length=1, max_length=120)
    created_by: str = Field(default="system", min_length=1, max_length=80)


class ComputeRequest(BaseModel):
    activity_code: str = Field(min_length=2, max_length=80)
    formula_code: str = Field(min_length=2, max_length=64)
    formula_version: int = Field(ge=1, le=1000000)
    computed_by: str = Field(default="system", min_length=1, max_length=80)


class ReportAction(BaseModel):
    actor: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)


class ErrataCreate(BaseModel):
    actor: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=2, max_length=1000)
    metrics_patch: dict[str, Any] = Field(default_factory=dict, description="对指标值的勘误修正，按层级合并")
