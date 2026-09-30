from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import from_storage, to_storage, utc_now
from app.core.errors import ConflictError, NotFoundError
from app.database import get_connection, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS impact_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS impact_participation_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL UNIQUE,
    activity_id INTEGER NOT NULL REFERENCES impact_activities(id) ON DELETE RESTRICT,
    participant_count INTEGER NOT NULL CHECK(participant_count >= 0),
    new_participant_count INTEGER NOT NULL DEFAULT 0 CHECK(new_participant_count >= 0),
    returning_participant_count INTEGER NOT NULL DEFAULT 0 CHECK(returning_participant_count >= 0),
    occurred_on TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    payload_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_batches_activity ON impact_participation_batches(activity_id, occurred_on);
CREATE TABLE IF NOT EXISTS impact_observation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_code TEXT NOT NULL UNIQUE,
    activity_id INTEGER NOT NULL REFERENCES impact_activities(id) ON DELETE RESTRICT,
    batch_code TEXT NOT NULL DEFAULT '',
    participant_alias TEXT NOT NULL DEFAULT '',
    behavior_type TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity >= 0),
    observed_at TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_events_activity ON impact_observation_events(activity_id, observed_at);
CREATE TABLE IF NOT EXISTS impact_survey_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_code TEXT NOT NULL UNIQUE,
    activity_id INTEGER NOT NULL REFERENCES impact_activities(id) ON DELETE RESTRICT,
    batch_code TEXT NOT NULL DEFAULT '',
    distributed_count INTEGER NOT NULL CHECK(distributed_count >= 0),
    returned_count INTEGER NOT NULL CHECK(returned_count >= 0),
    care_score_avg REAL,
    action_commit_count INTEGER NOT NULL DEFAULT 0 CHECK(action_commit_count >= 0),
    collected_at TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_surveys_activity ON impact_survey_summaries(activity_id, collected_at);
CREATE TABLE IF NOT EXISTS impact_formula_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(code, version)
);
CREATE TABLE IF NOT EXISTS impact_metric_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL REFERENCES impact_activities(id) ON DELETE RESTRICT,
    formula_version_id INTEGER NOT NULL REFERENCES impact_formula_versions(id) ON DELETE RESTRICT,
    report_seq INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'computed' CHECK(status IN ('computed','published','locked')),
    input_digest TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    computed_by TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    published_by TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    locked_by TEXT NOT NULL DEFAULT '',
    locked_at TEXT NOT NULL DEFAULT '',
    UNIQUE(activity_id, formula_version_id, input_digest),
    UNIQUE(activity_id, formula_version_id, report_seq)
);
CREATE INDEX IF NOT EXISTS idx_impact_reports_activity ON impact_metric_reports(activity_id, formula_version_id, report_seq);
CREATE TABLE IF NOT EXISTS impact_report_errata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES impact_metric_reports(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    metrics_patch_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_errata_report ON impact_report_errata(report_id, id);
"""


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _now() -> str:
    return to_storage(utc_now())


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _compute_metrics(
    activity: sqlite3.Row,
    definition: dict[str, Any],
    batches: list[sqlite3.Row],
    events: list[sqlite3.Row],
    surveys: list[sqlite3.Row],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按口径定义计算参与深度与后续行动率，缺失问卷只标记覆盖不足，不按零处理。"""
    depth_cfg = definition["depth"]
    follow_cfg = definition["follow_up"]
    coverage_cfg = definition["coverage"]

    participants_total = sum(int(row["participant_count"]) for row in batches)
    participants_new = sum(int(row["new_participant_count"]) for row in batches)
    participants_returning = sum(int(row["returning_participant_count"]) for row in batches)
    observation_quantity = sum(int(row["quantity"]) for row in events)

    repeat_rate = _round(participants_returning / participants_total) if participants_total else None
    depth_score = None
    if participants_total:
        depth_score = _round(
            (depth_cfg["observation_weight"] * observation_quantity + depth_cfg["returning_weight"] * participants_returning)
            / participants_total
        )

    survey_distributed = sum(int(row["distributed_count"]) for row in surveys)
    survey_returned = sum(int(row["returned_count"]) for row in surveys)
    scored = [row for row in surveys if row["care_score_avg"] is not None and int(row["returned_count"]) > 0]
    scored_returned = sum(int(row["returned_count"]) for row in scored)
    care_score_avg = None
    if scored_returned:
        care_score_avg = _round(sum(float(row["care_score_avg"]) * int(row["returned_count"]) for row in scored) / scored_returned)
    action_commit_count = sum(int(row["action_commit_count"]) for row in surveys) if surveys else None

    coverage_rate = _round(survey_returned / participants_total) if (participants_total and surveys) else None
    if not surveys:
        coverage_status = "missing"
    elif coverage_rate is not None and coverage_rate >= coverage_cfg["survey_min_rate"]:
        coverage_status = "sufficient"
    else:
        coverage_status = "insufficient"

    ends_at = from_storage(activity["ends_at"])
    window_days = int(follow_cfg["window_days"])
    window_end = ends_at + timedelta(days=window_days)
    action_types = set(follow_cfg["action_types"])
    action_rows = [
        row
        for row in events
        if row["behavior_type"] in action_types
        and ends_at < from_storage(row["observed_at"]) <= window_end
    ]
    action_events = sum(int(row["quantity"]) for row in action_rows)
    action_aliases = {row["participant_alias"] for row in action_rows if row["participant_alias"]}

    basis = follow_cfg["basis"]
    insufficient = coverage_status != "sufficient"
    follow_rate: float | None = None
    estimate = False
    if basis == "survey":
        if not insufficient and survey_returned:
            follow_rate = _round((action_commit_count or 0) / survey_returned)
    else:
        if participants_total:
            if action_aliases:
                follow_rate = _round(len(action_aliases) / participants_total)
            else:
                follow_rate = _round(action_events / participants_total)
                estimate = True

    metrics = {
        "participants_total": participants_total,
        "participants_new": participants_new,
        "participants_returning": participants_returning,
        "repeat_participation_rate": repeat_rate,
        "observation_events": len(events),
        "observation_quantity": observation_quantity,
        "depth_score": depth_score,
        "survey": {
            "distributed": survey_distributed,
            "returned": survey_returned,
            "care_score_avg": care_score_avg,
            "action_commit_count": action_commit_count,
        },
        "follow_up": {
            "basis": basis,
            "window_days": window_days,
            "action_types": sorted(action_types),
            "action_events": action_events,
            "action_participants": len(action_aliases),
            "rate": follow_rate,
            "estimate": estimate,
            "insufficient_coverage": basis == "survey" and insufficient,
        },
    }
    coverage = {
        "participants_total": participants_total,
        "survey_distributed": survey_distributed,
        "survey_returned": survey_returned,
        "coverage_rate": coverage_rate,
        "survey_min_rate": coverage_cfg["survey_min_rate"],
        "coverage_status": coverage_status,
    }
    return metrics, coverage


class ImpactService:
    """活动影响指标的事务服务：数据接收、口径版本、可复核计算、发布锁定与勘误。"""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_schema()

    # ---------- 活动 ----------

    def create_activity(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM impact_activities WHERE activity_code=?", (payload["activity_code"],)).fetchone():
                raise ConflictError("活动编码已存在")
            cursor = connection.execute(
                "INSERT INTO impact_activities(activity_code,title,category,location,starts_at,ends_at,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    payload["activity_code"], payload["title"], payload["category"], payload["location"],
                    to_storage(payload["starts_at"]), to_storage(payload["ends_at"]), payload["created_by"], now, now,
                ),
            )
            return dict(connection.execute("SELECT * FROM impact_activities WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_activities(self, category: str | None = None) -> list[dict[str, Any]]:
        if category:
            rows = self.connection.execute("SELECT * FROM impact_activities WHERE category=? ORDER BY activity_code", (category,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM impact_activities ORDER BY activity_code").fetchall()
        return [dict(row) for row in rows]

    def get_activity(self, activity_code: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM impact_activities WHERE activity_code=?", (activity_code,)).fetchone()
        if row is None:
            raise NotFoundError("活动不存在")
        result = dict(row)
        for key, table in (("batches", "impact_participation_batches"), ("observation_events", "impact_observation_events"), ("survey_summaries", "impact_survey_summaries"), ("reports", "impact_metric_reports")):
            result[f"{key}_count"] = int(self.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE activity_id=?", (row["id"],)).fetchone()[0])
        return result

    def _activity_or_404(self, connection: sqlite3.Connection, activity_code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM impact_activities WHERE activity_code=?", (activity_code,)).fetchone()
        if row is None:
            raise NotFoundError("活动不存在", context={"activity_code": activity_code})
        return row

    # ---------- 数据接收（幂等） ----------

    @staticmethod
    def _dedupe_or_raise(connection: sqlite3.Connection, table: str, code_field: str, code: str, payload_digest: str) -> dict[str, Any] | None:
        existing = connection.execute(f"SELECT * FROM {table} WHERE {code_field}=?", (code,)).fetchone()
        if existing is None:
            return None
        if existing["payload_digest"] != payload_digest:
            raise ConflictError("同一编码对应了不同的数据内容", context={code_field: code})
        return dict(existing)

    def ingest_batch(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        canonical = {**payload, "occurred_on": payload["occurred_on"].isoformat()}
        payload_digest = digest(canonical)
        with transaction(immediate=True) as connection:
            existing = self._dedupe_or_raise(connection, "impact_participation_batches", "batch_code", payload["batch_code"], payload_digest)
            if existing is not None:
                return existing, True
            activity = self._activity_or_404(connection, payload["activity_code"])
            cursor = connection.execute(
                "INSERT INTO impact_participation_batches(batch_code,activity_id,participant_count,new_participant_count,returning_participant_count,occurred_on,source,note,payload_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["batch_code"], activity["id"], payload["participant_count"], payload["new_participant_count"],
                    payload["returning_participant_count"], canonical["occurred_on"], payload["source"], payload["note"],
                    payload_digest, _now(),
                ),
            )
            return dict(connection.execute("SELECT * FROM impact_participation_batches WHERE id=?", (cursor.lastrowid,)).fetchone()), False

    def ingest_event(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        canonical = {**payload, "observed_at": to_storage(payload["observed_at"])}
        payload_digest = digest(canonical)
        with transaction(immediate=True) as connection:
            existing = self._dedupe_or_raise(connection, "impact_observation_events", "event_code", payload["event_code"], payload_digest)
            if existing is not None:
                return existing, True
            activity = self._activity_or_404(connection, payload["activity_code"])
            cursor = connection.execute(
                "INSERT INTO impact_observation_events(event_code,activity_id,batch_code,participant_alias,behavior_type,quantity,observed_at,payload_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    payload["event_code"], activity["id"], payload["batch_code"], payload["participant_alias"],
                    payload["behavior_type"], payload["quantity"], canonical["observed_at"], payload_digest, _now(),
                ),
            )
            return dict(connection.execute("SELECT * FROM impact_observation_events WHERE id=?", (cursor.lastrowid,)).fetchone()), False

    def ingest_survey(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        canonical = {**payload, "collected_at": to_storage(payload["collected_at"])}
        payload_digest = digest(canonical)
        with transaction(immediate=True) as connection:
            existing = self._dedupe_or_raise(connection, "impact_survey_summaries", "survey_code", payload["survey_code"], payload_digest)
            if existing is not None:
                return existing, True
            activity = self._activity_or_404(connection, payload["activity_code"])
            cursor = connection.execute(
                "INSERT INTO impact_survey_summaries(survey_code,activity_id,batch_code,distributed_count,returned_count,care_score_avg,action_commit_count,collected_at,payload_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["survey_code"], activity["id"], payload["batch_code"], payload["distributed_count"],
                    payload["returned_count"], payload["care_score_avg"], payload["action_commit_count"],
                    canonical["collected_at"], payload_digest, _now(),
                ),
            )
            return dict(connection.execute("SELECT * FROM impact_survey_summaries WHERE id=?", (cursor.lastrowid,)).fetchone()), False

    def list_records(self, kind: str, activity_code: str | None) -> list[dict[str, Any]]:
        table, code_field = {
            "batches": ("impact_participation_batches", "batch_code"),
            "events": ("impact_observation_events", "event_code"),
            "surveys": ("impact_survey_summaries", "survey_code"),
        }[kind]
        if activity_code:
            activity = self._activity_or_404(self.connection, activity_code)
            rows = self.connection.execute(f"SELECT * FROM {table} WHERE activity_id=? ORDER BY {code_field}", (activity["id"],)).fetchall()
        else:
            rows = self.connection.execute(f"SELECT * FROM {table} ORDER BY {code_field}").fetchall()
        return [dict(row) for row in rows]

    # ---------- 指标口径版本 ----------

    def create_formula(self, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM impact_formula_versions WHERE code=?", (payload["code"],)).fetchone():
                raise ConflictError("指标口径编码已存在，请使用新版本接口更新公式")
            return self._insert_formula_version(connection, payload["code"], 1, payload["name"], payload["definition"], payload["created_by"])

    def create_formula_version(self, code: str, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            latest = connection.execute("SELECT * FROM impact_formula_versions WHERE code=? ORDER BY version DESC LIMIT 1", (code,)).fetchone()
            if latest is None:
                raise NotFoundError("指标口径不存在，请先创建首个版本")
            name = payload["name"] or latest["name"]
            return self._insert_formula_version(connection, code, int(latest["version"]) + 1, name, payload["definition"], payload["created_by"])

    @staticmethod
    def _insert_formula_version(connection: sqlite3.Connection, code: str, version: int, name: str, definition: dict[str, Any], created_by: str) -> dict[str, Any]:
        cursor = connection.execute(
            "INSERT INTO impact_formula_versions(code,version,name,definition_json,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (code, version, name, json.dumps(definition, ensure_ascii=False, sort_keys=True), created_by, _now()),
        )
        return dict(connection.execute("SELECT * FROM impact_formula_versions WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_formulas(self, code: str | None = None) -> list[dict[str, Any]]:
        if code:
            rows = self.connection.execute("SELECT * FROM impact_formula_versions WHERE code=? ORDER BY version", (code,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM impact_formula_versions ORDER BY code, version").fetchall()
        return [self._formula_dict(row) for row in rows]

    @staticmethod
    def _formula_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["definition"] = json.loads(result.pop("definition_json"))
        return result

    # ---------- 指标计算 ----------

    def compute_report(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        with transaction(immediate=True) as connection:
            activity = self._activity_or_404(connection, payload["activity_code"])
            formula = connection.execute(
                "SELECT * FROM impact_formula_versions WHERE code=? AND version=?",
                (payload["formula_code"], payload["formula_version"]),
            ).fetchone()
            if formula is None:
                raise NotFoundError("指标口径版本不存在", context={"code": payload["formula_code"], "version": payload["formula_version"]})
            definition = json.loads(formula["definition_json"])
            batches = connection.execute("SELECT * FROM impact_participation_batches WHERE activity_id=? ORDER BY batch_code", (activity["id"],)).fetchall()
            events = connection.execute("SELECT * FROM impact_observation_events WHERE activity_id=? ORDER BY event_code", (activity["id"],)).fetchall()
            surveys = connection.execute("SELECT * FROM impact_survey_summaries WHERE activity_id=? ORDER BY survey_code", (activity["id"],)).fetchall()

            inputs = {
                "activity": {"activity_code": activity["activity_code"], "category": activity["category"], "starts_at": activity["starts_at"], "ends_at": activity["ends_at"]},
                "formula": {"code": formula["code"], "version": formula["version"], "definition": definition},
                "batches": [
                    {"batch_code": row["batch_code"], "participant_count": row["participant_count"], "new_participant_count": row["new_participant_count"], "returning_participant_count": row["returning_participant_count"], "occurred_on": row["occurred_on"]}
                    for row in batches
                ],
                "observation_events": [
                    {"event_code": row["event_code"], "behavior_type": row["behavior_type"], "quantity": row["quantity"], "participant_alias": row["participant_alias"], "observed_at": row["observed_at"]}
                    for row in events
                ],
                "survey_summaries": [
                    {"survey_code": row["survey_code"], "distributed_count": row["distributed_count"], "returned_count": row["returned_count"], "care_score_avg": row["care_score_avg"], "action_commit_count": row["action_commit_count"]}
                    for row in surveys
                ],
            }
            input_digest = digest(inputs)

            existing = connection.execute(
                "SELECT * FROM impact_metric_reports WHERE activity_id=? AND formula_version_id=? AND input_digest=?",
                (activity["id"], formula["id"], input_digest),
            ).fetchone()
            if existing is not None:
                return self._report_detail(connection, existing), True

            latest = connection.execute(
                "SELECT * FROM impact_metric_reports WHERE activity_id=? AND formula_version_id=? ORDER BY report_seq DESC LIMIT 1",
                (activity["id"], formula["id"]),
            ).fetchone()
            if latest is not None and latest["status"] == "locked":
                raise ConflictError("该口径的最新结果已锁定，不能静默重算；请通过勘误修正", context={"report_id": latest["id"]})

            metrics, coverage = _compute_metrics(activity, definition, batches, events, surveys)
            sources = {
                "activity_code": activity["activity_code"],
                "formula": {"code": formula["code"], "version": formula["version"]},
                "batches": [row["batch_code"] for row in batches],
                "observation_events": [row["event_code"] for row in events],
                "survey_summaries": [row["survey_code"] for row in surveys],
                "counts": {"batches": len(batches), "observation_events": len(events), "survey_summaries": len(surveys)},
            }
            report_seq = int(latest["report_seq"]) + 1 if latest else 1
            cursor = connection.execute(
                "INSERT INTO impact_metric_reports(activity_id,formula_version_id,report_seq,status,input_digest,sources_json,coverage_json,metrics_json,computed_by,computed_at) VALUES(?,?,?,'computed',?,?,?,?,?,?)",
                (
                    activity["id"], formula["id"], report_seq, input_digest,
                    json.dumps(sources, ensure_ascii=False, sort_keys=True),
                    json.dumps(coverage, ensure_ascii=False, sort_keys=True),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    payload["computed_by"], _now(),
                ),
            )
            row = connection.execute("SELECT * FROM impact_metric_reports WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._report_detail(connection, row), False

    # ---------- 报告查询 ----------

    def _report_detail(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        activity = connection.execute("SELECT * FROM impact_activities WHERE id=?", (row["activity_id"],)).fetchone()
        formula = connection.execute("SELECT * FROM impact_formula_versions WHERE id=?", (row["formula_version_id"],)).fetchone()
        errata_rows = connection.execute("SELECT * FROM impact_report_errata WHERE report_id=? ORDER BY id", (row["id"],)).fetchall()
        metrics = json.loads(row["metrics_json"])
        errata = [
            {"id": item["id"], "actor": item["actor"], "note": item["note"], "metrics_patch": json.loads(item["metrics_patch_json"]), "created_at": item["created_at"]}
            for item in errata_rows
        ]
        effective = dict(metrics)
        for item in errata:
            if item["metrics_patch"]:
                effective = _deep_merge(effective, item["metrics_patch"])
        return {
            "id": row["id"],
            "activity_code": activity["activity_code"],
            "activity_title": activity["title"],
            "category": activity["category"],
            "formula": {"code": formula["code"], "version": formula["version"], "name": formula["name"]},
            "report_seq": row["report_seq"],
            "status": row["status"],
            "input_digest": row["input_digest"],
            "sources": json.loads(row["sources_json"]),
            "coverage": json.loads(row["coverage_json"]),
            "metrics": metrics,
            "effective_metrics": effective,
            "errata": errata,
            "computed_by": row["computed_by"],
            "computed_at": row["computed_at"],
            "published_by": row["published_by"],
            "published_at": row["published_at"],
            "locked_by": row["locked_by"],
            "locked_at": row["locked_at"],
        }

    @staticmethod
    def _report_summary(detail: dict[str, Any]) -> dict[str, Any]:
        metrics = detail["metrics"]
        return {
            "id": detail["id"],
            "activity_code": detail["activity_code"],
            "category": detail["category"],
            "formula": detail["formula"],
            "report_seq": detail["report_seq"],
            "status": detail["status"],
            "input_digest": detail["input_digest"],
            "sources": detail["sources"],
            "coverage_status": detail["coverage"]["coverage_status"],
            "coverage_rate": detail["coverage"]["coverage_rate"],
            "headline": {
                "depth_score": metrics["depth_score"],
                "follow_up_rate": metrics["follow_up"]["rate"],
                "repeat_participation_rate": metrics["repeat_participation_rate"],
            },
            "errata_count": len(detail["errata"]),
            "computed_at": detail["computed_at"],
        }

    def get_report(self, report_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM impact_metric_reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise NotFoundError("指标报告不存在")
        return self._report_detail(self.connection, row)

    def list_reports(self, *, activity_code: str | None = None, formula_code: str | None = None, formula_version: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if activity_code:
            clauses.append("a.activity_code=?")
            values.append(activity_code)
        if formula_code:
            clauses.append("f.code=?")
            values.append(formula_code)
        if formula_version is not None:
            clauses.append("f.version=?")
            values.append(formula_version)
        if status:
            clauses.append("r.status=?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT r.* FROM impact_metric_reports r JOIN impact_activities a ON a.id=r.activity_id JOIN impact_formula_versions f ON f.id=r.formula_version_id"
            + where + " ORDER BY a.activity_code, f.code, f.version, r.report_seq",
            values,
        ).fetchall()
        return [self._report_summary(self._report_detail(self.connection, row)) for row in rows]

    def compare_activity(self, activity_code: str) -> dict[str, Any]:
        activity = self._activity_or_404(self.connection, activity_code)
        rows = self.connection.execute(
            "SELECT r.* FROM impact_metric_reports r JOIN impact_formula_versions f ON f.id=r.formula_version_id WHERE r.activity_id=? ORDER BY f.code, f.version, r.report_seq",
            (activity["id"],),
        ).fetchall()
        return {
            "activity_code": activity["activity_code"],
            "title": activity["title"],
            "category": activity["category"],
            "reports": [self._report_summary(self._report_detail(self.connection, row)) for row in rows],
        }

    # ---------- 发布、锁定与勘误 ----------

    def _report_row_or_404(self, connection: sqlite3.Connection, report_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM impact_metric_reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise NotFoundError("指标报告不存在")
        return row

    def publish_report(self, report_id: int, actor: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._report_row_or_404(connection, report_id)
            if row["status"] == "computed":
                connection.execute("UPDATE impact_metric_reports SET status='published',published_by=?,published_at=? WHERE id=?", (actor, _now(), report_id))
                row = self._report_row_or_404(connection, report_id)
            return self._report_detail(connection, row)

    def lock_report(self, report_id: int, actor: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._report_row_or_404(connection, report_id)
            if row["status"] == "computed":
                raise ConflictError("只有已发布的报告可以锁定")
            if row["status"] == "published":
                connection.execute("UPDATE impact_metric_reports SET status='locked',locked_by=?,locked_at=? WHERE id=?", (actor, _now(), report_id))
                row = self._report_row_or_404(connection, report_id)
            return self._report_detail(connection, row)

    def add_errata(self, report_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._report_row_or_404(connection, report_id)
            if row["status"] != "locked":
                raise ConflictError("只有已锁定的报告可以追加勘误")
            connection.execute(
                "INSERT INTO impact_report_errata(report_id,actor,note,metrics_patch_json,created_at) VALUES(?,?,?,?,?)",
                (report_id, payload["actor"], payload["note"], json.dumps(payload["metrics_patch"], ensure_ascii=False, sort_keys=True), _now()),
            )
            return self._report_detail(connection, self._report_row_or_404(connection, report_id))
