from __future__ import annotations

ACTIVITY = {
    "activity_code": "ACT-2026-01",
    "title": "湿地导赏第一期",
    "category": "导赏",
    "location": "城东湿地",
    "starts_at": "2026-05-01T09:00:00+00:00",
    "ends_at": "2026-05-01T12:00:00+00:00",
    "created_by": "ops",
}

FORMULA_V1 = {
    "code": "impact-core",
    "name": "参与深度与后续行动口径",
    "definition": {
        "depth": {"observation_weight": 1.0, "returning_weight": 2.0},
        "follow_up": {"action_types": ["复访", "志愿报名"], "window_days": 90, "basis": "survey"},
        "coverage": {"survey_min_rate": 0.6},
    },
    "created_by": "researcher",
}


def create_activity(client, **overrides):
    payload = {**ACTIVITY, **overrides}
    response = client.post("/api/impact/activities", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def create_formula(client, **overrides):
    payload = {**FORMULA_V1, **overrides}
    response = client.post("/api/impact/formulas", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def batch_payload(code, count, new, returning, occurred_on="2026-05-01"):
    return {
        "batch_code": code,
        "activity_code": ACTIVITY["activity_code"],
        "participant_count": count,
        "new_participant_count": new,
        "returning_participant_count": returning,
        "occurred_on": occurred_on,
        "source": "签到表",
    }


def survey_payload(code, distributed, returned, care_avg, commits):
    return {
        "survey_code": code,
        "activity_code": ACTIVITY["activity_code"],
        "distributed_count": distributed,
        "returned_count": returned,
        "care_score_avg": care_avg,
        "action_commit_count": commits,
        "collected_at": "2026-05-08T10:00:00+00:00",
    }


def compute(client, version=1, activity_code=ACTIVITY["activity_code"]):
    response = client.post(
        "/api/impact/reports/compute",
        json={"activity_code": activity_code, "formula_code": "impact-core", "formula_version": version, "computed_by": "analyst"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_activity_and_formula_versioning(client):
    create_activity(client)
    duplicate = client.post("/api/impact/activities", json=ACTIVITY)
    assert duplicate.status_code == 409

    v1 = create_formula(client)
    assert v1["version"] == 1
    again = client.post("/api/impact/formulas", json=FORMULA_V1)
    assert again.status_code == 409

    new_definition = {
        "depth": {"observation_weight": 1.5, "returning_weight": 3.0},
        "follow_up": {"action_types": ["复访"], "window_days": 60, "basis": "survey"},
        "coverage": {"survey_min_rate": 0.5},
    }
    v2 = client.post("/api/impact/formulas/impact-core/versions", json={"definition": new_definition, "created_by": "researcher"})
    assert v2.status_code == 201, v2.text
    assert v2.json()["version"] == 2
    assert v2.json()["name"] == FORMULA_V1["name"]

    versions = client.get("/api/impact/formulas?code=impact-core").json()["items"]
    assert [item["version"] for item in versions] == [1, 2]
    assert versions[0]["definition"]["depth"]["observation_weight"] == 1.0
    assert versions[1]["definition"]["depth"]["returning_weight"] == 3.0

    missing = client.post("/api/impact/formulas/no-such/versions", json={"definition": new_definition})
    assert missing.status_code == 404


def test_batch_upload_is_idempotent_and_conflict_on_change(client):
    create_activity(client)
    payload = batch_payload("B-001", 20, 14, 6)
    first = client.post("/api/impact/participation-batches", json=payload)
    assert first.status_code == 201, first.text
    assert first.json()["deduplicated"] is False
    second = client.post("/api/impact/participation-batches", json=payload)
    assert second.status_code == 201
    assert second.json()["deduplicated"] is True
    assert second.json()["id"] == first.json()["id"]

    changed = client.post("/api/impact/participation-batches", json={**payload, "participant_count": 21})
    assert changed.status_code == 409

    invalid = client.post("/api/impact/participation-batches", json=batch_payload("B-002", 10, 8, 5))
    assert invalid.status_code == 422

    unknown = client.post("/api/impact/participation-batches", json={**payload, "batch_code": "B-003", "activity_code": "NOPE"})
    assert unknown.status_code == 404


def test_batch_code_allows_chinese_labels(client):
    create_activity(client)
    payload = batch_payload("五一导赏-批次1", 12, 9, 3)
    first = client.post("/api/impact/participation-batches", json=payload)
    assert first.status_code == 201, first.text
    second = client.post("/api/impact/participation-batches", json=payload)
    assert second.status_code == 201
    assert second.json()["deduplicated"] is True


def test_out_of_order_import_and_repeat_participation(client):
    create_activity(client)
    create_formula(client)
    # 乱序导入：较晚的批次先到，较早的批次后到
    late = client.post("/api/impact/participation-batches", json=batch_payload("B-PM", 30, 20, 10, "2026-05-01"))
    early = client.post("/api/impact/participation-batches", json=batch_payload("B-AM", 10, 10, 0, "2026-04-30"))
    assert late.status_code == early.status_code == 201
    # 同一批次重复上传不能重复计入
    client.post("/api/impact/participation-batches", json=batch_payload("B-PM", 30, 20, 10, "2026-05-01"))

    for code, qty, behavior, observed in [
        ("E-1", 5, "物种记录", "2026-05-01T10:00:00+00:00"),
        ("E-2", 3, "物种记录", "2026-05-01T11:00:00+00:00"),
    ]:
        response = client.post("/api/impact/observation-events", json={
            "event_code": code, "activity_code": ACTIVITY["activity_code"],
            "behavior_type": behavior, "quantity": qty, "observed_at": observed,
        })
        assert response.status_code == 201, response.text

    report = compute(client)
    metrics = report["metrics"]
    assert metrics["participants_total"] == 40
    assert metrics["participants_returning"] == 10
    assert metrics["repeat_participation_rate"] == 0.25
    assert metrics["observation_quantity"] == 8
    # depth = (1.0 * 8 + 2.0 * 10) / 40 = 0.7
    assert metrics["depth_score"] == 0.7
    assert report["sources"]["batches"] == ["B-AM", "B-PM"]

    # 输入未变化时重算返回同一份报告
    reused = compute(client)
    assert reused["reused"] is True
    assert reused["id"] == report["id"]


def test_missing_survey_flags_insufficient_coverage_not_zero(client):
    create_activity(client)
    create_formula(client)
    client.post("/api/impact/participation-batches", json=batch_payload("B-001", 40, 30, 10))

    report = compute(client)
    coverage = report["coverage"]
    follow_up = report["metrics"]["follow_up"]
    assert coverage["coverage_status"] == "missing"
    assert coverage["coverage_rate"] is None
    assert follow_up["rate"] is None
    assert follow_up["insufficient_coverage"] is True
    assert report["metrics"]["survey"]["action_commit_count"] is None


def test_low_survey_coverage_marks_insufficient(client):
    create_activity(client)
    create_formula(client)
    client.post("/api/impact/participation-batches", json=batch_payload("B-001", 100, 80, 20))
    response = client.post("/api/impact/survey-summaries", json=survey_payload("S-1", 60, 30, 4.2, 12))
    assert response.status_code == 201, response.text

    report = compute(client)
    assert report["coverage"]["coverage_status"] == "insufficient"
    assert report["coverage"]["coverage_rate"] == 0.3
    assert report["metrics"]["follow_up"]["rate"] is None
    assert report["metrics"]["follow_up"]["insufficient_coverage"] is True


def test_sufficient_coverage_computes_follow_up_rate(client):
    create_activity(client)
    create_formula(client)
    client.post("/api/impact/participation-batches", json=batch_payload("B-001", 50, 40, 10))
    client.post("/api/impact/survey-summaries", json=survey_payload("S-1", 40, 32, 4.5, 16))
    client.post("/api/impact/survey-summaries", json=survey_payload("S-2", 10, 8, 3.5, 4))

    report = compute(client)
    coverage = report["coverage"]
    assert coverage["coverage_status"] == "sufficient"
    assert coverage["coverage_rate"] == 0.8
    metrics = report["metrics"]
    # follow_up = (16 + 4) / (32 + 8) = 0.5
    assert metrics["follow_up"]["rate"] == 0.5
    assert metrics["follow_up"]["insufficient_coverage"] is False
    # care_score_avg 按回收数加权 = (4.5*32 + 3.5*8) / 40 = 4.3
    assert metrics["survey"]["care_score_avg"] == 4.3


def test_survey_validation(client):
    create_activity(client)
    bad_return = client.post("/api/impact/survey-summaries", json=survey_payload("S-1", 10, 20, 4.0, 5))
    assert bad_return.status_code == 422
    bad_commit = client.post("/api/impact/survey-summaries", json=survey_payload("S-1", 10, 8, 4.0, 9))
    assert bad_commit.status_code == 422


def test_formula_switch_keeps_old_results_and_compare(client):
    create_activity(client)
    create_formula(client)
    client.post("/api/impact/participation-batches", json=batch_payload("B-001", 40, 30, 10))
    client.post("/api/impact/observation-events", json={
        "event_code": "E-1", "activity_code": ACTIVITY["activity_code"],
        "behavior_type": "物种记录", "quantity": 8, "observed_at": "2026-05-01T10:00:00+00:00",
    })
    client.post("/api/impact/survey-summaries", json=survey_payload("S-1", 40, 30, 4.0, 15))

    report_v1 = compute(client, version=1)
    assert report_v1["metrics"]["depth_score"] == 0.7

    new_definition = {
        "depth": {"observation_weight": 2.0, "returning_weight": 2.0},
        "follow_up": {"action_types": ["复访"], "window_days": 90, "basis": "survey"},
        "coverage": {"survey_min_rate": 0.6},
    }
    client.post("/api/impact/formulas/impact-core/versions", json={"definition": new_definition})
    report_v2 = compute(client, version=2)
    # depth = (2.0 * 8 + 2.0 * 10) / 40 = 0.9
    assert report_v2["metrics"]["depth_score"] == 0.9

    # 旧口径结果仍然保留且可复核
    kept = client.get(f"/api/impact/reports/{report_v1['id']}").json()
    assert kept["metrics"]["depth_score"] == 0.7
    assert kept["formula"]["version"] == 1

    compare = client.get(f"/api/impact/activities/{ACTIVITY['activity_code']}/compare").json()
    assert compare["activity_code"] == ACTIVITY["activity_code"]
    assert len(compare["reports"]) == 2
    by_version = {item["formula"]["version"]: item for item in compare["reports"]}
    assert by_version[1]["headline"]["depth_score"] == 0.7
    assert by_version[2]["headline"]["depth_score"] == 0.9
    for item in compare["reports"]:
        assert item["sources"]["counts"] == {"batches": 1, "observation_events": 1, "survey_summaries": 1}
        assert item["coverage_status"] == "sufficient"
        assert item["errata_count"] == 0

    listed = client.get("/api/impact/reports", params={"activity_code": ACTIVITY["activity_code"], "formula_version": 2}).json()["items"]
    assert [item["id"] for item in listed] == [report_v2["id"]]


def test_new_data_creates_new_report_version(client):
    create_activity(client)
    create_formula(client)
    client.post("/api/impact/participation-batches", json=batch_payload("B-001", 40, 30, 10))
    first = compute(client)
    assert first["report_seq"] == 1

    client.post("/api/impact/participation-batches", json=batch_payload("B-002", 10, 10, 0, "2026-05-02"))
    second = compute(client)
    assert second["reused"] is False
    assert second["report_seq"] == 2
    assert second["id"] != first["id"]
    assert second["metrics"]["participants_total"] == 50
    assert second["input_digest"] != first["input_digest"]

    kept = client.get(f"/api/impact/reports/{first['id']}").json()
    assert kept["metrics"]["participants_total"] == 40


def test_lock_blocks_recompute_and_allows_errata(client):
    create_activity(client)
    create_formula(client)
    client.post("/api/impact/participation-batches", json=batch_payload("B-001", 40, 30, 10))
    client.post("/api/impact/survey-summaries", json=survey_payload("S-1", 40, 30, 4.0, 15))
    report = compute(client)

    # 未发布不能锁定，未锁定不能勘误
    premature_lock = client.post(f"/api/impact/reports/{report['id']}/lock", json={"actor": "manager"})
    assert premature_lock.status_code == 409
    premature_errata = client.post(f"/api/impact/reports/{report['id']}/errata", json={"actor": "manager", "note": "太早了"})
    assert premature_errata.status_code == 409

    published = client.post(f"/api/impact/reports/{report['id']}/publish", json={"actor": "manager"})
    assert published.json()["status"] == "published"
    locked = client.post(f"/api/impact/reports/{report['id']}/lock", json={"actor": "manager"})
    assert locked.json()["status"] == "locked"
    assert locked.json()["locked_by"] == "manager"

    # 锁定后来了新数据也不能静默重算
    client.post("/api/impact/participation-batches", json=batch_payload("B-002", 10, 10, 0, "2026-05-02"))
    blocked = client.post("/api/impact/reports/compute", json={
        "activity_code": ACTIVITY["activity_code"], "formula_code": "impact-core",
        "formula_version": 1, "computed_by": "analyst",
    })
    assert blocked.status_code == 409

    # 追加勘误：修正后续行动率，原始数字不变
    errata = client.post(f"/api/impact/reports/{report['id']}/errata", json={
        "actor": "manager",
        "note": "问卷 S-1 漏录 3 份承诺，后续行动率由 0.5 更正为 0.6",
        "metrics_patch": {"follow_up": {"rate": 0.6}},
    })
    assert errata.status_code == 201, errata.text
    detail = errata.json()
    assert detail["metrics"]["follow_up"]["rate"] == 0.5
    assert detail["effective_metrics"]["follow_up"]["rate"] == 0.6
    assert len(detail["errata"]) == 1
    assert detail["errata"][0]["note"].startswith("问卷 S-1")

    again = client.post(f"/api/impact/reports/{report['id']}/errata", json={
        "actor": "manager", "note": "补充说明：原始记录已归档",
    })
    assert again.status_code == 201
    assert len(again.json()["errata"]) == 2

    compare = client.get(f"/api/impact/activities/{ACTIVITY['activity_code']}/compare").json()
    assert compare["reports"][0]["errata_count"] == 2
    assert compare["reports"][0]["status"] == "locked"


def test_locked_idempotent_recompute_returns_same_report(client):
    create_activity(client)
    create_formula(client)
    client.post("/api/impact/participation-batches", json=batch_payload("B-001", 40, 30, 10))
    report = compute(client)
    client.post(f"/api/impact/reports/{report['id']}/publish", json={"actor": "manager"})
    client.post(f"/api/impact/reports/{report['id']}/lock", json={"actor": "manager"})

    # 输入未变时，即使已锁定也返回原报告而不是报错
    same = client.post("/api/impact/reports/compute", json={
        "activity_code": ACTIVITY["activity_code"], "formula_code": "impact-core",
        "formula_version": 1, "computed_by": "analyst",
    })
    assert same.status_code == 201
    assert same.json()["reused"] is True
    assert same.json()["id"] == report["id"]
    assert same.json()["status"] == "locked"


def test_behavior_basis_follow_up_window(client):
    create_activity(client)
    create_formula(client, code="impact-behavior", name="行为口径", definition={
        "depth": {"observation_weight": 1.0, "returning_weight": 2.0},
        "follow_up": {"action_types": ["复访", "志愿报名"], "window_days": 30, "basis": "behavior"},
        "coverage": {"survey_min_rate": 0.6},
    })
    client.post("/api/impact/participation-batches", json=batch_payload("B-001", 20, 15, 5))
    events = [
        ("E-1", "复访", 1, "alias-a", "2026-05-10T09:00:00+00:00"),   # 窗口内
        ("E-2", "志愿报名", 1, "alias-b", "2026-05-20T09:00:00+00:00"), # 窗口内
        ("E-3", "复访", 1, "alias-a", "2026-05-25T09:00:00+00:00"),   # 窗口内，同一别名
        ("E-4", "复访", 1, "alias-c", "2026-08-15T09:00:00+00:00"),   # 超出 30 天窗口
        ("E-5", "物种记录", 4, "alias-d", "2026-05-10T09:00:00+00:00"), # 非后续行动类型
    ]
    for code, behavior, qty, alias, observed in events:
        response = client.post("/api/impact/observation-events", json={
            "event_code": code, "activity_code": ACTIVITY["activity_code"],
            "behavior_type": behavior, "quantity": qty,
            "participant_alias": alias, "observed_at": observed,
        })
        assert response.status_code == 201, response.text

    response = client.post("/api/impact/reports/compute", json={
        "activity_code": ACTIVITY["activity_code"], "formula_code": "impact-behavior",
        "formula_version": 1, "computed_by": "analyst",
    })
    assert response.status_code == 201, response.text
    follow_up = response.json()["metrics"]["follow_up"]
    assert follow_up["action_events"] == 3
    assert follow_up["action_participants"] == 2
    assert follow_up["rate"] == 0.1  # 2 / 20
    assert follow_up["estimate"] is False
    assert follow_up["insufficient_coverage"] is False


def test_activity_listing_and_detail(client):
    create_activity(client)
    create_activity(client, activity_code="ACT-2026-02", title="夜间自然观察", category="自然观察",
                    starts_at="2026-06-01T19:00:00+00:00", ends_at="2026-06-01T21:00:00+00:00")
    client.post("/api/impact/participation-batches", json=batch_payload("B-001", 20, 15, 5))

    guided = client.get("/api/impact/activities", params={"category": "导赏"}).json()["items"]
    assert [item["activity_code"] for item in guided] == ["ACT-2026-01"]

    detail = client.get("/api/impact/activities/ACT-2026-01").json()
    assert detail["batches_count"] == 1
    assert detail["reports_count"] == 0

    missing = client.get("/api/impact/activities/NOPE")
    assert missing.status_code == 404

    batches = client.get("/api/impact/participation-batches", params={"activity_code": "ACT-2026-01"}).json()["items"]
    assert [item["batch_code"] for item in batches] == ["B-001"]


def test_compute_requires_existing_formula_and_activity(client):
    create_activity(client)
    create_formula(client)
    missing_formula = client.post("/api/impact/reports/compute", json={
        "activity_code": ACTIVITY["activity_code"], "formula_code": "impact-core",
        "formula_version": 9, "computed_by": "analyst",
    })
    assert missing_formula.status_code == 404
    missing_activity = client.post("/api/impact/reports/compute", json={
        "activity_code": "NOPE", "formula_code": "impact-core",
        "formula_version": 1, "computed_by": "analyst",
    })
    assert missing_activity.status_code == 404
