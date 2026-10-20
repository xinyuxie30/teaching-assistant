# 列表接口、Jev 接入与小样本对照

更新：2026-10-20（America/Tijuana）。

本轮按用户要求完成代码，测试由用户执行。**没有运行新接口、测试、迁移或真实模型调用，也没有生成评分质量结论。** 旧版 M0 的 21 项测试通过记录不能作为本轮改动的验证结果。

## 1. 升级与配置

保留已有 `.env`，不要用示例文件覆盖现有数据库地址和开发令牌。启动新版 API、Worker 或演示前，先由你执行：

```powershell
uv run --frozen alembic upgrade head
```

新增迁移 `0003` 创建不可变的 `evaluation_profiles` 表、为现有 `fake-v1` 记录建立关联，并给模型调用增加 request ID、提示哈希、HTTP 状态和可重试标记。没有改写旧初评、复核或导出快照。当前模型代码共对应 24 张表。

真实模型通过现有 HTTPX 依赖接入，不需要增加 TypeSafe SDK 或修改锁文件。选择 HTTP 接口是为了明确每次实际请求和重试边界；后续仍可替换适配器。

在本地 `.env` 中填写：

```dotenv
# 示例是固定版本；应替换成你的账户可用的具体版本。
TA_JEV_MODEL=jev-1.13.0
TA_JEV_API_KEY=你的TypeSafe密钥
TA_JEV_ENDPOINT=https://api.typesafe.ai/v1/systemone
TA_JEV_MAX_INPUT_CHARACTERS=20000
TA_CALL_TIMEOUT_SECONDS=30
TA_LEASE_SECONDS=60
```

`TA_LEASE_SECONDS` 必须比调用超时长超过 2 秒。这里的输入上限是序列化请求的字符数，不是 token 数；超限直接报错，不截断学生答案。普通 HTTPS 端点和本机 HTTP 模拟端点均可配置，路径固定为 `/v1/systemone`，不接受重定向。

API 与 Worker 必须使用相同模型、端点、提示版本、输入上限和超时配置。启动命令见 [README](../README.md)。读取配置：

```powershell
uv run --frozen teaching-assistant profiles
```

也可使用 `GET /v1/evaluation-profiles`。`ready=true` 仅代表本机配置完整，**不代表密钥、模型权限或供应商可达性已经验证**。这些读取操作不会调用模型，也不会返回密钥。

默认评估器仍是 `fake-v1`。配置了 Jev 密钥也不会自动使用真实模型。新建批次时必须显式指定：

```json
{
  "rubric_version_id": "已确认细则的UUID",
  "answer_version_ids": ["已导入答案的UUID"],
  "evaluation_profile": "jev-v1"
}
```

这是 `POST /v1/grading-batches` 的请求体结构示例，需要 Bearer 身份和 `Idempotency-Key`；UUID 占位符须替换。也可将 `evaluation_profile` 设置为配置接口返回的具体 ID，以固定此次请求的目标配置。

`jev-v1` 是当前配置的别名，数据库保存 `jev-<完整SHA256>` 及不含凭据的配置快照。模型、提示或影响请求的配置变化会生成新 ID，从而形成新的评估目标。密钥轮换不改变 ID。同一幂等 key 重放仍返回原回执；需要新目标时使用新的 key。Worker 只领取它能够执行的配置，不会用另一模型替代旧任务；更改配置前应处理已有任务，或保留使用旧配置的 Worker。旧任务可从列表中按配置 ID 查到。

## 2. 列表与详情接口

以下接口均要求老师的 Bearer 身份，范围由数据库所属关系检查。列表参数之外的未知参数返回校验错误；不属于当前老师的父对象或筛选对象返回 404。

全部列表支持：

| 参数 | 语义 |
| --- | --- |
| `limit` | 默认 50，范围 1–100 |
| `cursor` | 上一页返回的 `next_cursor` |
| `created_from` / `created_to` | 含边界的创建时间范围；必须带时区，例如 `2026-10-20T00:00:00Z` |

按 `created_at DESC, id DESC` 排序。游标绑定老师、接口和筛选条件；换筛选条件应清空游标。单次请求的总数和内容来自同一个 REPEATABLE READ 快照，但多次翻页不持有跨请求快照，期间新增或状态变化可能改变总数和筛选结果。需要固定结果时创建导出快照。

统一响应：

```json
{
  "items": [],
  "total": 0,
  "limit": 50,
  "has_more": false,
  "next_cursor": null
}
```

`total` 是当前筛选的完整数量，不是游标后的剩余数量。ID、时间和 Decimal 分数分别按字符串、ISO 时间和十进制字符串返回；尚无分数为 `null`。

| GET 路径 | 额外筛选 |
| --- | --- |
| `/v1/courses` | `q`：课程名称包含文本 |
| `/v1/courses/{course_id}/classrooms` | `q`：班级名称 |
| `/v1/courses/{course_id}/students` | `q`：姓名或外部学号；`classroom_id` |
| `/v1/courses/{course_id}/assignments` | `q`：作业标题；`classroom_id` |
| `/v1/assignments/{assignment_id}/question-versions` | `question_key` |
| `/v1/question-versions/{question_id}/rubric-drafts` | `status=draft\|confirmed` |
| `/v1/question-versions/{question_id}/rubric-versions` | — |
| `/v1/assignments/{assignment_id}/answer-versions` | `question_version_id`、`student_id`、`source_revision` |
| `/v1/assignments/{assignment_id}/imports` | — |
| `/v1/grading-batches` | `course_id`、`assignment_id`、`rubric_version_id`、`status`、`outcome` |
| `/v1/grading-items` | `course_id`、`assignment_id`、`batch_id`、`rubric_version_id`、`student_id`、`status`、`review_status`、`evaluation_profile`、`issue_code` |
| `/v1/exports` | `batch_id`、`mode=working\|confirmed_only` |

`q` 是按字面文本的大小写不敏感包含搜索，不把 `%` 或 `_` 当通配符。不同有效父对象的筛选交集可以为空；不会自动改用该对象下的其他课程或作业。

批次状态为 `queued / running / settled`；结束结果为 `all_succeeded / partial_failure / all_failed`。单项执行状态为 `pending / running / retry_wait / succeeded / failed`；复核状态为 `pending / confirmed`。

常用查询：

```text
GET /v1/grading-items?status=succeeded&review_status=pending&limit=20
GET /v1/grading-items?batch_id=<UUID>&status=failed
GET /v1/grading-items?issue_code=evidence_pending_manual_selection
GET /v1/grading-items?evaluation_profile=<具体配置ID>
```

复核列表返回当前 `review_revision`、`confirmed_score`、`suggested_score`、`needs_review` 和 `simulation`。`review_status=pending` 表示还没有确认记录，可能尚未执行完成；待老师实际处理的完整初评应同时筛选 `status=succeeded`。`evaluation_profile` 筛选使用存储的具体 ID，不解析 `jev-v1` 别名。

新增详情接口：

```text
GET /v1/question-versions/{question_id}
GET /v1/rubric-drafts/{draft_id}
GET /v1/rubric-versions/{rubric_id}
GET /v1/answer-versions/{answer_id}
```

列表仅返回头信息或文本预览，完整原文和固定细则通过详情读取。原有批次、单项与导出详情继续使用；单项详情及新建 JSON 导出增加评估配置快照和供应商调用元数据。

## 3. Jev 执行与人工证据

适配器对应 TypeSafe 的 [Score 文档](https://docs.typesafe.ai/primitives/score)、[模型版本文档](https://docs.typesafe.ai/models) 和 [confidence 文档](https://docs.typesafe.ai/confidence)。本轮再次核对了文档，尚未验证账户级实际响应。

每个独立评分点发送一次 `POST /v1/systemone`。请求只含当前题干、参考资料、答案、确认的总规则和当前评分点的等级文字；不发送学生身份、历史表现、其他答案或对照集标注。等级顺序映射到 0 起始的供应商序号，分值仍由本地固定细则掌握。

返回后检查模型版本、唯一问题、等级描述映射、分布键、概率范围与总和、Score 范围及其与分布的一致性。候选等级取唯一最大概率项，并列则弃判。学生分数由程序按候选等级映射到 Decimal 分值后计算，连续 Score 和 confidence 不参与计分。

适配器没有隐藏重试。Worker 根据错误分类、总调用预算和 attempt 上限决定是否重试；429、暂时服务异常和超时可重试，鉴权失败、模型版本不匹配、输入超预算等直接进入失败。尊重 `Retry-After` 并使用有限退避。调用中断可能已产生费用，因此记录为 `unknown`，不能承诺供应商恰好执行一次。

供应商返回的 token 用量、请求与返回模型版本、request ID、提示哈希、延迟、HTTP 状态及错误代码会保留。不持久化供应商原始错误正文；缺失用量记录为未知，不能按零费用解释。每份答案的 `max_calls` 仍是适配步骤预算，包含本地证据占位步骤，不等于真实 HTTP 次数。

**Jev Score 不返回原答案引用。** 当前通过 `manual-evidence-v1` 生成明确的空证据占位，不声称“没有证据就是答案没有表达”。初评带有：

```text
requires_teacher_review
no_citation
evidence_pending_manual_selection
```

老师复核仍需逐点提交等级、理由和证据支持判断：

- `supported`：必须提供能够在该版本原答案中精确定位的 `citations`。
- `absence_confirmed`：老师确认答案未表达该评分点，引用清空。
- `teacher_judgment`：老师明确作出判断并写理由；不伪装成自动支持性验证。

引用形如 `{"quote":"原文片段","start":0,"end":4}`，采用 Python Unicode code point 的 `[start,end)`，不是 UTF-8 字节或 JavaScript UTF-16 下标。程序检查片段存在性，老师检查片段是否支持等级。全部初评均需老师确认；没有用 confidence 自动发布成绩。

## 4. 小样本对照命令

命令不依赖运行中的 API、Worker 或数据库；从本地读取固定数据，按需调用模型，逐项保存报告。样例 [synthetic-v1.json](../evals/samples/synthetic-v1.json) 包含完整回答、部分回答、空答案和带指令的答案，共 4 份、每份 2 个评分点。标签是工程合成标签，不是老师实测。

先查看调用计划。此模式不要求密钥，不写报告、不调用模型；选择 Jev 时仍需配置固定 `TA_JEV_MODEL`：

```powershell
uv run --frozen teaching-assistant compare --dataset evals/samples/synthetic-v1.json --profiles fake-v1 jev-v1 --max-samples 4 --max-calls 16 --dry-run
```

配置密钥后，由你决定何时执行真实请求：

```powershell
uv run --frozen teaching-assistant compare --dataset evals/samples/synthetic-v1.json --profiles fake-v1 jev-v1 --max-samples 4 --max-calls 16 --max-seconds 180 --max-attempts 1
```

该计划最多进行 16 次适配器调用，其中 8 次是 Jev 外部请求；证据提取不在此对照范围内。默认每点评估 1 次，不为失败自动增加费用；`--max-attempts` 可显式设为 1–3，仍受 `--max-calls` 和总执行时间限制。时间预算包括调用循环中的报告写入和重试等待，不包括开始前的数据加载。

输出位置默认为 `.local/comparisons/<run_id>/`：

| 文件 | 内容 |
| --- | --- |
| `dataset.snapshot.json` | 此次实际选取的题目、细则、答案和本地标注 |
| `report.json` | 配置快照、输入哈希、逐点分布、候选等级、每次调用、错误及汇总 |

报告每步写入临时文件再替换。可正常处理的中断会标记 `interrupted`；调用结果不明保留 `unknown`。强制终止可能留下 `running/dispatched`，需要人工核对供应商侧记录。再次执行会建立新 run，不会自动恢复或重用不明请求。

汇总包含有效覆盖量、等级一致率、分项与总分 MAE/偏差、完整答案数、失败和弃判数、高置信度误判明细，以及配置之间共同有效评分点的差异。失败、弃判和预算未执行项不算零分；总分仅对所有评分点有效的答案计算。必须连同分母、覆盖率、标签来源和 split 阅读指标。

`--confidence-audit-threshold` 默认 0.9，只用于列出高置信度误判，不是复核阈值或自动确认规则。严重总分偏差的分值线可通过 `--severe-error-points` 显式给出，默认不预设。

价格不写死。需要估算时同时提供 `--input-usd-per-million`、`--output-usd-per-million` 和 `--price-source`。报告只估算已返回用量的费用，并列出用量未知的尝试；这不是账单结算。延迟为小样本串行调用的描述性统计，不能作为并发性能结论。

真实数据可按样例结构放到已忽略的 `evals/private/` 中。每个文件固定一题和一个细则版本；每个样本有唯一 `sample_id`、`answer_version_id`、未改写的 `answer_text` 和逐点 `gold_levels`。真实老师确认的标签使用 `label_status=teacher_confirmed` 并注明 `label_source`；调参与最终留出集分别使用 `split=calibration / holdout`，不要把同一答案混入两者。`unlabeled` 数据不得填写 gold。

当前真实适配器只有 Jev；`fake-v1` 是流程对照，不能充当普通生成式模型基线。因此这一步能对照固定标注、发现分项偏差和高置信度错误，**尚不能得出 Jev 相对普通模型的质量、成本或老师复核收益结论**。普通模型、自动证据提取和人工耗时评测仍待后续实现。

## 5. 代码入口与待验证范围

- `listing_api.py` / `listings.py`：路由、权限范围、筛选、游标和轻量查询。
- `profiles.py` / `jev.py`：固定配置、HTTP 适配、返回契约及错误分类。
- `worker.py`：按配置领取、调用回执、有限重试和人工证据占位。
- `comparison.py` / `cli.py`：样本契约、预算、运行报告和偏差指标。
- `0003_profiles_and_provider_metadata.py`：新增结构与旧 fake 配置的兼容迁移。

本轮只做人工代码审阅与代码格式化。没有运行静态检查、迁移、既有测试或新功能验证。后续由你验证列表分页及身份隔离、升级与新库迁移、模型响应契约、失败重试、旧执行回写、人工引用与导出一致性，并使用真实老师标注样本评估质量。
