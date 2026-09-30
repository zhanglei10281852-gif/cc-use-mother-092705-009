"""活动影响指标服务：数据接收、口径版本、可复核计算、发布锁定与勘误。

设计要点：
- 参与批次、观察记录、问卷摘要均按业务键幂等接收，重复上传不会重复计入；
  同一业务键对应不同内容时返回冲突，避免静默覆盖。
- 指标口径（公式参数）按编码自动递增版本，旧版本与旧计算结果永久保留。
- 每次计算生成不可变的修订记录，完整保留来源清单与输入摘要，可随时复核。
- 报告状态机为 draft → published → locked；锁定后不能重新计算，
  只允许追加勘误，勘误本身也是只增不改的。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.impact.metrics import METRIC_FIELDS, canonical_digest, compute_snapshot, normalize_params

SCHEMA = """
CREATE TABLE IF NOT EXISTS impact_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS impact_participation_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key TEXT NOT NULL UNIQUE,
    activity_id INTEGER NOT NULL REFERENCES impact_activities(id) ON DELETE RESTRICT,
    occurred_on TEXT NOT NULL,
    participant_count INTEGER NOT NULL CHECK(participant_count >= 1),
    returning_count INTEGER NOT NULL DEFAULT 0 CHECK(returning_count >= 0),
    channel TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    content_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_batches_activity ON impact_participation_batches(activity_id, occurred_on);
CREATE INDEX IF NOT EXISTS idx_impact_batches_occurred ON impact_participation_batches(occurred_on);
CREATE TABLE IF NOT EXISTS impact_observation_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_key TEXT NOT NULL UNIQUE,
    batch_id INTEGER NOT NULL REFERENCES impact_participation_batches(id) ON DELETE RESTRICT,
    observed_on TEXT NOT NULL,
    observers INTEGER NOT NULL DEFAULT 0 CHECK(observers >= 0),
    actions INTEGER NOT NULL DEFAULT 0 CHECK(actions >= 0),
    minutes INTEGER NOT NULL DEFAULT 0 CHECK(minutes >= 0),
    content_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_observations_batch ON impact_observation_records(batch_id);
CREATE TABLE IF NOT EXISTS impact_survey_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_key TEXT NOT NULL UNIQUE,
    batch_id INTEGER NOT NULL REFERENCES impact_participation_batches(id) ON DELETE RESTRICT,
    distributed INTEGER NOT NULL DEFAULT 0 CHECK(distributed >= 0),
    returned INTEGER NOT NULL DEFAULT 0 CHECK(returned >= 0),
    follow_up_intents INTEGER NOT NULL DEFAULT 0 CHECK(follow_up_intents >= 0),
    follow_up_actions INTEGER NOT NULL DEFAULT 0 CHECK(follow_up_actions >= 0),
    superseded_by INTEGER REFERENCES impact_survey_summaries(id),
    content_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_surveys_batch ON impact_survey_summaries(batch_id, superseded_by);
CREATE TABLE IF NOT EXISTS impact_metric_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    params_json TEXT NOT NULL,
    params_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(code, version)
);
CREATE TABLE IF NOT EXISTS impact_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('activity','period')),
    scope_key TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    definition_id INTEGER NOT NULL REFERENCES impact_metric_definitions(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','locked')),
    current_revision INTEGER NOT NULL DEFAULT 0,
    published_revision INTEGER,
    locked_at TEXT,
    locked_by TEXT,
    lock_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope_type, scope_key, definition_id)
);
CREATE TABLE IF NOT EXISTS impact_report_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES impact_reports(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    input_digest TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    computed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(report_id, revision)
);
CREATE TABLE IF NOT EXISTS impact_report_errata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES impact_reports(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    corrections_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_errata_report ON impact_report_errata(report_id, id);
"""


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class ImpactService:
    """活动影响指标的事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    # ---------- 活动 ----------

    def create_activity(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        ends_on = payload.get("ends_on") or payload["starts_on"]
        if ends_on < payload["starts_on"]:
            raise ValidationError("活动结束日期不能早于开始日期")
        content = {
            "code": payload["code"],
            "name": payload["name"],
            "kind": payload["kind"],
            "location": payload.get("location", ""),
            "starts_on": payload["starts_on"],
            "ends_on": ends_on,
        }
        digest = canonical_digest(content)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM impact_activities WHERE code=?", (content["code"],)).fetchone()
            if existing is not None:
                if existing["content_digest"] != digest:
                    raise ConflictError("同一活动编码对应了不同的活动内容")
                return dict(existing)
            cursor = connection.execute(
                "INSERT INTO impact_activities(code,name,kind,location,starts_on,ends_on,content_digest,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (content["code"], content["name"], content["kind"], content["location"], content["starts_on"], content["ends_on"], digest, actor, now, now),
            )
            return dict(connection.execute("SELECT * FROM impact_activities WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_activities(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM impact_activities ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    # ---------- 数据接收（幂等） ----------

    def upload_batch(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        content = {
            "activity_code": payload["activity_code"],
            "occurred_on": payload["occurred_on"],
            "participant_count": payload["participant_count"],
            "returning_count": payload["returning_count"],
            "channel": payload.get("channel", ""),
            "note": payload.get("note", ""),
        }
        digest = canonical_digest(content)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            activity = connection.execute("SELECT * FROM impact_activities WHERE code=?", (content["activity_code"],)).fetchone()
            if activity is None:
                raise NotFoundError("活动不存在", context={"activity_code": content["activity_code"]})
            existing = connection.execute("SELECT * FROM impact_participation_batches WHERE batch_key=?", (payload["batch_key"],)).fetchone()
            if existing is not None:
                if existing["content_digest"] != digest:
                    raise ConflictError("同一参与批次键对应了不同的内容")
                return dict(existing)
            cursor = connection.execute(
                "INSERT INTO impact_participation_batches(batch_key,activity_id,occurred_on,participant_count,returning_count,channel,note,content_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (payload["batch_key"], activity["id"], content["occurred_on"], content["participant_count"], content["returning_count"], content["channel"], content["note"], digest, actor, now),
            )
            return dict(connection.execute("SELECT * FROM impact_participation_batches WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_batches(self, activity_code: str | None = None) -> list[dict[str, Any]]:
        if activity_code:
            rows = self.connection.execute(
                "SELECT b.*, a.code AS activity_code FROM impact_participation_batches b JOIN impact_activities a ON a.id=b.activity_id WHERE a.code=? ORDER BY b.occurred_on, b.batch_key",
                (activity_code,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT b.*, a.code AS activity_code FROM impact_participation_batches b JOIN impact_activities a ON a.id=b.activity_id ORDER BY b.occurred_on, b.batch_key"
            ).fetchall()
        return [dict(row) for row in rows]

    def upload_observation(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        content = {
            "batch_key": payload["batch_key"],
            "observed_on": payload["observed_on"],
            "observers": payload["observers"],
            "actions": payload["actions"],
            "minutes": payload["minutes"],
        }
        digest = canonical_digest(content)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            batch = connection.execute("SELECT * FROM impact_participation_batches WHERE batch_key=?", (content["batch_key"],)).fetchone()
            if batch is None:
                raise NotFoundError("参与批次不存在", context={"batch_key": content["batch_key"]})
            existing = connection.execute("SELECT * FROM impact_observation_records WHERE record_key=?", (payload["record_key"],)).fetchone()
            if existing is not None:
                if existing["content_digest"] != digest:
                    raise ConflictError("同一观察记录键对应了不同的内容")
                return dict(existing)
            cursor = connection.execute(
                "INSERT INTO impact_observation_records(record_key,batch_id,observed_on,observers,actions,minutes,content_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (payload["record_key"], batch["id"], content["observed_on"], content["observers"], content["actions"], content["minutes"], digest, actor, now),
            )
            return dict(connection.execute("SELECT * FROM impact_observation_records WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_observations(self, batch_key: str | None = None) -> list[dict[str, Any]]:
        if batch_key:
            rows = self.connection.execute(
                "SELECT o.*, b.batch_key FROM impact_observation_records o JOIN impact_participation_batches b ON b.id=o.batch_id WHERE b.batch_key=? ORDER BY o.record_key",
                (batch_key,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT o.*, b.batch_key FROM impact_observation_records o JOIN impact_participation_batches b ON b.id=o.batch_id ORDER BY o.record_key"
            ).fetchall()
        return [dict(row) for row in rows]

    def upload_survey(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        content = {
            "batch_key": payload["batch_key"],
            "distributed": payload["distributed"],
            "returned": payload["returned"],
            "follow_up_intents": payload["follow_up_intents"],
            "follow_up_actions": payload["follow_up_actions"],
        }
        digest = canonical_digest(content)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            batch = connection.execute("SELECT * FROM impact_participation_batches WHERE batch_key=?", (content["batch_key"],)).fetchone()
            if batch is None:
                raise NotFoundError("参与批次不存在", context={"batch_key": content["batch_key"]})
            existing = connection.execute("SELECT * FROM impact_survey_summaries WHERE survey_key=?", (payload["survey_key"],)).fetchone()
            if existing is not None:
                if existing["content_digest"] != digest:
                    raise ConflictError("同一问卷摘要键对应了不同的内容")
                return dict(existing)
            cursor = connection.execute(
                "INSERT INTO impact_survey_summaries(survey_key,batch_id,distributed,returned,follow_up_intents,follow_up_actions,content_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (payload["survey_key"], batch["id"], content["distributed"], content["returned"], content["follow_up_intents"], content["follow_up_actions"], digest, actor, now),
            )
            new_id = cursor.lastrowid
            # 每个批次只保留一条有效问卷摘要：新摘要上线时作废旧摘要，历史仍可追溯
            previous = connection.execute(
                "SELECT * FROM impact_survey_summaries WHERE batch_id=? AND superseded_by IS NULL AND id<>?",
                (batch["id"], new_id),
            ).fetchone()
            if previous is not None:
                connection.execute("UPDATE impact_survey_summaries SET superseded_by=? WHERE id=?", (new_id, previous["id"]))
            return dict(connection.execute("SELECT * FROM impact_survey_summaries WHERE id=?", (new_id,)).fetchone())

    def list_surveys(self, batch_key: str | None = None) -> list[dict[str, Any]]:
        if batch_key:
            rows = self.connection.execute(
                "SELECT s.*, b.batch_key FROM impact_survey_summaries s JOIN impact_participation_batches b ON b.id=s.batch_id WHERE b.batch_key=? ORDER BY s.id",
                (batch_key,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT s.*, b.batch_key FROM impact_survey_summaries s JOIN impact_participation_batches b ON b.id=s.batch_id ORDER BY s.id"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 指标口径（公式版本） ----------

    def create_definition(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        params = normalize_params(payload.get("params"))
        digest = canonical_digest(params)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            latest = connection.execute(
                "SELECT * FROM impact_metric_definitions WHERE code=? ORDER BY version DESC LIMIT 1",
                (payload["code"],),
            ).fetchone()
            if latest is not None and latest["params_digest"] == digest:
                return self._definition_dict(latest)
            version = 1 if latest is None else int(latest["version"]) + 1
            cursor = connection.execute(
                "INSERT INTO impact_metric_definitions(code,version,name,params_json,params_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (payload["code"], version, payload["name"], json.dumps(params, ensure_ascii=False, sort_keys=True), digest, actor, now),
            )
            row = connection.execute("SELECT * FROM impact_metric_definitions WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._definition_dict(row)

    def list_definitions(self, code: str | None = None) -> list[dict[str, Any]]:
        if code:
            rows = self.connection.execute("SELECT * FROM impact_metric_definitions WHERE code=? ORDER BY version", (code,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM impact_metric_definitions ORDER BY code, version").fetchall()
        return [self._definition_dict(row) for row in rows]

    # ---------- 报告：计算、发布、锁定、勘误 ----------

    def compute_report(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            definition = self._definition(connection, payload["definition_code"], payload.get("definition_version"))
            scope_key, period_start, period_end, batches = self._resolve_scope(connection, payload)
            report = connection.execute(
                "SELECT * FROM impact_reports WHERE scope_type=? AND scope_key=? AND definition_id=?",
                (payload["scope_type"], scope_key, definition["id"]),
            ).fetchone()
            if report is not None and report["status"] == "locked":
                raise ConflictError("报告已锁定，只能追加勘误，不能重新计算")
            inputs, snapshot = self._snapshot(connection, definition, batches)
            if report is None:
                cursor = connection.execute(
                    "INSERT INTO impact_reports(scope_type,scope_key,period_start,period_end,definition_id,status,current_revision,created_by,created_at,updated_at) VALUES(?,?,?,?,?,'draft',0,?,?,?)",
                    (payload["scope_type"], scope_key, period_start, period_end, definition["id"], actor, now, now),
                )
                report_id = int(cursor.lastrowid)
            else:
                report_id = int(report["id"])
            revision = int(connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM impact_report_revisions WHERE report_id=?", (report_id,)).fetchone()[0])
            connection.execute(
                "INSERT INTO impact_report_revisions(report_id,revision,input_digest,inputs_json,metrics_json,coverage_json,computed_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    report_id,
                    revision,
                    inputs["digest"],
                    json.dumps(inputs["provenance"], ensure_ascii=False, sort_keys=True),
                    json.dumps(snapshot["metrics"], ensure_ascii=False, sort_keys=True),
                    json.dumps(snapshot["coverage"], ensure_ascii=False, sort_keys=True),
                    actor,
                    now,
                ),
            )
            connection.execute("UPDATE impact_reports SET current_revision=?,updated_at=? WHERE id=?", (revision, now, report_id))
            return self._report_detail(connection, report_id)

    def list_reports(
        self,
        *,
        scope_type: str | None = None,
        scope_key: str | None = None,
        definition_code: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if scope_type:
            clauses.append("r.scope_type=?")
            values.append(scope_type)
        if scope_key:
            clauses.append("r.scope_key=?")
            values.append(scope_key)
        if definition_code:
            clauses.append("d.code=?")
            values.append(definition_code)
        if status:
            clauses.append("r.status=?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT r.* FROM impact_reports r JOIN impact_metric_definitions d ON d.id=r.definition_id" + where + " ORDER BY r.id",
            values,
        ).fetchall()
        return [self._report_summary(self.connection, row) for row in rows]

    def get_report(self, report_id: int) -> dict[str, Any]:
        return self._report_detail(self.connection, report_id)

    def publish_report(self, report_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            report = self._report_row(connection, report_id)
            if report["status"] == "locked":
                raise ConflictError("报告已锁定，不能更改发布状态")
            connection.execute(
                "UPDATE impact_reports SET status='published',published_revision=current_revision,updated_at=? WHERE id=?",
                (now, report_id),
            )
            return self._report_detail(connection, report_id)

    def lock_report(self, report_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            report = self._report_row(connection, report_id)
            if report["status"] == "locked":
                raise ConflictError("报告已经处于锁定状态")
            if report["status"] != "published":
                raise ConflictError("只有已发布的报告可以锁定")
            connection.execute(
                "UPDATE impact_reports SET status='locked',locked_at=?,locked_by=?,lock_reason=?,updated_at=? WHERE id=?",
                (now, actor, reason, now, report_id),
            )
            return self._report_detail(connection, report_id)

    def append_erratum(self, report_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        corrections = payload.get("corrections") or {}
        unknown = set(corrections) - set(METRIC_FIELDS)
        if unknown:
            raise ValidationError("勘误包含未知指标字段", context={"fields": sorted(unknown)})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            report = self._report_row(connection, report_id)
            if report["status"] != "locked":
                raise ConflictError("只有锁定的报告才能追加勘误")
            cursor = connection.execute(
                "INSERT INTO impact_report_errata(report_id,actor,reason,corrections_json,note,created_at) VALUES(?,?,?,?,?,?)",
                (report_id, actor, payload["reason"], json.dumps(corrections, ensure_ascii=False, sort_keys=True), payload.get("note", ""), now),
            )
            row = connection.execute("SELECT * FROM impact_report_errata WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._erratum_dict(row)

    def list_errata(self, report_id: int) -> list[dict[str, Any]]:
        self._report_row(self.connection, report_id)
        rows = self.connection.execute("SELECT * FROM impact_report_errata WHERE report_id=? ORDER BY id", (report_id,)).fetchall()
        return [self._erratum_dict(row) for row in rows]

    def compare_reports(self, report_ids: list[int]) -> dict[str, Any]:
        """并排对比多份报告的来源、覆盖率、指标与勘误，用于复核不同口径或活动的结果。"""
        items: list[dict[str, Any]] = []
        for report_id in dict.fromkeys(report_ids):
            report = self._report_row(self.connection, report_id)
            definition = self.connection.execute("SELECT * FROM impact_metric_definitions WHERE id=?", (report["definition_id"],)).fetchone()
            published = report["published_revision"]
            revision_no = published if published else report["current_revision"]
            revision = self.connection.execute(
                "SELECT * FROM impact_report_revisions WHERE report_id=? AND revision=?",
                (report_id, revision_no),
            ).fetchone()
            errata_rows = self.connection.execute("SELECT * FROM impact_report_errata WHERE report_id=? ORDER BY id", (report_id,)).fetchall()
            errata = [self._erratum_dict(row) for row in errata_rows]
            metrics = json.loads(revision["metrics_json"])
            inputs = json.loads(revision["inputs_json"])
            items.append(
                {
                    "report_id": report["id"],
                    "scope_type": report["scope_type"],
                    "scope_key": report["scope_key"],
                    "period_start": report["period_start"],
                    "period_end": report["period_end"],
                    "definition": self._definition_dict(definition),
                    "status": report["status"],
                    "revision_used": revision_no,
                    "revision_basis": "published" if published else "current",
                    "input_digest": revision["input_digest"],
                    "sources": {
                        "batches": [item["batch_key"] for item in inputs["batches"]],
                        "observation_records": [item["record_key"] for item in inputs["observation_records"]],
                        "survey_summaries": [item["survey_key"] for item in inputs["survey_summaries"]],
                    },
                    "metrics": metrics,
                    "coverage": json.loads(revision["coverage_json"]),
                    "errata": errata,
                    "corrected_metrics": self._corrected_metrics(metrics, errata),
                }
            )
        return {"items": items}

    # ---------- 内部辅助 ----------

    def _definition(self, connection: sqlite3.Connection, code: str, version: int | None) -> sqlite3.Row:
        if version is None:
            row = connection.execute(
                "SELECT * FROM impact_metric_definitions WHERE code=? ORDER BY version DESC LIMIT 1",
                (code,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM impact_metric_definitions WHERE code=? AND version=?",
                (code, version),
            ).fetchone()
        if row is None:
            raise NotFoundError("指标口径不存在", context={"code": code, "version": version})
        return row

    def _resolve_scope(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> tuple[str, str | None, str | None, list[sqlite3.Row]]:
        if payload["scope_type"] == "activity":
            code = payload["activity_code"]
            activity = connection.execute("SELECT * FROM impact_activities WHERE code=?", (code,)).fetchone()
            if activity is None:
                raise NotFoundError("活动不存在", context={"activity_code": code})
            batches = connection.execute(
                "SELECT b.*, a.code AS activity_code FROM impact_participation_batches b JOIN impact_activities a ON a.id=b.activity_id WHERE a.code=? ORDER BY b.occurred_on, b.batch_key",
                (code,),
            ).fetchall()
            return code, None, None, batches
        start, end = payload["period_start"], payload["period_end"]
        if end < start:
            raise ValidationError("统计结束日期不能早于开始日期")
        scope_key = f"{start}~{end}"
        batches = connection.execute(
            "SELECT b.*, a.code AS activity_code FROM impact_participation_batches b JOIN impact_activities a ON a.id=b.activity_id WHERE b.occurred_on BETWEEN ? AND ? ORDER BY b.occurred_on, b.batch_key",
            (start, end),
        ).fetchall()
        return scope_key, start, end, batches

    def _snapshot(self, connection: sqlite3.Connection, definition: sqlite3.Row, batches: list[sqlite3.Row]) -> tuple[dict[str, Any], dict[str, Any]]:
        batch_ids = [int(batch["id"]) for batch in batches]
        observations: list[sqlite3.Row] = []
        surveys: list[sqlite3.Row] = []
        if batch_ids:
            placeholders = ",".join("?" for _ in batch_ids)
            observations = connection.execute(
                f"SELECT o.*, b.batch_key FROM impact_observation_records o JOIN impact_participation_batches b ON b.id=o.batch_id WHERE o.batch_id IN ({placeholders}) ORDER BY o.record_key",
                batch_ids,
            ).fetchall()
            surveys = connection.execute(
                f"SELECT s.*, b.batch_key FROM impact_survey_summaries s JOIN impact_participation_batches b ON b.id=s.batch_id WHERE s.batch_id IN ({placeholders}) AND s.superseded_by IS NULL ORDER BY s.survey_key",
                batch_ids,
            ).fetchall()
        params = json.loads(definition["params_json"])
        snapshot = compute_snapshot(
            [dict(batch) for batch in batches],
            [dict(item) for item in observations],
            [dict(item) for item in surveys],
            params,
        )
        provenance = {
            "definition": {"code": definition["code"], "version": definition["version"], "params_digest": definition["params_digest"]},
            "batches": [
                {
                    "batch_key": batch["batch_key"],
                    "activity_code": batch["activity_code"],
                    "occurred_on": batch["occurred_on"],
                    "participant_count": batch["participant_count"],
                    "returning_count": batch["returning_count"],
                }
                for batch in batches
            ],
            "observation_records": [
                {
                    "record_key": item["record_key"],
                    "batch_key": item["batch_key"],
                    "observers": item["observers"],
                    "actions": item["actions"],
                    "minutes": item["minutes"],
                }
                for item in observations
            ],
            "survey_summaries": [
                {
                    "survey_key": item["survey_key"],
                    "batch_key": item["batch_key"],
                    "distributed": item["distributed"],
                    "returned": item["returned"],
                    "follow_up_intents": item["follow_up_intents"],
                    "follow_up_actions": item["follow_up_actions"],
                }
                for item in surveys
            ],
        }
        digest = canonical_digest({"provenance": provenance, "params": params})
        return {"digest": digest, "provenance": provenance}, snapshot

    def _report_row(self, connection: sqlite3.Connection, report_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM impact_reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise NotFoundError("影响指标报告不存在", context={"report_id": report_id})
        return row

    def _report_summary(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        report = dict(row)
        definition = connection.execute("SELECT * FROM impact_metric_definitions WHERE id=?", (report["definition_id"],)).fetchone()
        report["definition"] = self._definition_dict(definition)
        errata_count = connection.execute("SELECT COUNT(*) FROM impact_report_errata WHERE report_id=?", (report["id"],)).fetchone()[0]
        report["errata_count"] = int(errata_count)
        return report

    def _report_detail(self, connection: sqlite3.Connection, report_id: int) -> dict[str, Any]:
        report = self._report_summary(connection, self._report_row(connection, report_id))
        revisions = connection.execute("SELECT * FROM impact_report_revisions WHERE report_id=? ORDER BY revision", (report_id,)).fetchall()
        report["revisions"] = [self._revision_dict(row) for row in revisions]
        errata_rows = connection.execute("SELECT * FROM impact_report_errata WHERE report_id=? ORDER BY id", (report_id,)).fetchall()
        report["errata"] = [self._erratum_dict(row) for row in errata_rows]
        published = report["published_revision"] or report["current_revision"]
        current = next((item for item in report["revisions"] if item["revision"] == published), None)
        report["corrected_metrics"] = self._corrected_metrics(current["metrics"], report["errata"]) if current else None
        return report

    @staticmethod
    def _corrected_metrics(metrics: dict[str, Any], errata: list[dict[str, Any]]) -> dict[str, Any]:
        corrected = dict(metrics)
        for erratum in errata:
            corrected.update(erratum["corrections"])
        return corrected

    @staticmethod
    def _definition_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["params"] = json.loads(value.pop("params_json"))
        return value

    @staticmethod
    def _revision_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["inputs"] = json.loads(value.pop("inputs_json"))
        value["metrics"] = json.loads(value.pop("metrics_json"))
        value["coverage"] = json.loads(value.pop("coverage_json"))
        return value

    @staticmethod
    def _erratum_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["corrections"] = json.loads(value.pop("corrections_json"))
        return value
