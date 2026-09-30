from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_impact_demo() -> int:
    """活动影响指标验收演示：乱序导入、切换口径版本、发布锁定、勘误与对比。"""
    actor = "cli-demo"
    steps: dict[str, object] = {}
    with TestClient(app) as client:
        for code, kind, day in [("reed-tour", "导赏", "2026-08-02"), ("mudflat-watch", "自然观察", "2026-08-16")]:
            created = client.post("/api/impact/activities", json={"code": code, "name": f"演示-{code}", "kind": kind, "starts_on": day, "actor": actor})
            if created.status_code != 201:
                print(created.text)
                return 1
        # 乱序导入三个参与批次，其中一批重复上传验证幂等
        batches = [
            {"batch_key": "demo-batch-late", "activity_code": "mudflat-watch", "occurred_on": "2026-08-16", "participant_count": 30, "returning_count": 6},
            {"batch_key": "demo-batch-early-2", "activity_code": "reed-tour", "occurred_on": "2026-08-02", "participant_count": 20, "returning_count": 2},
            {"batch_key": "demo-batch-early-1", "activity_code": "reed-tour", "occurred_on": "2026-08-01", "participant_count": 25, "returning_count": 5},
        ]
        for batch in batches:
            response = client.post("/api/impact/participation-batches", json={**batch, "actor": actor})
            if response.status_code != 201:
                print(response.text)
                return 1
        replay = client.post("/api/impact/participation-batches", json={**batches[0], "actor": actor})
        steps["batch_replay_same_id"] = replay.json()["id"]
        observations = [
            {"record_key": "demo-obs-late", "batch_key": "demo-batch-late", "observed_on": "2026-08-16", "observers": 30, "actions": 90, "minutes": 300},
            {"record_key": "demo-obs-early-2", "batch_key": "demo-batch-early-2", "observed_on": "2026-08-02", "observers": 20, "actions": 30, "minutes": 100},
            {"record_key": "demo-obs-early-1", "batch_key": "demo-batch-early-1", "observed_on": "2026-08-01", "observers": 25, "actions": 50, "minutes": 150},
        ]
        for observation in observations:
            response = client.post("/api/impact/observation-records", json={**observation, "actor": actor})
            if response.status_code != 201:
                print(response.text)
                return 1
        # demo-batch-early-2 故意缺问卷，验证覆盖不足标记
        surveys = [
            {"survey_key": "demo-survey-late", "batch_key": "demo-batch-late", "distributed": 30, "returned": 27, "follow_up_intents": 9, "follow_up_actions": 3},
            {"survey_key": "demo-survey-early-1", "batch_key": "demo-batch-early-1", "distributed": 25, "returned": 25, "follow_up_intents": 15, "follow_up_actions": 6},
        ]
        for survey in surveys:
            response = client.post("/api/impact/survey-summaries", json={**survey, "actor": actor})
            if response.status_code != 201:
                print(response.text)
                return 1
        v1 = client.post("/api/impact/definitions", json={"code": "demo-impact", "name": "演示口径", "params": {"depth_weights": {"action": 1.0, "minute": 0.0, "returning": 0.0}}, "actor": actor})
        period = {"scope_type": "period", "period_start": "2026-08-01", "period_end": "2026-08-31", "definition_code": "demo-impact"}
        report_v1 = client.post("/api/impact/reports/compute", json={**period, "actor": actor})
        if report_v1.status_code != 201:
            print(report_v1.text)
            return 1
        steps["v1_coverage"] = report_v1.json()["revisions"][-1]["coverage"]["status"]
        steps["v1_follow_up_rate"] = report_v1.json()["revisions"][-1]["metrics"]["follow_up_rate"]
        # 切换口径版本：加入时长权重，生成第二版并重新计算
        v2 = client.post("/api/impact/definitions", json={"code": "demo-impact", "name": "演示口径", "params": {"depth_weights": {"action": 1.0, "minute": 0.1, "returning": 0.0}}, "actor": actor})
        report_v2 = client.post("/api/impact/reports/compute", json={**period, "actor": actor})
        steps["definition_versions"] = [v1.json()["version"], v2.json()["version"]]
        report_v2_id = report_v2.json()["id"]
        # 发布、锁定，锁定后重算应被拒绝，只能追加勘误
        client.post(f"/api/impact/reports/{report_v2_id}/publish", json={"actor": "manager"})
        client.post(f"/api/impact/reports/{report_v2_id}/lock", json={"actor": "manager", "reason": "演示定稿"})
        blocked = client.post("/api/impact/reports/compute", json={**period, "actor": actor})
        steps["recompute_after_lock_status"] = blocked.status_code
        erratum = client.post(f"/api/impact/reports/{report_v2_id}/errata", json={"actor": "manager", "reason": "演示勘误", "corrections": {"participants_total": 76}})
        steps["erratum_status"] = erratum.status_code
        compared = client.get(f"/api/impact/reports/compare?report_ids={report_v1.json()['id']},{report_v2_id}")
        steps["compare_items"] = [
            {
                "report_id": item["report_id"],
                "definition_version": item["definition"]["version"],
                "coverage": item["coverage"]["status"],
                "sources": item["sources"]["batches"],
                "errata_count": len(item["errata"]),
            }
            for item in compared.json()["items"]
        ]
    print(json.dumps(steps, ensure_ascii=False, indent=2))
    ok = (
        steps["v1_coverage"] == "insufficient"
        and steps["v1_follow_up_rate"] is None
        and steps["definition_versions"] == [1, 2]
        and steps["recompute_after_lock_status"] == 409
        and steps["erratum_status"] == 201
        and len(steps["compare_items"]) == 2
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("impact-demo", help="执行活动影响指标验收演示")
    args = parser.parse_args()
    return {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "impact-demo": command_impact_demo,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
