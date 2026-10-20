# 面向教师的全面教学助手

已实现第一条最小后端链路：

**确认评分细则 → 导入文本答案 → 分点评估与证据 → 老师复核 → 固定快照导出。**

Python + FastAPI + PostgreSQL + 自研 asyncio Worker。已补课程到复核项的列表接口、可选的真实 Jev 适配器与小样本对照命令。默认 `fake-v1` 仍是模拟评估器，不做语义评分；Jev 需显式配置和选择，证据由老师补充。所有结果都需老师复核。普通生成式模型与教师网页工作台尚未实现。

**2026-10-20 更新：本轮按要求只写代码，未运行测试、迁移或真实模型调用。** 使用新版 API/Worker 前先执行 `uv run --frozen alembic upgrade head`；本机数据库尚未应用新增的 `0003`。完整配置、列表参数和对照命令见 [列表与真实模型使用说明](docs/LISTS_AND_MODEL.md)。

## 直接查看本机结果

前一阶段 M0 已在当前工作区建立 `.venv`、锁定依赖和未纳入版本管理的 `.env`，并使用本机 PostgreSQL 17.11（`127.0.0.1:55432`）运行了演示。`teaching` 保存合成演示数据，`teaching_test` 用于隔离测试。以下已保存结果来自该阶段。

- [完整演示导出](.local/demo-result.json)：2 份答案，1 份模拟老师确认、1 份待复核。
- [实现状态与边界](docs/IMPLEMENTATION_STATUS.md)：已实现能力、实际验证、后续工作。
- [演示脚本](backend/src/teaching_assistant/demo.py)：完整 API 请求示例。
- [列表与模型接入](docs/LISTS_AND_MODEL.md)：本轮新增代码、配置与待验证边界。

在项目根目录可以重新运行；同一 `run-id` 复用已有回执和记录：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.cache\uv'
uv run --frozen alembic upgrade head
uv run --frozen teaching-assistant demo
```

该命令直接通过 ASGI 调用 API，并在进程内调用同一 Worker，不依赖已经启动的 HTTP 服务。它只处理本次演示批次的任务，并且仅对明确标记的合成答案模拟一次老师复核。

启动可交互的 API 文档：

```powershell
uv run --frozen uvicorn teaching_assistant.api:create_app --factory --host 127.0.0.1 --port 8000
```

打开 [API 文档](http://127.0.0.1:8000/docs)。在 `Authorize` 中填入本地 `.env` 的 `TA_DEV_API_TOKEN`。另开终端运行持续处理新批次的 Worker：

```powershell
uv run --frozen teaching-assistant worker
```

`worker --once` 领取一份答案；`worker --drain` 处理当前可领取项目后退出，不等待未来的重试到期时间。持续 Worker 才会继续扫描到期重试及失效租约。

## 从空环境启动

要求 Python 3.12–3.14、uv、PostgreSQL 17。此前 M0 实际验证环境为 Windows、Python 3.13.5、PostgreSQL 17.11。

1. 安装依赖，创建本地配置。

```powershell
uv sync --frozen --python 3.13
Copy-Item .env.example .env
```

将 `.env` 中的 `TA_DEV_API_TOKEN` 改成自选的至少 20 字符令牌。开发身份通过数据库中保存的令牌哈希绑定老师；不能在请求中传入 `teacher_id` 冒充身份。该入口仅用于本地开发，正式登录、令牌轮换及组织权限尚未实现。

2. 启动 PostgreSQL。已有实例时设置 `TA_DATABASE_URL`；有 Docker 时可用：

```powershell
docker compose up -d postgres
```

Compose 只把数据库绑定到 `127.0.0.1:5432`。本机本次开发使用的是 `55432`，应保留本机 `.env` 中对应端口，不能与新环境的默认配置混用。

3. 应用迁移并创建开发老师。

```powershell
uv run --frozen alembic upgrade head
uv run --frozen teaching-assistant bootstrap
uv run --frozen teaching-assistant demo
```

随后按上方命令启动 API 和 Worker。不要使用 `Base.metadata.create_all()` 替代迁移：迁移包含保护固定记录的数据库触发器，以及旧评估配置的兼容记录。

### 本机无 Docker 的辅助启动方式

[PostgreSQL 官方 Windows 下载页](https://www.postgresql.org/download/windows/)提供 EDB 二进制包入口。把 PostgreSQL 17 的 Windows x64 ZIP 放到 `.local/postgresql-windows.zip` 后，可以运行：

```powershell
.\scripts\local-postgres.ps1 -Action start
.\scripts\local-postgres.ps1 -Action status
# 使用结束后停止，保留数据：
.\scripts\local-postgres.ps1 -Action stop
```

脚本默认端口 `55432`，创建 `teaching` 和 `teaching_test` 两个数据库，不安装系统服务。EDB Windows 程序在中文路径下定位自身存在问题，因此脚本把项目专用实例放在当前用户 TEMP 下的 `teaching-pg-<项目路径哈希>` 目录；这是可清理的开发环境，不能作为真实教学数据的长期存储。

## 已实现的接口

除导入预览外，所有 POST 写操作都要求 `Idempotency-Key`。同一身份、操作、key 和请求内容返回原回执；key 对应不同内容时返回 409。

| 接口 | 用途 |
| --- | --- |
| `POST /v1/workspaces` | 创建课程、班级和学生名单 |
| `POST /v1/courses/{id}/assignments` | 建作业并固定参与名单 |
| `POST /v1/assignments/{id}/question-versions` | 创建新的题目版本 |
| `POST /v1/question-versions/{id}/rubric-drafts` | 保存手动录入的细则草稿 |
| `POST /v1/rubric-drafts/{id}/confirm` | 确认固定细则版本 |
| `POST /v1/assignments/{id}/imports/preview` | 检查结构化 JSON 答案，不写入 |
| `POST /v1/assignments/{id}/imports` | 重新校验并原子提交全部答案 |
| `POST /v1/grading-batches` | 建批次，同一评估目标跨批次复用 |
| `GET /v1/grading-batches/{id}` | 批次、单项执行和复核状态 |
| `GET /v1/grading-items/{id}` | 原答案、细则、分项、证据、复核历史、调用记录 |
| `POST /v1/grading-items/{id}/reviews` | 按修订号追加完整复核 |
| `POST /v1/grading-items/{id}/retry` | 单项失败重试，显式补充有限额度 |
| `POST /v1/grading-batches/{id}/exports` | 创建 working / confirmed_only 快照 |
| `GET /v1/exports/{id}?format=json\|csv` | 读取固定快照或下载 CSV |
| `GET /v1/evaluation-profiles` | 查看当前可选配置及具体 ID，不调用模型 |

新增 12 个分页列表接口和 4 个版本详情接口，覆盖课程、班级、学生、作业、题目、细则、答案、导入、批次、复核项与导出。完整路径及筛选参数见 [接口说明](docs/LISTS_AND_MODEL.md#2-列表与详情接口)；待复核查询为 `GET /v1/grading-items?status=succeeded&review_status=pending`。

JSON 导出包含细则、原答案、引用及复核历史。CSV 是简化成绩表，保留版本、模拟标识、执行状态和复核状态，分别列出建议分数与确认分数；待确认分数留空。

首个 CSV/Excel **导入**格式仍未选定；当前内部 JSON 导入契约可独立使用。

## 由你执行的验证

本轮没有执行以下命令：

```powershell
uv run --frozen ruff check backend
uv run --frozen ruff format --check backend
uv run --frozen alembic check
uv run --frozen pytest -q
```

数据库测试要求配置 `TA_TEST_DATABASE_URL`，数据库名必须以 `_test` 结尾。Compose 的首次测试设置：

```powershell
docker compose exec postgres createdb -U teaching teaching_test
$env:TA_TEST_DATABASE_URL = 'postgresql+asyncpg://teaching:local-development-only@127.0.0.1:5432/teaching_test'
uv run --frozen pytest -q
```

每项测试在专用数据库的随机 schema 中应用真实迁移，结束时仅删除该 schema。未配置测试库时，集成测试会明确跳过，不能把这种结果视为完整验收。

历史 M0 版本曾有 **21 项测试通过**，包含 PostgreSQL 下的去重、导入回滚、权限隔离、并发复核、租约恢复和快照验证。本轮改动尚未重新执行这些测试，也未新增测试覆盖；不能用历史结果推断新增列表、真实适配器及迁移已经验收。没有真实模型质量、性能或生产部署结论。

## 代码入口

- [domain.py](backend/src/teaching_assistant/domain.py)：细则契约、候选等级、Decimal 计分、引用校验。
- [service.py](backend/src/teaching_assistant/service.py)：授权范围、业务事务、幂等回执、复核、快照。
- [worker.py](backend/src/teaching_assistant/worker.py)：租约、执行代次、调用预算、检查点和重试。
- [evaluators.py](backend/src/teaching_assistant/evaluators.py)：评估与证据接口、模拟实现、人工证据占位。
- [jev.py](backend/src/teaching_assistant/jev.py)：Jev HTTP 适配与响应校验。
- [profiles.py](backend/src/teaching_assistant/profiles.py)：模型及提示配置的版本化。
- [listings.py](backend/src/teaching_assistant/listings.py)：列表查询与游标分页。
- [comparison.py](backend/src/teaching_assistant/comparison.py)：固定小样本对照与报告。
- [db.py](backend/src/teaching_assistant/db.py)：24 张业务和执行表的模型；迁移见 `backend/migrations`。
- [api.py](backend/src/teaching_assistant/api.py)：API、开发鉴权和导出。

较长远的设计与本次实现需要区分阅读：[长期记忆](docs/PROJECT_MEMORY.md)、[架构审查](docs/ARCHITECTURE_REVIEW.md)、[原实施方案](docs/IMPLEMENTATION_PLAN.md)、[参考审查](docs/REFERENCE_AUDIT.md)。EchoMind 仅用于借鉴部分思路，本工程未迁移其客服 Agent 代码。
