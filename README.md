# 面向教师的全面教学助手

一个把作业准备、AI 初评和教师复核串起来的本地教学工作台。

老师可以录入题目、准备评分细则，再导入学生的文本答案并发起批量初评。系统展示评分建议、理由和依据；老师结合原答案逐项检查、调整并确认，最后导出成绩和复核记录。

**AI 提供建议，最终评分由老师决定。**

## 能做什么

### 准备作业与评分细则

创建课程、班级和学生名单，录入作业题目。评分细则可以手动编写，也可以由 Agent 生成草稿；老师确认后，细则才用于批改。

如果对草稿不满意，可以提出修改意见，生成新的草稿，并在确认前对照新旧版本。原稿、来源快照和已确认版本会保留。

### 批量初评，逐份复核

答案支持逐份录入、JSON 和 CSV 导入。老师选择题目、细则和评估配置后，系统按批次处理答案。复核页面把原答案、评分点、建议等级和理由放在一起；老师可以补充依据、调整评分并记录理由。

系统分别保存 AI 建议分数和教师确认分数，方便回看判断过程。

### 导出固定结果

可以导出 CSV 成绩表，或导出包含答案、细则和复核记录的 JSON。每次导出保存当时的快照，后续修改评分不会改变已生成的结果。

## 使用流程

```text
创建课程与名单 → 录入题目 → 起草并确认评分细则
                                   ↓
              导出成绩 ← 教师复核 ← 批量初评 ← 导入答案
```

默认 `fake-v1` 评估器只用于体验流程，不进行真实语义评分。项目提供 DeepSeek 和 Jev 适配器；使用真实模型需要自行配置并在工作台中明确选择，结果仍需要老师复核。

## 本地运行

### 准备环境

- Python 3.12–3.14 和 [uv](https://docs.astral.sh/uv/)
- Node.js；仓库 CI 使用 24.16.0
- PostgreSQL 17，或使用 Docker Compose 启动数据库

以下示例使用 Windows PowerShell。除前端命令外，均在项目根目录执行。

### 1. 安装依赖并配置

```powershell
uv sync --frozen --python 3.13
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env`，将 `TA_DEV_API_TOKEN` 改为自己的令牌（至少 20 个字符）。如使用已有 PostgreSQL，还需设置 `TA_DATABASE_URL`。已有 `.env` 时保留原配置即可。

### 2. 启动数据库并初始化

使用仓库提供的数据库配置：

```powershell
docker compose up -d postgres
```

等待数据库就绪后，执行：

```powershell
uv run --frozen alembic upgrade head
uv run --frozen teaching-assistant bootstrap
```

已有 PostgreSQL 实例时可跳过 Docker 命令；升级已有数据库前请先备份。

### 3. 启动 API 与后台任务

分别在三个终端中运行，并保持运行：

```powershell
# API
uv run --frozen uvicorn teaching_assistant.api:create_app --factory --host 127.0.0.1 --port 8000
```

```powershell
# 批量初评 Worker
uv run --frozen teaching-assistant worker
```

```powershell
# 评分细则 Agent Worker
uv run --frozen teaching-assistant agent-worker
```

API 启动后可查看 [本地接口文档](http://127.0.0.1:8000/docs)。

### 4. 启动教师工作台

另开终端：

```powershell
Set-Location .\frontend
npm.cmd ci
npm.cmd run dev
```

打开 [教师工作台](http://127.0.0.1:5173)，输入 `.env` 中设置的开发令牌。前端启动和配置的更多说明见 [前端 README](frontend/README.md)。

如果想先体验一组模拟数据，可在项目根目录运行：

```powershell
uv run --frozen teaching-assistant demo
```

演示使用合成答案和模拟评估，不代表真实教学场景中的评分质量。

## 当前范围

- 目前面向文本答案，支持 JSON 和 UTF-8 CSV；不直接处理 Excel、图片或手写作业。CSV 需要 `external_student_id` 和 `answer_text` 列。
- 当前工作台使用本地开发令牌，正式账号登录与学校组织权限尚未实现。
- 真实模型需要单独配置；模型质量、费用和人工复核收益应结合实际教学数据进一步评估。

## 技术与项目结构

前端使用 React、TypeScript 和 Vite；后端使用 Python、FastAPI、PostgreSQL 和自研 asyncio Worker。

```text
├── frontend/       教师工作台
├── backend/        API、评分逻辑、Agent 和后台任务
├── docs/           使用说明、设计与验证记录
├── evals/          评估样例与配置
├── scripts/        本地辅助脚本
├── compose.yaml    PostgreSQL 启动配置
└── .env.example    环境变量示例
```

进一步了解：[作业准备](docs/ASSIGNMENT_PREPARATION.md) · [评分细则 Agent](docs/RUBRIC_AGENT_2026-10-22.md) · [模型与接口](docs/API_AND_EVALUATION.md) · [实现状态](docs/IMPLEMENTATION_STATUS.md)
