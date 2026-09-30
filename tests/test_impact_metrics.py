from __future__ import annotations

import pytest

ACTOR = "impact-tester"


def make_activity(client, code: str, *, kind: str = "导赏", starts_on: str = "2026-08-02") -> dict:
    response = client.post(
        "/api/impact/activities",
        json={"code": code, "name": f"活动-{code}", "kind": kind, "location": "湿地公园", "starts_on": starts_on, "actor": ACTOR},
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload_batch(client, batch_key: str, activity_code: str, occurred_on: str, participants: int, returning: int = 0) -> dict:
    response = client.post(
        "/api/impact/participation-batches",
        json={
            "batch_key": batch_key,
            "activity_code": activity_code,
            "occurred_on": occurred_on,
            "participant_count": participants,
            "returning_count": returning,
            "channel": "现场报名",
            "actor": ACTOR,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload_observation(client, record_key: str, batch_key: str, *, actions: int, minutes: int = 0, observers: int = 0) -> dict:
    response = client.post(
        "/api/impact/observation-records",
        json={
            "record_key": record_key,
            "batch_key": batch_key,
            "observed_on": "2026-08-02",
            "observers": observers,
            "actions": actions,
            "minutes": minutes,
            "actor": ACTOR,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload_survey(client, survey_key: str, batch_key: str, *, distributed: int, returned: int, intents: int, actions: int = 0) -> dict:
    response = client.post(
        "/api/impact/survey-summaries",
        json={
            "survey_key": survey_key,
            "batch_key": batch_key,
            "distributed": distributed,
            "returned": returned,
            "follow_up_intents": intents,
            "follow_up_actions": actions,
            "actor": ACTOR,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def define_metric(client, params: dict | None = None, *, code: str = "impact-core", name: str = "参与深度与后续行动") -> dict:
    response = client.post("/api/impact/definitions", json={"code": code, "name": name, "params": params or {}, "actor": ACTOR})
    assert response.status_code == 201, response.text
    return response.json()


def compute(client, payload: dict) -> dict:
    response = client.post("/api/impact/reports/compute", json={"actor": ACTOR, **payload})
    assert response.status_code == 201, response.text
    return response.json()


def latest_revision(report: dict) -> dict:
    return report["revisions"][-1]


def test_batch_reupload_is_idempotent_and_conflict_detected(client):
    make_activity(client, "reed-tour")
    first = upload_batch(client, "batch-001", "reed-tour", "2026-08-02", 20, 3)
    replay = upload_batch(client, "batch-001", "reed-tour", "2026-08-02", 20, 3)
    assert replay["id"] == first["id"]

    conflict = client.post(
        "/api/impact/participation-batches",
        json={
            "batch_key": "batch-001",
            "activity_code": "reed-tour",
            "occurred_on": "2026-08-02",
            "participant_count": 25,
            "returning_count": 3,
            "actor": ACTOR,
        },
    )
    assert conflict.status_code == 409

    define_metric(client)
    upload_observation(client, "obs-001", "batch-001", actions=40)
    upload_survey(client, "survey-001", "batch-001", distributed=20, returned=18, intents=9)
    report = compute(client, {"scope_type": "activity", "activity_code": "reed-tour", "definition_code": "impact-core"})
    metrics = latest_revision(report)["metrics"]
    # 重复上传只计入一次：总人数 20 而不是 40
    assert metrics["participants_total"] == 20
    assert metrics["returning_participants"] == 3


def test_missing_survey_marks_insufficient_coverage_instead_of_zero(client):
    make_activity(client, "mudflat-watch", kind="自然观察", starts_on="2026-08-16")
    upload_batch(client, "batch-a", "mudflat-watch", "2026-08-16", 30, 6)
    upload_batch(client, "batch-b", "mudflat-watch", "2026-08-16", 30, 0)
    upload_observation(client, "obs-a", "batch-a", actions=60, minutes=120, observers=30)
    upload_survey(client, "survey-a", "batch-a", distributed=30, returned=24, intents=12, actions=5)
    define_metric(client)

    report = compute(client, {"scope_type": "activity", "activity_code": "mudflat-watch", "definition_code": "impact-core"})
    revision = latest_revision(report)
    metrics, coverage = revision["metrics"], revision["coverage"]
    assert coverage["status"] == "insufficient"
    assert coverage["missing_survey_batches"] == ["batch-b"]
    assert coverage["coverage_ratio"] == 0.5
    # 缺失问卷不按零处理：后续行动率留空并说明原因，而不是 0.0
    assert metrics["follow_up_rate"] is None
    assert metrics["questionnaires_returned"] == 24
    assert any("缺少问卷" in reason for reason in coverage["reasons"])

    # 补上第二个批次的问卷后重新计算，覆盖率达标，行动率正常给出
    upload_survey(client, "survey-b", "batch-b", distributed=30, returned=30, intents=6)
    recomputed = compute(client, {"scope_type": "activity", "activity_code": "mudflat-watch", "definition_code": "impact-core"})
    new_revision = latest_revision(recomputed)
    assert new_revision["revision"] == 2
    assert new_revision["coverage"]["status"] == "sufficient"
    assert new_revision["metrics"]["follow_up_rate"] == pytest.approx(18 / 54, abs=1e-4)
    # 旧修订仍然保留，可复核历史
    assert len(recomputed["revisions"]) == 2
    assert recomputed["revisions"][0]["coverage"]["status"] == "insufficient"


def test_definition_update_creates_new_version_and_keeps_old_results(client):
    make_activity(client, "reed-tour")
    upload_batch(client, "batch-001", "reed-tour", "2026-08-02", 20, 4)
    upload_observation(client, "obs-001", "batch-001", actions=40, minutes=200)
    upload_survey(client, "survey-001", "batch-001", distributed=20, returned=20, intents=10)

    v1 = define_metric(client, {"depth_weights": {"action": 1.0, "minute": 0.0, "returning": 0.0}})
    assert v1["version"] == 1
    same = define_metric(client, {"depth_weights": {"action": 1.0, "minute": 0.0, "returning": 0.0}})
    # 参数相同不生成新版本，幂等返回已有版本
    assert same["version"] == 1 and same["id"] == v1["id"]

    report_v1 = compute(client, {"scope_type": "activity", "activity_code": "reed-tour", "definition_code": "impact-core"})
    depth_v1 = latest_revision(report_v1)["metrics"]["participation_depth"]
    assert depth_v1 == pytest.approx(40 / 20, abs=1e-4)

    v2 = define_metric(client, {"depth_weights": {"action": 1.0, "minute": 0.1, "returning": 0.5}})
    assert v2["version"] == 2
    report_v2 = compute(client, {"scope_type": "activity", "activity_code": "reed-tour", "definition_code": "impact-core"})
    depth_v2 = latest_revision(report_v2)["metrics"]["participation_depth"]
    assert depth_v2 == pytest.approx((40 + 0.1 * 200 + 0.5 * 4) / 20, abs=1e-4)

    # 两个版本各自独立成报告，旧版本结果保持不变
    assert report_v1["definition"]["version"] == 1
    assert report_v2["definition"]["version"] == 2
    assert report_v1["id"] != report_v2["id"]
    reread = client.get(f"/api/impact/reports/{report_v1['id']}").json()
    assert latest_revision(reread)["metrics"]["participation_depth"] == depth_v1
    versions = client.get("/api/impact/definitions?code=impact-core").json()["items"]
    assert [item["version"] for item in versions] == [1, 2]


def test_publish_lock_then_only_errata_allowed(client):
    make_activity(client, "reed-tour")
    upload_batch(client, "batch-001", "reed-tour", "2026-08-02", 20, 0)
    upload_survey(client, "survey-001", "batch-001", distributed=20, returned=20, intents=10)
    define_metric(client)
    report = compute(client, {"scope_type": "activity", "activity_code": "reed-tour", "definition_code": "impact-core"})
    report_id = report["id"]

    # 未发布不能锁定
    early_lock = client.post(f"/api/impact/reports/{report_id}/lock", json={"actor": "manager", "reason": "提前锁定"})
    assert early_lock.status_code == 409

    published = client.post(f"/api/impact/reports/{report_id}/publish", json={"actor": "manager"})
    assert published.status_code == 200 and published.json()["status"] == "published"
    assert published.json()["published_revision"] == 1

    locked = client.post(f"/api/impact/reports/{report_id}/lock", json={"actor": "manager", "reason": "季度发布会定稿"})
    assert locked.status_code == 200 and locked.json()["status"] == "locked"
    assert locked.json()["locked_by"] == "manager"

    # 锁定后不能静默重算，也不能重新发布
    recompute = client.post(
        "/api/impact/reports/compute",
        json={"actor": ACTOR, "scope_type": "activity", "activity_code": "reed-tour", "definition_code": "impact-core"},
    )
    assert recompute.status_code == 409
    republish = client.post(f"/api/impact/reports/{report_id}/publish", json={"actor": "manager"})
    assert republish.status_code == 409

    # 锁定前不能追加勘误的校验在另一报告上体现；锁定后勘误可追加且只增不改
    erratum = client.post(
        f"/api/impact/reports/{report_id}/errata",
        json={"actor": "manager", "reason": "复核发现两份问卷录入遗漏", "corrections": {"questionnaires_returned": 22, "follow_up_rate": 0.5}, "note": "以纸质问卷复核为准"},
    )
    assert erratum.status_code == 201, erratum.text
    detail = client.get(f"/api/impact/reports/{report_id}").json()
    assert len(detail["errata"]) == 1
    assert detail["errata"][0]["corrections"]["questionnaires_returned"] == 22
    # 原始发布数字不被修改，更正值单独呈现
    assert latest_revision(detail)["metrics"]["questionnaires_returned"] == 20
    assert detail["corrected_metrics"]["questionnaires_returned"] == 22
    assert detail["corrected_metrics"]["follow_up_rate"] == 0.5

    unknown_field = client.post(
        f"/api/impact/reports/{report_id}/errata",
        json={"actor": "manager", "reason": "字段名写错", "corrections": {"not_a_metric": 1}},
    )
    assert unknown_field.status_code == 422


def test_erratum_rejected_before_lock(client):
    make_activity(client, "reed-tour")
    upload_batch(client, "batch-001", "reed-tour", "2026-08-02", 10, 0)
    define_metric(client)
    report = compute(client, {"scope_type": "activity", "activity_code": "reed-tour", "definition_code": "impact-core"})
    response = client.post(
        f"/api/impact/reports/{report['id']}/errata",
        json={"actor": "manager", "reason": "尚未锁定", "note": "应被拒绝"},
    )
    assert response.status_code == 409


def test_out_of_order_import_and_compare_across_versions(client):
    make_activity(client, "reed-tour", kind="导赏", starts_on="2026-08-02")
    make_activity(client, "mudflat-watch", kind="自然观察", starts_on="2026-08-16")
    # 乱序导入：晚发生的批次先上传
    upload_batch(client, "batch-late", "mudflat-watch", "2026-08-16", 30, 6)
    upload_batch(client, "batch-early-2", "reed-tour", "2026-08-02", 20, 2)
    upload_batch(client, "batch-early-1", "reed-tour", "2026-08-01", 25, 5)
    upload_observation(client, "obs-late", "batch-late", actions=90, minutes=300, observers=30)
    upload_observation(client, "obs-early-2", "batch-early-2", actions=30, minutes=100, observers=20)
    upload_observation(client, "obs-early-1", "batch-early-1", actions=50, minutes=150, observers=25)
    upload_survey(client, "survey-late", "batch-late", distributed=30, returned=27, intents=9, actions=3)
    upload_survey(client, "survey-early-2", "batch-early-2", distributed=20, returned=16, intents=8)
    upload_survey(client, "survey-early-1", "batch-early-1", distributed=25, returned=25, intents=15, actions=6)

    define_metric(client, {"depth_weights": {"action": 1.0, "minute": 0.0, "returning": 0.0}})
    period = compute(
        client,
        {"scope_type": "period", "period_start": "2026-08-01", "period_end": "2026-08-31", "definition_code": "impact-core"},
    )
    period_metrics = latest_revision(period)["metrics"]
    # 乱序不影响归集：总人数 75，观察行为 170 次
    assert period_metrics["participants_total"] == 75
    assert period_metrics["observation_actions"] == 170
    assert period_metrics["follow_up_rate"] == pytest.approx(32 / 68, abs=1e-4)
    sources = latest_revision(period)["inputs"]["batches"]
    assert [item["batch_key"] for item in sources] == ["batch-early-1", "batch-early-2", "batch-late"]

    # 切换口径版本后分别计算，再并排对比来源、覆盖率与勘误
    define_metric(client, {"depth_weights": {"action": 1.0, "minute": 0.1, "returning": 0.0}})
    period_v2 = compute(
        client,
        {"scope_type": "period", "period_start": "2026-08-01", "period_end": "2026-08-31", "definition_code": "impact-core"},
    )
    assert period_v2["definition"]["version"] == 2
    assert latest_revision(period_v2)["metrics"]["participation_depth"] != period_metrics["participation_depth"]

    client.post(f"/api/impact/reports/{period_v2['id']}/publish", json={"actor": "manager"})
    client.post(f"/api/impact/reports/{period_v2['id']}/lock", json={"actor": "manager", "reason": "月报定稿"})
    client.post(
        f"/api/impact/reports/{period_v2['id']}/errata",
        json={"actor": "manager", "reason": "一批次人数复核修正", "corrections": {"participants_total": 76}},
    )

    compared = client.get(f"/api/impact/reports/compare?report_ids={period['id']},{period_v2['id']}")
    assert compared.status_code == 200, compared.text
    items = compared.json()["items"]
    assert [item["definition"]["version"] for item in items] == [1, 2]
    first, second = items
    assert first["sources"]["batches"] == ["batch-early-1", "batch-early-2", "batch-late"]
    assert first["coverage"]["status"] == "sufficient"
    assert first["errata"] == []
    assert second["errata"][0]["corrections"] == {"participants_total": 76}
    assert second["corrected_metrics"]["participants_total"] == 76
    assert second["metrics"]["participants_total"] == 75
    assert first["input_digest"] != second["input_digest"] or first["definition"]["params"] != second["definition"]["params"]


def test_survey_supersede_correction_flow(client):
    make_activity(client, "reed-tour")
    upload_batch(client, "batch-001", "reed-tour", "2026-08-02", 20, 0)
    upload_survey(client, "survey-v1", "batch-001", distributed=20, returned=10, intents=2)
    define_metric(client)
    first = compute(client, {"scope_type": "activity", "activity_code": "reed-tour", "definition_code": "impact-core"})
    assert latest_revision(first)["metrics"]["questionnaires_returned"] == 10

    # 上传更正后的问卷摘要：旧摘要作废，新摘要生效，历史仍可查
    upload_survey(client, "survey-v2", "batch-001", distributed=20, returned=18, intents=9)
    surveys = client.get("/api/impact/survey-summaries?batch_key=batch-001").json()["items"]
    assert len(surveys) == 2
    superseded = {item["survey_key"]: item["superseded_by"] for item in surveys}
    assert superseded["survey-v1"] is not None
    assert superseded["survey-v2"] is None

    recomputed = compute(client, {"scope_type": "activity", "activity_code": "reed-tour", "definition_code": "impact-core"})
    assert latest_revision(recomputed)["metrics"]["questionnaires_returned"] == 18
    assert latest_revision(recomputed)["metrics"]["follow_up_rate"] == pytest.approx(9 / 18, abs=1e-4)


def test_validation_and_not_found(client):
    make_activity(client, "reed-tour")
    bad_batch = client.post(
        "/api/impact/participation-batches",
        json={"batch_key": "batch-x", "activity_code": "reed-tour", "occurred_on": "2026-08-02", "participant_count": 10, "returning_count": 11, "actor": ACTOR},
    )
    assert bad_batch.status_code == 422

    missing_activity = client.post(
        "/api/impact/participation-batches",
        json={"batch_key": "batch-y", "activity_code": "ghost", "occurred_on": "2026-08-02", "participant_count": 10, "actor": ACTOR},
    )
    assert missing_activity.status_code == 404

    upload_batch(client, "batch-001", "reed-tour", "2026-08-02", 20, 0)
    bad_survey = client.post(
        "/api/impact/survey-summaries",
        json={"survey_key": "survey-x", "batch_key": "batch-001", "distributed": 10, "returned": 12, "follow_up_intents": 1, "actor": ACTOR},
    )
    assert bad_survey.status_code == 422

    bad_params = client.post(
        "/api/impact/definitions",
        json={"code": "impact-core", "name": "非法参数", "params": {"unknown_switch": True}, "actor": ACTOR},
    )
    assert bad_params.status_code == 422

    missing_definition = client.post(
        "/api/impact/reports/compute",
        json={"actor": ACTOR, "scope_type": "activity", "activity_code": "reed-tour", "definition_code": "ghost"},
    )
    assert missing_definition.status_code == 404

    bad_scope = client.post(
        "/api/impact/reports/compute",
        json={"actor": ACTOR, "scope_type": "period", "period_start": "2026-08-31", "period_end": "2026-08-01", "definition_code": "impact-core"},
    )
    assert bad_scope.status_code == 422

    missing_report = client.get("/api/impact/reports/9999")
    assert missing_report.status_code == 404
