# DeepSeek 基线与固定评分 Runtime

2026-10-22 更新：指纹修复后端回归 **79 passed**，Ruff/格式及独立测试 schema 的 `0004` 迁移一致性检查通过；业务库尚未迁移，真实 DeepSeek 未调用。详见 [最新测试记录](FINGERPRINT_VERIFICATION_2026-10-22.md)。下文的未验证表述属于此前交付记录。

2026-10-21 首版交付后，用户报告记载工程验证通过（见文末历史记录），DeepSeek 使用本地 HTTP 桩，未调用真实供应商。同日审查再次修复调用指纹的记录链并新增 `0004` 迁移，尚未执行迁移或重跑验证；没有修改本机 `.env` 或新增依赖。当前升级与验收要求见 [审查修复说明](DEEPSEEK_TEST_REVIEW_2026-10-21.md)，历史通过数不覆盖本轮修复。

## 实现范围

- `deepseek.py`：普通生成式模型结构化初评，接入 API 批次、Worker 和 compare。
- `grading_context.py`：版本化评分上下文，只包含当前题目、参考、答案及已确认评分点；不传学生身份、历史表现、教师评测标签。
- `grading_runtime.py`：白名单步骤 `evaluate → extract_evidence`。预检全部评分点后再预留调用额度；输入超限停止，不截断答案。模型只提供结果数据，没有任意工具调用、改规则、确认或发布成绩的权限。
- Worker 继续负责事务、租约 epoch、预算、检查点、重试与旧执行回写拦截。两步分别保存；提取失败后可复用已成功的等级判断。证据模型调用的 usage/request id 与评分调用一样落库。
- `GET /v1/grading-items/{item_id}/trace`：老师范围内、同一数据库快照的执行详情，包括版本、profile、预算、等待条件、attempts、calls、checkpoints 和计划。越权返回 404；不返回密钥或完整请求提示。检查点可能含原文引用，仍属于该老师的数据。

这仍是明确可恢复的评分工作流。尚未实现通用自主规划、任意工具循环、跨作业教学 Agent、长期记忆、子 Agent、心跳或通知。

## 配置和显式选用

在本地配置中自行填写密钥；字段样例见根目录 `.env.example`：

```dotenv
TA_DEEPSEEK_MODEL=deepseek-flash
TA_DEEPSEEK_API_KEY=<自行填写>
TA_DEEPSEEK_ENDPOINT=https://api.deepseek.com/chat/completions
TA_DEEPSEEK_REVISION=pilot-2026-10-21
TA_DEEPSEEK_EVIDENCE_MODE=manual
TA_DEEPSEEK_MAX_INPUT_CHARACTERS=40000
TA_DEEPSEEK_MAX_OUTPUT_TOKENS=2048
TA_CALL_TIMEOUT_SECONDS=30
TA_LEASE_SECONDS=60
```

模型名称以账号实际可用模型为准。这里的 revision 是实验标记，不是供应商承诺的权重版本。请求与响应模型名默认要求相同；供应商返回另一具体名称时，可以显式设置 `TA_DEEPSEEK_EXPECTED_MODEL`，不能静默忽略差异。别名背后的权重变化仍无法仅凭名称检测。业务调用表、item/trace 调用列表和 compare attempt 统一记录收到的 `system_fingerprint` 为 `provider_fingerprint`，包含已解析响应但输出校验失败的情况；缺失时为 null。成功检查点也保留该信息。trace 的 `profile.fingerprint` 是配置哈希，不能充当供应商指纹；二者均不能单独证明模型权重不可变。

`deepseek-v1` 是配置入口别名，创建批次时解析为 `deepseek-<hash>`。模型名、预期返回名、endpoint、revision、提示版本/哈希、证据策略、输入/输出上限和超时进入 manifest；密钥和代理不进入。改配置产生新 profile，旧任务需匹配旧 profile 的 Worker，不能自动换模型。API 与 Worker 应同时更新配置。输入限制是序列化请求的字符数上限，并非精确 token 估算。

默认仍为 `fake-v1`。配置密钥不会自动启用付费评分。通过 `GET /v1/evaluation-profiles` 查看可用配置，网页的批次配置列表按接口数据加载；选择具体 DeepSeek profile，或 API 建批次显式传 `evaluation_profile: "deepseek-v1"`。代理可选 `TA_DEEPSEEK_PROXY`，仅影响该模型 HTTP 请求。HTTP 客户端不读取环境代理，也不跟随重定向；无内部隐藏重试。

## 输出与证据边界

采用非流式 Chat Completions、JSON object 输出、显式关闭 thinking。等级判断只接受：

```json
{"criterion_id":"当前评分点 ID","level_id":"已确认的等级 ID"}
```

`level_id` 可为 null，表示弃权，建议总分保持空值；不能当作零分。拒绝额外字段、错误评分点、未知等级、重复 JSON key、非有限数值、截断/拒绝/工具调用结果。JSON 模式不保证符合业务契约，适配器仍做校验。DeepSeek 不伪造概率分布或 confidence；程序依据老师已确认的等级分值计算成绩。

证据模式可选：

| 模式 | 行为 | 调用量（不含重试） |
| --- | --- | --- |
| `manual`（默认） | 空引用，由老师选取；用于先与 Jev 独立比较等级判断 | 每评分点 1 次 HTTP + 1 次本地证据步骤 |
| `model` | 独立第二次 DeepSeek 请求提取原文片段，不传前一步候选等级 | 每评分点 2 次 HTTP |

提取结果为 `criterion_id` 与 `quotes: [{quote, occurrence}]`。程序按 occurrence（从 0 开始，重复文本计入重叠匹配）在当前原答案中定位 Unicode code point 偏移，不接受模型直接给的偏移。保留空格、换行和 emoji；不做文本规范化。找不到片段、重复引用或非法 occurrence 会失败，成功证据绑定答案版本和哈希。空列表不表示已证实缺失知识点。

片段存在仅证明引用准确，不证明片段支持等级判断。所有初评仍需老师确认，支持性状态仍为 `requires_teacher_review`。两个请求也不代表两个独立模型给出了相互验证。

## 停止、恢复与预算

- 全部输入预检在本份答案第一次预留调用之前完成。无效 profile、输入超限、输出截断、认证错误或模型名不符会终止该次可执行流程；修改语义配置应创建新 profile/批次。
- 429、服务暂不可用、超时和可重试契约错误，仍由 Worker 进行有上限的持久化重试，遵守 Retry-After。已保存步骤通过固定输入哈希复用。
- 原有每份答案最多 3 次自动 attempt、每评分点 6 次适配器调用的默认额度继续生效。预算计数包括本地证据步骤；它不等于付费 HTTP 数，更不是美元预算。老师手动重试沿用现有显式授权流程。
- 超时或崩溃可能已经被供应商计费；无法保证外部请求 exactly-once。租约隔离保证旧实例不能覆盖有效业务结果，unknown 调用保留未知用量。需要货币硬上限时，还需供应商限额或另建预留策略，本轮没有声称实现。
- trace 的 waiting_for 表示持久化状态要求的下一条件，不证明当前有活跃 Worker，也不创建定时任务。

## 小样本对照与计价

先使用同一题目、固定细则和答案比较候选等级；教师标签只用于报告。`compare` 只执行 `evaluate`，即使 profile 开启 model 证据模式，也不测证据质量或成本，不产生业务成绩。

以下是用户可执行的命令模板；不要将历史桩验证当作当前配置的运行证明：

```powershell
uv run --frozen teaching-assistant compare --dataset evals/samples/synthetic-v1.json --profiles jev-v1 deepseek-v1 --max-samples 1 --max-calls 4 --max-seconds 180 --max-attempts 1 --dry-run
# 去掉 --dry-run 才会真实调用两个已配置供应商；合成样本仅验证接入，不证明教学效果。
```

多真实模型不能共用旧全局单价参数。新增 `--pricing-file <路径>`，JSON 对象的键为选中的真实 profile ID 或别名，值包含：

- `input_usd_per_million`、`output_usd_per_million`：必填，非负十进制值。
- `cached_input_usd_per_million`：可选，启用时 input 单价对应未命中输入；缺少缓存分项用量则该次费用不可完整估算。
- `source`：必填计价出处和适用条件；`effective_date`：可选 `YYYY-MM-DD`。

文件解析为具体 profile ID 后写入报告，不在线抓价格、不内置单价。不填写的模型保留费用未知。单个真实模型仍支持 `--input-usd-per-million`、`--output-usd-per-million`、`--price-source`，可加 `--cached-input-usd-per-million`。不提供缓存单价时明确按填写的统一输入单价估算，不声称采用供应商实际缓存优惠。

估算仅覆盖已报告且可计价的用量。未知用量或缺失价格保留不完整标识，不能当作完整账单。不得将一次成功评分的成本当作含失败、重试、证据和人工复核的全流程成本。

## 已写入的回归用例（已运行）

`test_deepseek.py` 覆盖输出契约、弃权、HTTP 错误、模型名/截断/工具请求、精确引用、预检和 profile；`test_deepseek_worker.py` 覆盖证据失败后新 Worker 复用评分检查点、证据用量落库、trace 权限和零调用预检失败；`test_comparison_deepseek.py` 覆盖实际对照入口的模拟供应商、调用预算、标签隔离和多模型/缓存计价。新增用例均使用模拟供应商，PostgreSQL 用例使用既有独立测试库和隔离 schema。2026-10-21 实测全量 **72 passed**，其中 DeepSeek 专项 27 项、Worker 专项 3 项。

复跑命令：

```powershell
uv run --frozen pytest backend/tests/test_deepseek.py backend/tests/test_comparison_deepseek.py
# 已配置独立 TA_TEST_DATABASE_URL 时，再执行全部后端回归：
uv run --frozen pytest
```

真实教学质量、证据支持性、DeepSeek 延迟/成本与老师复核耗时仍需老师标注的独立评测集。下一步先完成这批代码的工程验收，再进行同样本 Jev/DeepSeek 比较及人工复核策略评估；之后再推进知识点、订正及跨次作业的数据关联。

## 执行验证（2026-10-21，指纹修复前的用户报告）

| 项目 | 结果 |
| --- | --- |
| `ruff check` / `format --check` | 首次失败：`model_http.py` ASYNC109、11 个文件未格式化；修复后通过 |
| `alembic check` | 无待生成操作，未新增迁移 |
| `pytest -q` | **72 passed**；DeepSeek 专项 27、Worker 专项 3 |
| profile 状态 | 未配置 → `id: null`、`ready: false`；配置桩后 ready，证据策略 `deepseek-quotes-v1` |
| 无配置建批次 | `409 model_not_configured`，未产生评分项 |
| 生产链路（本地桩） | 4 次调用全 200，证据定位 `(0, 6)`，建议 10.00，trace 预算 4/12 |
| trace 授权 | 随机 item 404；其他老师 404；响应不含密钥或完整提示 |
| 预检零调用 | 45,000 字符 → `model_input_budget_exceeded`，`calls_used: 0`，无调用记录 |
| compare 计价 | 缓存分项 `0.000258`、Jev `0.0001374`，与手算一致 |
| 模型名不符 | `provider_model_version_mismatch`，2/2 失败，分数 null |

审查更正：原版成功检查点已经保存供应商指纹，并在 trace 的 checkpoints 中返回；并非只有 compare 输出。原调用表缺列及失败响应丢失指纹的问题已在本轮代码修复，需升级 `0004` 并重新验收。上表“无新增迁移”和 72 passed 仅为修复前记录。两份费用均按合成验证价计算，不是供应商实际收费。

边界：本机无 DeepSeek 密钥，未验证供应商真实 JSON 合规率、延迟、限流与真实价格；已有 PostgreSQL 集成测试覆盖新建 Worker 对象恢复，但尚无 DeepSeek 跨进程证据恢复验证；教学质量未评估。详细记录见 `.local/e2e/deepseek-verification-2026-10-21.md`。

## 本轮核验的官方接口

2026-10-21 阅读 [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/) 和 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。它们是接口实现依据，不是本工程的测试通过证明；后续供应商变化需重新审查配置与适配器。
