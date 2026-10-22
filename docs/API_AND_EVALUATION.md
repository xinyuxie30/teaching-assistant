# 当前列表接口与模型对照入口

2026-10-21：根据现有源码整理的索引，接替已删除的 `LISTS_AND_MODEL.md` 使用说明。没有恢复已删除文件或迁移其缺失的验证记录。历史运行结果见 [后端报告](TEST_REPORT_2026-10-20.md)，修复与已执行的浏览器回归见 [审查处理记录](TEST_REVIEW_2026-10-21.md)。

## 列表与详情接口

鉴权、写操作和从空环境启动方式见 [根 README](../README.md)。以下路径均使用 `GET /v1` 前缀，按当前老师授权范围查询，不调用模型。

列表返回 `items / total / limit / has_more / next_cursor`，以创建时间及 ID 倒序分页。通用参数为 `limit / cursor / created_from / created_to`；时间需要带时区。继续下一页时保留原筛选，改变筛选后从第一页开始；游标不可跨老师、对象范围或筛选条件复用。

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

## 评分细则 Agent（2026-10-22）

新增 `GET /v1/agent-profiles`、`POST /v1/question-versions/{id}/rubric-agent-runs`、`GET /v1/agent-runs`、`GET /v1/agent-runs/{id}`、`GET /v1/agent-runs/{id}/events` 与 `POST /v1/agent-runs/{id}/cancel`。需先迁移至 `0005`，运行独立 `teaching-assistant agent-worker`。列表仅返回当前老师最近任务，支持 limit，尚无通用游标；事件按 sequence 增量读取。

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
