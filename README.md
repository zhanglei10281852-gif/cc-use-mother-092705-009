# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。

## 活动影响指标服务

`app/impact` 模块面向公益团队的活动效果评估，接口前缀为 `/api/impact`：

- **数据接收**：`POST /activities`（活动）、`POST /participation-batches`（匿名参与批次，含首次/重复参与人数）、`POST /observation-events`（观察行为）、`POST /survey-summaries`（问卷摘要）。批次、行为、问卷均按业务编码幂等：同一编码重复上传返回原记录（`deduplicated: true`），不会重复计入；同一编码对应不同内容则返回 409。数据可按任意顺序导入。
- **口径版本**：`POST /formulas` 创建首个口径版本，`POST /formulas/{code}/versions` 生成新版本。口径定义包含参与深度权重、后续行动类型与统计窗口、问卷覆盖率阈值；已创建的版本不可变，旧版本与其计算结果永久保留。
- **指标计算**：`POST /reports/compute` 按选定口径版本计算参与深度与后续行动率。每份报告记录 `input_digest`（输入数据与口径的摘要）和来源清单，输入未变时重算幂等返回原报告，输入变化时生成新序号报告而不覆盖旧结果。问卷缺失或回收覆盖率低于口径阈值时，报告标记 `coverage_status` 为 `missing`/`insufficient`，后续行动率置为 `null` 而不是按零处理。
- **发布与锁定**：`POST /reports/{id}/publish` 发布，`POST /reports/{id}/lock` 锁定。锁定后重算返回 409，只能通过 `POST /reports/{id}/errata` 追加勘误；原始指标保持不变，接口同时返回合并勘误后的 `effective_metrics`。
- **对比复核**：`GET /activities/{code}/compare` 与 `GET /reports` 列出每次结果的口径版本、来源、覆盖率和勘误数量，`GET /reports/{id}` 返回完整明细与勘误记录。
