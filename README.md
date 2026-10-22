# 面向教师的全面教学助手

**最新增量：老师反馈驱动的 Agent 细则修订。** 在已有任务历史与恢复基础上，老师可基于当前草稿提出修改意见，生成独立的新草稿并单独确认；原稿、已确认版本、来源快照与修改意见均保留。后端 **118 passed**、前端 **40 passed**，Ruff、类型、构建及浏览器完整流程通过。新增迁移 **`0006`**，本机业务库已在复验时备份（`.local/backups/teaching-before-0006.dump`）并升级；GitHub Actions 最新提交的 frontend / backend / browser 三个 job 全部通过。真实 DeepSeek 单题修订也已跑通（2 次调用、草稿 2 点满分 10.00），但仍无教师标注与单价。详见 [修订交付记录](docs/AGENT_REVISION_2026-10-22.md) 与 [复验记录](docs/AGENT_REVISION_REVIEW_2026-10-22.md)；前阶段的 [任务历史与恢复](docs/AGENT_HISTORY_2026-10-22.md) 保留。

已实现第一条最小后端链路：

**确认评分细则 → 导入文本答案 → 分点评估与证据 → 老师复核 → 固定快照导出。**

Python + FastAPI + PostgreSQL + 自研 asyncio Worker。已有列表接口、Jev 与 DeepSeek 适配器代码及小样本对照命令；React 教师工作台覆盖课程名单、作业/题目、细则确认、答案导入、建批次、复核和导出。默认 `fake-v1` 是模拟评估器，不做语义评分；真实模型需显式配置和选择，所有成绩须老师确认。

**2026-10-22 新增评分细则 Agent：读取题目 → 工具调用起草 → 保存草稿 → 等待老师 → 确认后完成。** 参考本机 Claude Code `src` 的工具、Hook、上下文与停止机制，自主实现持久模型循环、预算、取消和恢复。默认模拟器，DeepSeek 显式选用；作业准备页已有“用 Agent 起草”面板，可查看进度、预算、事件与失败原因，打开草稿后仍由老师显式确认。见 [Agent 使用与验收](docs/RUBRIC_AGENT_2026-10-22.md)。

**前阶段验证（2026-10-22 审查修复后）：后端 110 项测试全部通过**，Ruff 检查与 51 个文件格式检查通过；前端 34 项 Vitest、`tsc` 与 `vite build` 通过；浏览器回归 7/7 断言阶段、0 浏览器错误，覆盖 Agent 起草 → 等待老师 → 确认完成。业务库已从 `0003` 升级到 `0005`（升级前备份 `.local/backups/teaching-before-0005.dump`），演示链路在真实库上重新跑通。**真实 DeepSeek 已验证**：`deepseek-chat`（实际返回 `deepseek-flash`）的 compare 2/2、评分批次与 Agent 起草均完成，并修复了真实 `tool_calls` 的 `index` 被拒、等级排序指引缺失两个缺陷（见 [真实验证记录](.local/e2e/real-deepseek-verification-2026-10-22.md)）。`0004` 在含历史数据的测试库上精确回填 4 条指纹。本轮修复清单见 [审查修复记录](docs/REVIEW_FIXES_2026-10-22.md)。

**2026-10-21 修复前验证记录：** 用户报告记载 **72 项 pytest** 通过，DeepSeek 完整应用链路由本地 HTTP 桩走通，未调用真实 DeepSeek；该结果不覆盖本轮指纹修复。此前导入预览、浏览器回归、Jev 强杀恢复与并发 409 的历史记录见 [修复与验收边界](docs/TEST_REVIEW_2026-10-21.md)。真实教学质量、DeepSeek 供应商行为与费用仍未评测。

## 启动教师工作台

先按下方说明启动已有 API；需要处理待办时再启动 Worker。另开终端：

```powershell
Set-Location 'D:\PyChram_project\面向教师的全面助手\frontend'
npm.cmd ci
npm.cmd run dev
```

打开 [教师工作台](http://127.0.0.1:5173)，输入根目录 `.env` 中的 `TA_DEV_API_TOKEN`。Node 建议使用 CI 固定的 24.16.0，详细说明见 [前端 README](frontend/README.md)。已有数据库需要先执行 `uv run --frozen alembic upgrade head` 升至 `0006`，然后重启 API 和 Worker；本机业务库已于 2026-10-22 备份后升级。

侧栏“作业准备”可从新建课程名单开始，完成作业、题目、细则确认、答案导入和建批次；“作业与复核”处理已有批次。答案导入支持逐份录入、结构化 JSON 粘贴，以及 **CSV 粘贴或选择文件**（UTF-8，首行表头；必填 `external_student_id`、`answer_text`，可选 `external_submission_id`、`source_revision`）。CSV 在浏览器内映射到固定名单，提交时仍由服务器重新校验并原子写入；Excel 文件格式仍待真实样例。

## 直接查看本机结果

前一阶段 M0 已在当前工作区建立 `.venv`、锁定依赖和未纳入版本管理的 `.env`，并使用本机 PostgreSQL 17.11（`127.0.0.1:55432`）运行了演示。`teaching` 保存合成演示数据，`teaching_test` 用于隔离测试。以下已保存结果来自该阶段。

- [完整演示导出](.local/demo-result.json)：2 份答案，1 份模拟老师确认、1 份待复核。
- [实现状态与边界](docs/IMPLEMENTATION_STATUS.md)：已实现能力、实际验证、后续工作。
- [演示脚本](backend/src/teaching_assistant/demo.py)：完整 API 请求示例。
- [当前列表与模型对照](docs/API_AND_EVALUATION.md)：接口、配置和评测边界。
- [审查与下一阶段计划](docs/REVIEW_AND_NEXT_STEPS_2026-10-20.md)：回归资产收口、教师复核工作台及真实评测的实施顺序。

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

评分细则 Agent 使用单独命令 `uv run --frozen teaching-assistant agent-worker`，也支持 `--once / --drain`。通过 `POST /v1/question-versions/{id}/rubric-agent-runs` 创建任务；运行状态、草稿和调用事件的接口见 [Agent 说明](docs/RUBRIC_AGENT_2026-10-22.md)。

## 从空环境启动

要求 Python 3.12–3.14、uv、PostgreSQL 17。此前 M0 实际验证环境为 Windows、Python 3.13.5、PostgreSQL 17.11。

1. 安装依赖，创建本地配置。

```powershell
uv sync --frozen --python 3.13
Copy-Item .env.example .env
```

将 `.env` 中的 `TA_DEV_API_TOKEN` 改成自选的至少 20 字符令牌。开发身份通过数据库中保存的令牌哈希绑定老师；不能在请求中传入 `teacher_id` 冒充身份。该入口仅用于本地开发，已有令牌轮换命令；正式登录及组织权限尚未实现。

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

随后按上方命令启动 API 和 Worker。轮换本机开发令牌时，先修改 `.env` 的 `TA_DEV_API_TOKEN`，再执行 `uv run --frozen teaching-assistant rotate-token`：它替换原老师的令牌摘要而不新建身份，并写入审计事件。不要用重新 `bootstrap` 代替轮换，那会生成第二个老师、看不到原有课程。不要使用 `Base.metadata.create_all()` 替代迁移：迁移包含保护固定记录的数据库触发器，以及旧评估配置的兼容记录。

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
| `GET /v1/assignments/{id}` | 作业、课程/班级信息及作业固定名单 |
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

新增 12 个分页列表接口和 4 个版本详情接口，覆盖课程、班级、学生、作业、题目、细则、答案、导入、批次、复核项与导出。完整路径及筛选参数见 [接口说明](docs/API_AND_EVALUATION.md#列表与详情接口)；待复核查询为 `GET /v1/grading-items?status=succeeded&review_status=pending`。

JSON 导出包含细则、原答案、引用及复核历史。CSV 是简化成绩表，保留版本、模拟标识、执行状态和复核状态，分别列出建议分数与确认分数；待确认分数留空。

首个 CSV 导入格式已在页面定义（表头契约见上），但仍建议用真实样例复核；Excel 文件适配继续等待样例。内部 JSON 导入契约可独立使用。

## 测试记录与运行方式

用户后续已授权执行验证。最新实际结果见本文顶部及对应交付记录；可用以下命令在专用测试环境复跑，`alembic check` 会读取当前 `TA_DATABASE_URL` 指向的数据库：

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

此前 [C 阶段记录](docs/ASSIGNMENT_PREPARATION.md#本机验证结果)记载 **42 项 pytest、21 项 Vitest 通过**，包含固定名单、细则/导入契约和未知写操作等用例。修复后实测为后端 **42 项**、前端 **22 项**（新增含冒号的行标识用例），[正式浏览器回归](frontend/e2e/README.md) 已于 2026-10-21 在独立数据库执行通过。前端单测在 `frontend/` 下运行 `npm test`，`npm run build` 包含类型检查；浏览器测试在 `frontend/e2e/` 下独立运行，需专用数据库和服务。历史通过数不能替代本次记录。

最新真实 compare 是 1 份合成答案的 2 个评分点，不构成真实教师标注集上的模型质量对照。费用依据、强制终止、并发负载及人工复核收益仍需后续补齐，详见审查记录。

## 代码入口

- [前端工程](frontend/README.md)：教师复核工作台、启动方式和用户验收入口。
- [domain.py](backend/src/teaching_assistant/domain.py)：细则契约、候选等级、Decimal 计分、引用校验。
- [service.py](backend/src/teaching_assistant/service.py)：授权范围、业务事务、幂等回执、复核、快照。
- [worker.py](backend/src/teaching_assistant/worker.py)：租约、执行代次、调用预算、检查点和重试。
- [evaluators.py](backend/src/teaching_assistant/evaluators.py)：评估与证据接口、模拟实现、人工证据占位。
- [jev.py](backend/src/teaching_assistant/jev.py)：Jev HTTP 适配与响应校验。
- [profiles.py](backend/src/teaching_assistant/profiles.py)：模型及提示配置的版本化。
- [listings.py](backend/src/teaching_assistant/listings.py)：列表查询与游标分页。
- [comparison.py](backend/src/teaching_assistant/comparison.py)：固定小样本对照与报告。
- [db.py](backend/src/teaching_assistant/db.py)：26 张业务和执行表的模型；迁移见 `backend/migrations`。
- [api.py](backend/src/teaching_assistant/api.py)：API、开发鉴权和导出。

较长远的设计与本次实现需要区分阅读：[长期记忆](docs/PROJECT_MEMORY.md)、[架构审查](docs/ARCHITECTURE_REVIEW.md)、[原实施方案](docs/IMPLEMENTATION_PLAN.md)、[参考审查](docs/REFERENCE_AUDIT.md)。EchoMind 仅用于借鉴部分思路，本工程未迁移其客服 Agent 代码。
