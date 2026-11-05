# 面向教师的全面教学助手

一个把作业准备、AI 初评、教师复核和课后补练放在一起的教学工作台。

批完一份作业，老师往往还要整理成绩、找出全班容易失分的地方，再准备下一次讲评和练习。这个项目希望把这些工作串起来：先确定怎么评分，再让 AI 帮忙做初评，老师结合原答案检查和确认，最后用复核结果了解班级情况、安排补练。

**AI 提供建议，老师决定最终评分。每个评分点都能回看依据，每次修改都保留记录。**

目前主要面向文本作业，提供可在本地运行的 Web 工作台。

## 能做什么

### 准备作业，也准备好评分标准

在“作业准备”中建立课程、班级和学生名单，录入题目，设置评分细则。你可以自己填写评分点和等级，也可以让 Agent 起草一份，再提出修改意见。

修改已有草稿时，可以对照新旧版本检查变化。细则由老师确认后用于批改，原稿和历史版本都会保留。

### 批量初评，逐点复核

答案支持逐份录入、JSON 导入和 CSV 文件导入。选择题目、评分细则和评估配置后，就可以提交批量初评，查看每份答案的处理进度。

复核时，原答案、评分细则、建议等级和理由放在同一页面。有原文引用时，可以直接定位；也可以手动选取答案片段、补充依据、调整评分并写下理由。系统分别保存建议分数与教师确认分数，方便回看每次判断。

### 看清班级表现，找到讲评线索

批次中的“班级学情”展示老师已确认的成绩均分、分布和各评分点表现。你可以从某个失分较多的评分点出发，回看对应答案和复核理由，再决定课堂上重点讲什么。

页面同时展示确认覆盖率，尚未复核和处理失败的答案不会被当成零分计入统计。

### 从课程题库中安排针对性补练

在“针对性补练”中维护课程题库，按知识点、年级、难度和题型筛选题目。选择班级历史批次后，系统结合已确认的评分点表现检索候选题，展示匹配依据和题目来源。

你可以手动选题，也可以让 Agent 从候选题中选择，给出补练理由和安排。草稿经过老师确认后保存；老师确认的课程经验也可以作为后续补练的参考。

### 导出成绩，保留当时的结果

支持导出 CSV 成绩表和包含答案、细则、引用及复核历史的 JSON。可以导出工作汇总，也可以只导出已确认成绩。

每次导出都会保存一份固定快照，之后修改评分不会改动已经生成的导出文件。

## 怎么使用

日常使用可以按下面的顺序进行：

```text
创建课程与名单 → 录入作业题目 → 起草并确认评分细则
                                      ↓
导出成绩 ← 教师逐份复核 ← 批量初评 ← 导入学生答案
               ↓
          查看班级学情 → 从题库中选题 → 确认补练安排
```

工作台侧栏提供三个入口：

| 入口 | 主要用途 |
| --- | --- |
| 作业准备 | 建立课程和作业、确认细则、导入答案、提交初评 |
| 作业与复核 | 查看批次、复核答案、分析班级表现、导出结果 |
| 针对性补练 | 管理题库和课程经验，根据历史作业安排补练 |

## 本地运行

### 环境准备

- Python 3.12–3.14，推荐 3.13
- [uv](https://docs.astral.sh/uv/)，用于安装和运行后端
- Node.js 24.16.0，与仓库 CI 使用的版本一致
- PostgreSQL 17，可使用已有实例或通过 Docker Compose 启动

下面以 Windows PowerShell 为例。除前端命令外，均在项目根目录执行。

### 1. 安装依赖并配置环境

```powershell
uv sync --frozen --python 3.13
Copy-Item .env.example .env
```

编辑 `.env`：

- 将 `TA_DEV_API_TOKEN` 改成你自己的访问令牌，至少 20 个字符，进入工作台时会用到。
- 如使用已有 PostgreSQL，修改 `TA_DATABASE_URL`，使其指向准备好的数据库。

已有 `.env` 时保留原配置即可。默认使用模拟评估器，不需要模型密钥，可以先体验完整操作流程；模拟结果不代表真实语义评分。

### 2. 启动数据库并初始化

使用仓库提供的数据库配置：

```powershell
docker compose up -d postgres
```

等待数据库就绪后，创建数据表和本地教师身份：

```powershell
uv run --frozen alembic upgrade head
uv run --frozen teaching-assistant bootstrap
```

已有数据库实例时可以跳过 Docker 命令；升级已有数据前请先备份。

### 3. 启动 API 和后台任务

分别打开三个终端，在项目根目录运行以下命令，并保持运行。

API 服务：

```powershell
uv run --frozen uvicorn teaching_assistant.api:create_app --factory --host 127.0.0.1 --port 8000
```

作业初评 Worker：

```powershell
uv run --frozen teaching-assistant worker
```

负责细则起草、修订和补练的 Agent Worker：

```powershell
uv run --frozen teaching-assistant harness-worker --concurrency 2
```

API 启动后可访问 [接口文档](http://127.0.0.1:8000/docs)。

### 4. 启动教师工作台

另开终端：

```powershell
Set-Location .\frontend
npm.cmd ci
npm.cmd run dev
```

打开 [教师工作台](http://127.0.0.1:5173)，输入 `.env` 中的 `TA_DEV_API_TOKEN` 即可进入。前端默认连接本机 `8000` 端口的 API，修改方式见 [前端说明](frontend/README.md#本机启动)。

如果想先看一组示例数据，可以在项目根目录运行：

```powershell
uv run --frozen teaching-assistant demo
```

演示会创建合成作业和答案，完成模拟初评、一次模拟教师复核及导出。

## 接入真实模型

项目已提供 DeepSeek 和 Jev 适配器。DeepSeek 可用于初评、评分细则起草与补练等任务，Jev 用于作业初评。

在 `.env` 中配置对应的模型名称和 API Key，重启 API 与相关 Worker，再在工作台中明确选择真实模型配置。可用配置及参数说明见 [.env.example](.env.example) 和 [模型与接口说明](docs/API_AND_EVALUATION.md)。模型密钥只需配置在后端。

真实模型输出仍需老师复核。已有的公开数据评测、评分质量与耗时结果见 [评测报告](docs/PUBLIC_JEV_EVALUATION_2026-10-30.md)。

## 当前支持范围

- 目前支持文本答案、JSON 和 UTF-8 CSV 导入，暂不支持直接读取 Excel、图片或手写作业。
- 当前使用本地开发令牌进入工作区，尚未提供正式账号登录和学校组织权限管理。
- 补练面向全班，题目来自老师维护的课程题库；检索采用关键词和条件筛选，选题与安排需要老师检查。
- 课程备课 Agent 也提供了 API，目前没有独立的备课页面。

## 技术栈

| 部分 | 技术与用途 |
| --- | --- |
| 教师工作台 | React、TypeScript、Vite、Ant Design |
| 后端 API | Python、FastAPI、Pydantic |
| 数据存储 | PostgreSQL、SQLAlchemy、Alembic |
| 后台任务 | 自研 asyncio Worker，处理批量初评和 Agent 任务，保存进度并支持中断恢复 |
| 模型接入 | DeepSeek、Jev，以及用于本地体验的模拟器 |

## 项目结构

```text
├── frontend/       # 教师工作台
├── backend/        # API、评分逻辑、Agent 与后台任务
├── docs/           # 使用说明、设计文档与评测报告
├── evals/          # 评测相关数据与配置
├── scripts/        # 本地辅助脚本
├── compose.yaml    # PostgreSQL 本地启动配置
└── .env.example    # 环境变量示例
```

想进一步了解，可以阅读 [作业准备说明](docs/ASSIGNMENT_PREPARATION.md)、[评分细则 Agent](docs/RUBRIC_AGENT_2026-10-22.md)、[班级学情分析](docs/CLASS_ANALYSIS_2026-10-22.md) 和 [针对性补练说明](docs/CLASS_PRACTICE_2026-10-28.md)。
