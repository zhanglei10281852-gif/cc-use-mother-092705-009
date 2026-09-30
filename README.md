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

## 活动影响指标服务

`/api/impact` 面向公益团队的导赏与自然观察活动评估，解决"只有人数、无法解释重复参与、问卷缺失和指标口径变化"的问题：

- **数据接收**：`POST /activities`、`/participation-batches`、`/observation-records`、`/survey-summaries` 分别接收活动、匿名参与批次、观察行为和问卷摘要。所有接收接口按业务键幂等：同一批次重复上传返回已有记录、不会重复计入；同一键对应不同内容返回 409。问卷摘要支持更正——同一批次上传新摘要时旧摘要自动作废但保留可查。
- **口径版本**：`POST /definitions` 定义指标口径（参与深度权重、覆盖率阈值等）。同一编码参数不变时幂等返回；参数变化自动生成新版本，旧版本与旧计算结果永久保留。
- **可复核计算**：`POST /reports/compute` 按活动或期间范围计算参与深度与后续行动率，每次计算生成不可变修订，记录来源清单（批次键、记录键、问卷键）、输入摘要、指标与覆盖率。缺失问卷的批次计入覆盖率分母并标记 `insufficient`，后续行动率置空而不是按零处理。
- **发布与锁定**：报告状态机为 draft → published → locked（`POST /reports/{id}/publish`、`/lock`）。锁定后重新计算返回 409，只允许通过 `POST /reports/{id}/errata` 追加勘误；勘误只增不改，原始发布数字不被修改，更正值在 `corrected_metrics` 中单独呈现。
- **对比复核**：`GET /reports/compare?report_ids=1,2` 并排输出每份结果的口径版本、来源、覆盖率、指标与勘误记录，支持乱序导入后切换口径版本的横向比较。

完整验收演示：`python -m app.cli impact-demo`（乱序导入三批数据、重复上传验证幂等、缺失问卷触发覆盖不足、切换口径版本、发布锁定后追加勘误并对比）。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入、现有生态计算接口，以及活动影响指标的幂等接收、覆盖不足标记、口径版本、发布锁定与勘误流程。

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
