# 当前列表接口与模型对照入口

2026-10-21：根据现有源码整理的索引，接替已删除的 `LISTS_AND_MODEL.md` 使用说明。没有恢复已删除文件或迁移其缺失的验证记录。历史运行结果见 [后端报告](TEST_REPORT_2026-10-20.md)，修复与已执行的浏览器回归见 [审查处理记录](TEST_REVIEW_2026-10-21.md)。

## 列表与详情接口

鉴权、写操作和从空环境启动方式见 [根 README](../README.md)。以下路径均使用 `GET /v1` 前缀，按当前老师授权范围查询，不调用模型。

列表返回 `items / total / limit / has_more / next_cursor`，以创建时间及 ID 倒序分页。通用参数为 `limit / cursor / created_from / created_to`；时间需要为 ISO 8601 格式并包含偏移量。继续下一页时保留原筛选，改变筛选后从第一页开始；游标不可跨老师、对象范围或筛选条件复用。

| 路径（省略 `/v1`） | 额外筛选 |
| --- | --- |
| `/courses` | `q` |
| `/courses/{course_id}/classrooms` | `q` |
| `/courses/{course_id}/students` | `q`、`classroom_id` |
| `/courses/{course_id}/assignments` | `q`、`classroom_id` |
| `/assignments/{assignment_id}/question-versions` | `question_key` |
| `/question-versions/{question_id}/rubric-drafts` | `status=draft / confirmed` |
| `/question-versions/{question_id}/rubric-versions` | 无 |
| `/assignments/{assignment_id}/answer-versions` | `question_version_id`、`student_id`、`source_revision` |
| `/assignments/{assignment_id}/imports` | 无 |
| `/grading-batches` | `course_id`、`assignment_id`、`rubric_version_id`、`status`、`outcome` |
| `/grading-items` | `course_id`、`assignment_id`、`batch_id`、`rubric_version_id`、`student_id`、`status`、`review_status`、`evaluation_profile`、`issue_code` |
| `/exports` | `batch_id`、`mode=working / confirmed_only` |

批次状态为 `queued / running / settled`，终态 outcome 为 `all_succeeded / partial_failure / all_failed`，不能只以 `settled` 判断成功。单项执行状态与复核状态独立；待老师复核查询为 `/grading-items?status=succeeded&review_status=pending`。

详情路径为 `/assignments/{id}`、`/question-versions/{id}`、`/rubric-drafts/{id}`、`/rubric-versions/{id}`、`/answer-versions/{id}`、`/grading-batches/{id}`、`/grading-items/{id}`、`/exports/{id}`。作业详情的名单取自创建作业时固定的成员关系；评分及导出引用具体版本，不能拿列表最新一行代替它们。

字段约束以 [schemas.py](../backend/src/teaching_assistant/schemas.py)、[listing_api.py](../backend/src/teaching_assistant/listing_api.py) 和 [api.py](../backend/src/teaching_assistant/api.py) 为准。

## 班级学情分析（2026-10-22）

新增 `GET /v1/grading-batches/{id}/analysis` 和 `GET /v1/grading-batches/{id}/analysis/students`。前者返回固定题目/细则范围、覆盖计数、老师确认的均分/分布、分项等级人数及 `basis_hash`；后者返回可回看原答案的分页明细，支持 `status=confirmed / pending_review / failed / in_progress`、`criterion_id`、`below_full_credit` 和 `basis_hash`。分页时间筛选针对初评项的创建时间，不表示按复核时间重算概览。

所有统计只用本批次所选答案的当前老师复核，不跨规则或答案版本聚合。每次请求使用 `REPEATABLE READ`；前端把概览 `basis_hash` 传给明细，来源变化返回 `409 / analysis_changed`，要求重新读取概览。`below_full_credit` 必须指定评分点，评分点筛选只适用于确认结果；越权 404，错误参数 422。契约与边界见 [班级分析交付](CLASS_ANALYSIS_2026-10-22.md)。

## 评分细则 Agent（2026-10-22）

新增 `GET /v1/agent-profiles`、`POST /v1/question-versions/{id}/rubric-agent-runs`、`GET /v1/agent-runs`、`GET /v1/agent-runs/{id}`、`GET /v1/agent-runs/{id}/events` 与 `POST /v1/agent-runs/{id}/cancel`。当前需先迁移至 `0006`，运行独立 `teaching-assistant agent-worker`；作业准备页已有对应面板。列表支持 `question_version_id / status / created_from / created_to / limit / cursor`，游标绑定老师和筛选，返回通用分页结构；事件按 sequence 增量读取。详见 [任务历史与页面恢复](AGENT_HISTORY_2026-10-22.md)。

创建任务时可传同题目版本下的 `source_draft_id` 和显式非空 `instructions`，基于该草稿快照与老师意见生成新版；原稿及已确认版本保持原状。任务返回 `source_draft`（id / revision / content_hash），详情另含 `revision_feedback`，列表省略反馈全文。省略来源字段仍是普通起草，兼容原创建请求的幂等回执。示例、错误与验证见 [Agent 修订](AGENT_REVISION_2026-10-22.md)。

来源可为手动草稿或 Agent 草稿，也可已确认；前端草稿详情现有“让 Agent 修改这份草稿”入口，复用上述接口。`GET /v1/rubric-drafts/{id}` 的 `revision_comparison` 返回来源快照、意见、当前目标摘要和结构差异；普通草稿为 `null`，“有来源但未变化”则返回 `difference.has_changes: false`，两者含义不同。该读取不调用模型或修改草稿。详见 [差异契约](AGENT_REVISION_DIFF_2026-10-22.md) 与 [直接修订入口](AGENT_DRAFT_ENTRY_2026-10-22.md)。

Agent 仅生成未确认草稿，默认模拟器；DeepSeek 需显式选择。保存后等待老师，原有细则确认接口会在同一事务内将对应任务改为完成。详细请求、配置、事务与恢复边界见 [Agent 说明](RUBRIC_AGENT_2026-10-22.md)。

## 模型配置与对照

默认 `fake-v1` 是工程模拟器，始终选择最低等级，不做语义评分。Jev 适配器实现于 [jev.py](../backend/src/teaching_assistant/jev.py)，使用 HTTPX 请求 TypeSafe Score。[deepseek.py](../backend/src/teaching_assistant/deepseek.py) 提供普通模型基线和可选证据提取，已有模拟供应商回归，尚未验证真实 DeepSeek；配置及完整边界见 [DeepSeek 与评分 Runtime](DEEPSEEK_AND_GRADING_RUNTIME.md)。

新增 `GET /v1/grading-items/{item_id}/trace`，在同一数据库快照中返回版本/profile、预算、等待条件、尝试、调用与步骤检查点。使用相同 Bearer 身份和老师对象范围，越权 404；这是只读接口，不执行或重试任务。

2026-10-21 报告审查修复后，trace 的 `calls` 和 item 详情的 `execution.calls` 均返回可空 `provider_fingerprint`；compare 失败 attempt 也保留已收到的指纹。新增 `0004` 迁移，需先升级数据库再运行新版服务；本轮未执行迁移或测试。旧成功检查点本就有指纹，迁移只复制有依据的历史值，详情见 [审查与验收](DEEPSEEK_TEST_REVIEW_2026-10-21.md)。

Jev 配置字段包括 `TA_JEV_API_KEY`、固定版本 `TA_JEV_MODEL=jev-x.y.z`、可选 `TA_JEV_ENDPOINT` 和 `TA_JEV_PROXY`。密钥只放后端配置，不写入 profile、前端或报告。实际可用配置可通过 `GET /v1/evaluation-profiles` 或 `teaching-assistant profiles` 查看；设置密钥不会更改默认模拟器。

模型、提示、适配器和证据策略等构成不可变 manifest 与具体 `jev-<hash>` ID。API 与 Worker 需使用匹配配置，真实批次显式选择对应 ID；旧配置任务不会自动换用新模型。Jev 连续 Score 不直接转换为成绩，程序依据候选等级和老师确认的分值计分，引用由老师补充并判断支持性。

以下命令用于查看配置与输入计划，不产生真实调用：

```powershell
uv run --frozen teaching-assistant profiles
uv run --frozen teaching-assistant compare --dataset evals/samples/synthetic-v1.json --profiles fake-v1 --max-samples 1 --dry-run
```

需要真实对照时，由执行者选定已配置 profile、固定数据集，并明确 `--max-samples / --max-calls / --max-seconds / --max-attempts`。`compare` 只写本地对照报告，不生成业务成绩；教师标签不发送给模型。输出默认位于 `.local/comparisons/<run-id>/`，命令参数见 [cli.py](../backend/src/teaching_assistant/cli.py)。

单个真实模型的费用估算需同时提供 `--input-usd-per-million`、`--output-usd-per-million` 与 `--price-source`，来源说明应包含适用日期，可加 `--cached-input-usd-per-million`。多个真实模型使用 `--pricing-file` 按 profile 分别配置，拒绝直接共用全局价格。缓存分项或失败调用用量未知时保留不完整标识，不回填 0。compare 只测等级判断，不包含证据调用和老师复核成本。

2026-10-21 实际执行的真实对照（`run_id=587d30421db04f2d871a91619aeb5fae`）：profile `jev-b4bd3af…`，1 份合成答案、2 个评分点，2/2 成功，1298 input / 38 output tokens，建议分 10.00，单次延迟 3.27 s；`estimated_usd_for_reported_usage` 仍为 null、`cost_is_complete=false`。同日浏览器全链路也以该 profile 跑通，4 次 `evaluate` 全部 HTTP 200，request id 与 usage 写入 `model_calls`。这些只证明接入与派生计分可用，不是教学准确性结论。

历史真实 compare 同样只有一份合成答案、两个评分点，不能据此推断教学准确性、优于普通模型或节约老师时间。DeepSeek 对照代码已写入；真实教师标注集、对照执行及完整成本评估仍待实施。


## 通用 Harness（2026-10-22）

已抽出共享任务运行时与 typed ToolRegistry，并增加课程级教学方案适配器。任务目录、profile选择、创建、产物读取/人工确认、迁移 `0007` 及示例见 [Harness API 与边界](HARNESS_CORE_2026-10-22.md#api-与运行)。原细则起草和确认接口兼容，`agent-runs` 新增 `task_type` 字段与过滤；非题目任务的 `question_version_id` 为 null。


## Harness v2：压缩与子任务（2026-10-22）

该阶段迁移为 `0008`；新增 `lesson-plan-team` profile、`waiting_children`状态、`agent-runs/{id}/children` 与 `/context-checkpoints`，运行详情包含父/根ID、深度及根任务树预算。子任务仅能由注册工具创建，不能通过创建接口绕过父权限。历史示例和兼容策略见 [压缩与持久子Agent调度](HARNESS_CONTEXT_AND_SUBAGENTS_2026-10-22.md)。

## Harness v3：摘要、并行与等待（2026-10-22）

该阶段迁移头为 `0009`。新增 `lesson-plan-parallel` 根任务、`lesson-practice-designer` 和 `lesson-review` 内部任务；最多两层、同树两个运行中的子任务，摘要和各层调用共享根预算。新增 `POST /v1/agent-runs/{root_id}/wait`、`/wake` 及 `GET /v1/agent-runs/{root_id}/waits`；所有写入需幂等键与当前老师身份。暂停释放租约、冻结剩余执行时间，唤醒只接受当前 token，保留已用预算。详细请求、旧配置恢复规则和迁移步骤见 [Harness v3](HARNESS_V3_2026-10-22.md)。

## Harness v4：主动问答、课程知识与并行执行（2026-10-22）

该阶段迁移头为 `0010`。新增 `lesson-plan-assisted` 根任务和 `fake-assisted-lesson-agent-v1` / `deepseek-assisted-lesson-agent-v1` 配置。模型只能查询同课程的已确认有效知识，`ask_teacher` 形成 `wait.question`；老师通过原 `/wake` 接口提交非空原文回答。答案以原工具调用的结果交接，不重复计工具预算，也不代替成果确认。新增 `/v1/courses/{course_id}/teaching-memories` 创建/检索和 `/{memory_id}` 详情、`/confirm`、`/revoke` 接口；写入均需幂等键。Worker 新增 `--concurrency 1..16`。完整契约、示例、兼容边界见 [Harness v4](HARNESS_V4_2026-10-22.md)。

## Harness v5：引用、预算、后台汇合与事件续传（2026-10-22）

当前迁移头为 `0012`。新增 `lesson-plan-research` 与 `lesson-plan-background`，可选择对应 `fake-` 或 `deepseek-` 配置；课程创建入口不变。前者支持不可变大结果分页回读，后者支持 spawn、非阻塞输出查询及持久 join。创建请求可附 `max_total_tokens` 和字符串金额 `max_cost_usd`，整树共享预留；`GET /v1/agent-runs/{id}/budget` 查看已知用量、未知预留和固定价格核算。新 `GET /v1/agent-runs/{id}/events/stream` 支持 `after`、`Last-Event-ID` 与 0–60 秒连接上限，每页重新鉴权。具体形状与边界见 [Harness v5](HARNESS_V5_2026-10-22.md)。

真实验收命令 `uv run --frozen python backend/tools/verify_harness_real.py` 会产生付费调用。默认最多 36 次、0.30 美元固定峰值计价额度，四场景各分配四分之一；只允许官方 DeepSeek 端点及 `_test` 库随机 schema，使用合成材料，产物保持未确认。调用、事件、上下文、产物和费用报告保存在 `.local/e2e/harness-v5-real/<id>/`；历史是合成 fixture 的摘要场景会明确标记，失败也先存证再清理。真实执行结果见 [实施记录](IMPLEMENTATION_STATUS.md)，该脚本不是教学效果评测。

## 多进程恢复验收（2026-10-23）

`backend/tools/verify_runtime_stress.py` 可复测多 Worker 持续负载、进程强杀和私有数据库连接断开，使用真实 PostgreSQL、合成数据及模拟模型，无付费调用。新建 Harness 配置的子任务调用上限为 5 次，仍受父任务和整树总预算限制；原 v5 及更早配置继续按已冻结额度恢复。具体命令、检查项和实测边界见 [持续压测记录](RUNTIME_STRESS_2026-10-23.md)。

`backend/tools/verify_harness_recovery_real.py` 进一步使用真实 DeepSeek API 验证三个独立 Worker、子 Agent 并行、进程强杀、数据库断连、未知调用与重复执行。默认跨所有进程最多 36 次调用、0.30 美元固定峰值计价额度；调用前持久预留，未知回执不释放额度，更换 Worker 不重置上限。`--offline` 可先无付费预演。真实模型回执未持久化时仍按未知记账；只有已提交的回执可直接复用。相同上下文的未知重试可能在供应商侧再次执行，但不能重复已提交的有效工具结果或产物。28 次真实请求、预算对账、故障恢复与复测命令见 [真实恢复验收](HARNESS_REAL_RECOVERY_2026-10-23.md)。
