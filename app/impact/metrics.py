"""活动影响指标的纯计算逻辑：口径参数校验、参与深度与后续行动率、问卷覆盖判定。

该模块不接触数据库，输入为已归集的批次、观察记录和问卷摘要，
输出可复核的指标与覆盖说明，便于单独测试和审计复算。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.errors import ValidationError

DEFAULT_PARAMS: dict[str, Any] = {
    "depth_weights": {"action": 1.0, "minute": 0.0, "returning": 0.0},
    "min_coverage": 0.6,
    "min_returned": 1,
}

METRIC_FIELDS: tuple[str, ...] = (
    "participants_total",
    "returning_participants",
    "new_participants",
    "observers_total",
    "observation_actions",
    "observation_minutes",
    "participation_depth",
    "questionnaires_distributed",
    "questionnaires_returned",
    "response_rate",
    "follow_up_intents",
    "follow_up_actions",
    "follow_up_rate",
)


def canonical_digest(value: Any) -> str:
    """对任意可 JSON 化的数据生成稳定的 SHA-256 摘要，用于幂等和来源指纹。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def normalize_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """校验并补全口径参数，保证同一版本对应的公式完全确定。"""
    raw = params or {}
    unknown = set(raw) - set(DEFAULT_PARAMS)
    if unknown:
        raise ValidationError("口径包含未声明的参数", context={"parameters": sorted(unknown)})
    weights_raw = raw.get("depth_weights", {})
    if not isinstance(weights_raw, dict):
        raise ValidationError("depth_weights 必须是对象")
    unknown_weights = set(weights_raw) - set(DEFAULT_PARAMS["depth_weights"])
    if unknown_weights:
        raise ValidationError("深度权重包含未声明的项", context={"weights": sorted(unknown_weights)})
    merged: dict[str, Any] = {
        "depth_weights": {**DEFAULT_PARAMS["depth_weights"], **weights_raw},
        "min_coverage": raw.get("min_coverage", DEFAULT_PARAMS["min_coverage"]),
        "min_returned": raw.get("min_returned", DEFAULT_PARAMS["min_returned"]),
    }
    for name, value in merged["depth_weights"].items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValidationError(f"深度权重 {name} 必须是非负数值")
    coverage = merged["min_coverage"]
    if not isinstance(coverage, (int, float)) or isinstance(coverage, bool) or not 0 <= coverage <= 1:
        raise ValidationError("min_coverage 必须位于 0 到 1 之间")
    returned = merged["min_returned"]
    if not isinstance(returned, int) or isinstance(returned, bool) or returned < 0:
        raise ValidationError("min_returned 必须是非负整数")
    return merged


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def compute_snapshot(
    batches: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    surveys: list[dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    """按口径参数计算一次指标快照。

    batches: 参与批次（participant_count、returning_count、batch_key）。
    observations: 观察行为记录（actions、minutes、observers）。
    surveys: 每批次至多一条有效问卷摘要（distributed、returned、follow_up_intents、follow_up_actions）。
    缺失问卷的批次计入覆盖率分母，但不进入问卷指标基数，且会触发覆盖不足标记。
    """
    participants_total = sum(int(batch["participant_count"]) for batch in batches)
    returning = sum(int(batch["returning_count"]) for batch in batches)
    actions = sum(int(item["actions"]) for item in observations)
    minutes = sum(int(item["minutes"]) for item in observations)
    observers = sum(int(item["observers"]) for item in observations)

    survey_batches = {survey["batch_key"] for survey in surveys}
    covered = [batch for batch in batches if batch["batch_key"] in survey_batches]
    missing = sorted(batch["batch_key"] for batch in batches if batch["batch_key"] not in survey_batches)
    covered_participants = sum(int(batch["participant_count"]) for batch in covered)

    distributed = sum(int(survey["distributed"]) for survey in surveys)
    returned = sum(int(survey["returned"]) for survey in surveys)
    intents = sum(int(survey["follow_up_intents"]) for survey in surveys)
    follow_up_actions = sum(int(survey["follow_up_actions"]) for survey in surveys)

    weights = params["depth_weights"]
    depth_score = weights["action"] * actions + weights["minute"] * minutes + weights["returning"] * returning
    coverage_ratio = _ratio(covered_participants, participants_total)

    reasons: list[str] = []
    if participants_total == 0:
        reasons.append("统计范围内没有参与批次")
    if missing:
        reasons.append("以下批次缺少问卷摘要：" + "、".join(missing))
    if coverage_ratio is not None and coverage_ratio < params["min_coverage"]:
        reasons.append(f"问卷覆盖率 {coverage_ratio:.2f} 低于阈值 {params['min_coverage']:.2f}")
    if returned < params["min_returned"]:
        reasons.append(f"回收问卷 {returned} 份低于最少要求 {params['min_returned']} 份")
    sufficient = not reasons

    metrics = {
        "participants_total": participants_total,
        "returning_participants": returning,
        "new_participants": participants_total - returning,
        "observers_total": observers,
        "observation_actions": actions,
        "observation_minutes": minutes,
        "participation_depth": _ratio(depth_score, participants_total),
        "questionnaires_distributed": distributed,
        "questionnaires_returned": returned,
        "response_rate": _ratio(returned, distributed),
        "follow_up_intents": intents,
        "follow_up_actions": follow_up_actions,
        # 覆盖不足时后续行动率置空而不是按零处理，避免把缺失问卷误读为无人行动
        "follow_up_rate": _ratio(intents, returned) if sufficient else None,
    }
    coverage = {
        "status": "sufficient" if sufficient else "insufficient",
        "total_participants": participants_total,
        "covered_participants": covered_participants,
        "coverage_ratio": coverage_ratio,
        "min_coverage": params["min_coverage"],
        "min_returned": params["min_returned"],
        "batches_total": len(batches),
        "batches_with_survey": len(covered),
        "missing_survey_batches": missing,
        "reasons": reasons,
    }
    return {"metrics": metrics, "coverage": coverage}
