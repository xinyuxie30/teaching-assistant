# 参考资料与接口审查

更新：本文保留原始设计审查。2026-10-22 后续已参考本地源码机制实现通用 Harness、摘要压缩、并行与多层子 Agent、等待/问答和确认课程知识。v5 进一步补齐大结果引用、token/费用预留、后台查询/join、Hook 快照及 SSE，并完成真实 DeepSeek 四场景验收。当前源码映射、明确差异和验证边界见 [Harness v5](HARNESS_V5_2026-10-22.md)；下文未调用模型等表述只描述原始审查阶段。

审查日期：2026-10-19。
方式：官方文档核验和定向静态源码阅读。没有运行参考项目、调用真实模型或进行性能测试。

后续更新（2026-10-20）：当前工程已有最小后端，并新增列表与 Jev 适配代码。下文是开始实施前的参考审查；不表示当前目录仍只有设计。新增适配再次核对 [Score](https://docs.typesafe.ai/primitives/score)、[模型版本](https://docs.typesafe.ai/models) 与 [confidence](https://docs.typesafe.ai/confidence) 官方文档，采用 HTTPX 直接对接固定版本的 System One 请求，没有复制参考项目源码，当时没有进行账户级调用。当前说明已迁至 [API 与模型对照](API_AND_EVALUATION.md)；后续账户级测试见 [测试报告](TEST_REPORT_2026-10-20.md)。

## 1. 现状与来源

2026-10-22 补充：按用户继续实现 Agent 的要求，再次定向阅读内嵌 `src` 的 `Tool.ts`、`services/tools/toolExecution.ts`、`context.ts` 与 `query.ts`。已在自主 Python 实现中落地类型化工具、受限 Hook、分层上下文、请求预算、调用结果配对和有限 Stop 修正；另由 PostgreSQL 承担持久恢复。参考位置、实际范围及代码映射见 [评分细则 Agent](RUBRIC_AGENT_2026-10-22.md)。下表仍保留首次审查时的状态，不能作为当前工程进展。

| 资产 | 实际状态 | 对本项目的作用 |
| --- | --- | --- |
| local-perks 中两份教学助手文档 | 规划记录，范围与本次需求基本一致 | 需求依据；不意味着该仓库已有教学助手 |
| 当前教学助手工作区 | 审查开始时为空 | 本轮仅保存设计文档 |
| learn-claude-code | Python 教学示例及扩展章节 | 理解循环、工具、Hook、上下文和恢复的教学材料 |
| 其中的 `claude-code-main/src` | 存在真实可读的 TypeScript 源码树 | 本轮重点分析的上下文、Hook、工具执行参考 |
| EchoMind ZIP | 客服问答 Agent 的 Python、Java、Vue 前端和文档 | 仅借鉴部分工具结果、超时、回执和评测处理思路 |

内嵌 `claude-code-main/README.md` 自述这是从 source map 恢复的源码树，并称其不是官方发布或受支持 SDK。这是该文件的来源声明，本轮未独立验证其与某个正式发行版本的完整一致性。不能把外层教学项目的 MIT 文件直接当作这份内嵌源码的授权证明。

EchoMind 归档中，本轮排除 `.venv`、`.mvn` 等依赖目录后，未找到项目级 LICENSE/COPYING/NOTICE 文件。本轮只提炼设计，不复制实现；后续若要复用或发布具体代码，需核实对应来源与许可。

用户随后进一步限定：EchoMind 只能作为局部实现思路的参考。因此本方案不把它列为代码复用来源，也不以客服问答、意图路由或客服角色分工来决定教学助手的领域架构。

## 2. Jev 官方接口核验

### 已核实的接口事实

| 项目 | 文档所示 | 本项目设计含义 |
| --- | --- | --- |
| 定位 | 对 `state` 中的数据回答带类型的问题 | 按评分点做判断；不当作自由生成文本的接口 |
| Python | `typesafe-sdk`，`typesafe_sdk.AsyncTypeSafeClient` / `TypeSafeClient` | 异步 Worker 可直接使用官方 Client |
| 调用 | `system_one(state=..., questions=..., model=...)`；HTTP `POST /v1/systemone` | 适配器负责协议转换；运行时负责状态和恢复 |
| Score | `criteria` 为 2–10 个有序等级，每级有独立文字描述 | 明确写出各级含义，不能仅传“0/1/2” |
| 返回 | Score 包含 `score`、`probabilities`、`legend`、`confidence` | 保存完整分布和元数据；候选等级与学生分数由本项目规则产生 |
| 索引 | HTTP 分布键为字符串；Python SDK 的等级键为整数 | 适配器统一转换为内部稳定的 `level_id` |
| 证据 | 所核验的 Score 响应没有答案引用片段字段 | 必须另做证据提取与校验 |

依据：[Introduction](https://docs.typesafe.ai/introduction)、[Python SDK](https://docs.typesafe.ai/sdk/python)、[Score](https://docs.typesafe.ai/primitives/score)、[System One API](https://docs.typesafe.ai/api/system-one)。

`score` 是等级序号的概率加权位置，不是老师定义的分值。题目 ID/问题键只用于关联返回，等级描述需要自身完整，不能依赖“比上一级更好”来定义含义。以上约束来自 [Score 文档](https://docs.typesafe.ai/primitives/score)。

confidence 是输出分布的统计量。Score 的统计还考虑概率相对最可能等级的距离，不能简单当成最高概率，更不能当成真实评分正确率。本项目保留原值，不用统一的任意阈值直接免复核。[Confidence](https://docs.typesafe.ai/confidence)

SDK 提供模型列表、模型选择、`RetryPolicy` 和带状态/请求 ID 的错误；文档默认模型别名为 `jev-latest`。评测应先核实账户可用的明确版本，记录请求模型及服务返回模型，不把别名当成固定版本。SDK 重试与 Harness 重试必须合并预算，避免层层重试。[SDK 用法](https://docs.typesafe.ai/sdk/python/usage/basic)

### 不能从文档推导的结论

- 中文学科简答题的准确性、严重误判率、证据支持质量。
- 使用 Jev 后老师复核时间是否减少。
- 加上生成式证据提取后，相比普通模型是否更便宜或更快。
- 本账户可用模型、限流、准确请求上限、实际价格与网络稳定性。
- 固定模型名是否完全固定服务端行为；仍需保存请求与返回，标注供应商可复现性边界。

文档示例中的模型版本不是本轮已经验证可调用的版本。补查的 SDK 用法页含晚于本会话日期的媒体功能说明；该内容未用于文本首版能力承诺。真实接入仍须做账户级契约测试。

### 建议的分工

JevScoreEvaluator 输出一个评分点的结构化判断；GenerativeEvaluator 提供同输入的对照初评；EvidenceExtractor 独立提取引用；程序验证位置与格式；老师确认引用是否支持结论。普通生成式模型若不提供可解释来源的分布，其 `probabilities` 和 `provider_confidence` 留空，不能伪造为 Jev 的同类指标。

这部分是本项目的设计选择，不是 TypeSafe 声称提供的整套评分流程。

## 3. 重点：恢复源码中的上下文、Hook 与 tool_use

源码根目录：`claude-code-main`。

| 阅读位置 | 实际观察 | 拟迁移到教学助手的约束 |
| --- | --- | --- |
| `context.ts:116`、`context.ts:155` | 分开组织系统上下文与用户上下文，缓存上下文资料 | 评分输入包固定题目、细则、材料和答案版本；权限与配置不由答案提供 |
| `query.ts:365` | 在压缩边界后取消息，先处理工具结果预算，再处理历史裁剪和压缩 | 工具大结果存为产物引用；先限制上下文，再发请求 |
| `microcompact.ts:253` | 根据时间、调用来源、模型能力和功能开关选择压缩路径 | 不照搬某一供应商的缓存编辑机制；按适配器能力处理 |
| `autoCompact.ts:32`、`autoCompact.ts:51` | 预留输出空间，跟踪压缩失败次数并设置停止边界 | 预算包含输出与恢复开销；失败不能触发无限压缩/重试 |
| `Tool.ts:379`、`Tool.ts:489` | 工具有 schema、执行接口、并发属性、业务校验和权限检查 | ToolSpec 分离描述、参数校验、授权、执行和回执 |
| `toolExecution.ts:615`、`toolExecution.ts:800`、`toolExecution.ts:921` | 可见 schema 校验、工具业务校验、PreToolUse、权限决策的执行路径 | 核心校验必须是程序代码；Hook 改写参数后在本项目中重新校验和授权 |
| `toolHooks.ts:332` | Hook 的 allow 仍受 deny/ask 规则约束 | 扩展 Hook 不得放宽课程权限或绕过老师确认 |
| `hooksConfigSnapshot.ts:95` | 捕获配置快照，有显式更新路径 | 每次执行固定 workflow/policy/hook 版本，恢复不悄悄更换 |
| `toolOrchestration.ts:91` | 按经过校验的输入和 `isConcurrencySafe` 组织工具批次 | 不把所有工具一律并行；同一成绩的写入必须事务串行化 |
| `query.ts:1011` | 中断时补全缺失的工具结果，维持调用与返回匹配 | 取消、超时、失败也是明确结果；工具调用不能无记录消失 |
| `query.ts:1258` | API 错误与 Stop Hook 的路径分离，避免错误—阻止结束—重试循环 | 模型停止与业务验收分离；停止检查受预算和最大修复次数约束 |

这些是从所读分支中观察到的机制，不意味着所有功能开关都启用，也不证明此源码树可以独立构建。源码中的性能注释不作为本项目的测量结果。

本项目有一个关键差异：本次评分的原答案和已确认评分规则不能被摘要替换。可以压缩对话历史和解释材料；不能靠有损摘要完成计分。上下文装不下时应阻塞并明确处理，不静默截断答案。

首版借鉴以上协议与边界，保留代码注册的生命周期扩展点；暂不开放用户自定义 Shell/HTTP Hook，不需要复制 CLI、终端权限弹窗或通用文件操作工具。

## 4. learn-claude-code 教学代码

- `s02_tool_use/code.py:163`：读取 `tool_use`，按注册表分派，并带同一 `tool_use_id` 回传结果。适合理解协议，不能直接用该无限循环作为教学批改任务状态机。
- `s04_hooks/code.py:224`：通过 PreToolUse/PostToolUse/Stop 扩展循环。教学助手应把强制权限留在不可绕过的执行链，而不是仅靠可替换 Hook。
- `s08_context_compact/code.py:5`：演示结果落盘、历史裁剪、压缩及超限恢复。可借鉴“摘要 + 原文引用”，不用于替代评分原文。
- `s16_workflow_runtime/code.py:329`：JSONL journal、语义键和恢复缓存实际存在；不是只有 README 设计。
- `s16_workflow_runtime/code.py:379`：有预算对象和调用限制。该本地 journal/锁方案不能直接提供 PostgreSQL 事务、跨 Worker 租约或业务幂等，需重新实现。

## 5. EchoMind 实际实现

归档：`EchoMind ZIP`。
下表路径相对于参考归档内的项目根目录，不依赖个人文件名或本机存放位置。

| 归档内路径与行号 | 实际实现 | 借鉴或差距 |
| --- | --- | --- |
| `EchoMind/agents/agent_orchestrator.py:319` | `_call_llm` 有最多三轮工具循环、调用超时、角色工具白名单、参数校验、工具 trace | 可参考有界循环与错误记录；三轮不是本项目的通用正确参数 |
| 同文件 `:392` | 对非 retry-safe 工具记录请求内操作，并允许执行前回调 | 有副作用意识，但请求内集合本身不能替代持久操作回执 |
| `EchoMind/agents/tools.py:117`、`:135`、`:167` | 排障工具返回步骤，账单工具明确不连接支付系统，交接工具返回摘要 | 不能把“生成建议/摘要”写成“已经执行外部业务” |
| `EchoMind/tooling/tool_manager.py:176`、`:446` | 参数校验、缓存、熔断、超时、可选重排；缓存包含上下文和版本，防止旧请求回填缓存 | 参考统一结果和错误分类；身份必须由服务端绑定，不能让模型伪造缓存范围 |
| `EchoMind/tooling/knowledge_base.py:140`、`:188` | Chroma 查询和返回文档 ID/版本；所读 handler 未将 context 传入查询过滤 | 资料可追溯值得借鉴；教师/课程范围过滤必须在本项目检索层明确实现 |
| `EchoMind/memory/conversation_memory.py:138`、`:155` | 进程锁 + Redis 租约；Redis transaction 原子保存回合、状态和回执，部分数据有 TTL | 可参考回执及原子提交思想；教学事实必须改由 PostgreSQL 保存 |
| `EchoMind/core/conversation_state.py:5` | 合并业务实体并记录来源 request_id | 当前任务上下文应有来源；不能把字段齐全当作身份授权 |
| `EchoMind/evaluation/jobs.py:8` | 任务在字典和 `asyncio.create_task` 中运行；关闭时取消 | 这是进程内任务组织，无法直接满足批改服务重启恢复 |
| `EchoMind/evaluation/evaluator.py:49`、`:94` | Judge 失败可返回空评分，并区分评测错误数与有效样本数 | 借鉴“不把基础设施失败算作低质量答案”；教学金标准仍需老师标注 |
| `EchoMindJava/.../agent/BaseAgent.java:27` | 该入口构造提示并调用一次 `llmGateway.chat` | 不能假设 Java 与 Python 已具备完全一致的工具循环 |
| `EchoMindJava/.../tool/KnowledgeToolManager.java:180` | fallback 是带错误标记的解释性检索结果 | 本项目要区分查到证据与系统降级提示 |
| `EchoMindFrontend/package.json:12`、`src/lib/evaluationJobs.js:1` | 前端是 Vue + Vite；评测状态有轮询逻辑 | 用户草案 React 是新选型，不是直接沿用已有 React 前端 |

没有把参考项目中的 Redis/Chroma、通用客服路由和多 Agent 结构整体搬入教学助手。首先复用边界设计，具体代码与领域模型重新实现。

## 6. 基础设施结论的官方依据

- PostgreSQL 明确说明 `SKIP LOCKED` 可用于多个消费者访问队列表，但它产生跳过锁定行的视图，不适合一般一致性报表。拟只用于任务领取；成绩与导出采用正常事务查询。[SELECT](https://www.postgresql.org/docs/current/sql-select.html)
- SQLAlchemy 的 `AsyncSession` 代表有状态事务，不应被多个并发 asyncio 任务共享。每个 Worker 活动及短事务使用独立 session。[asyncio 文档](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-asyncsession-with-concurrent-tasks)
- Celery 的确认机制和重投递仍要求业务幂等，`acks_late` 也不等于所有进程退出场景都会自动重投。引入 Celery 后仍需数据库回执和扫描恢复。[Tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
- FastAPI 的 `BackgroundTasks` 用于响应后的进程内工作；本项目持久批改采用独立 Worker 和数据库记录，是根据恢复要求作出的架构选择。[Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)

## 7. 审查定位

本轮可以给出“工程可实施”和“接口形状适配”的结论。还不能给出“教学评分质量合格”“Jev 优于普通模型”“已实现持久恢复”等结论。

便于复查的本地文件 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `claude-code-main/src/query.ts` | `74E0CE0D86CFD453ADD8DC1D15CCB6311B02964B8321E3721B8E71FBD87252CE` |
| `claude-code-main/src/Tool.ts` | `29D555AB8E50DE9FB005264B5334ABA870F1DA472198E14D0B3AB0BF86D7C49C` |
| `s16_workflow_runtime/code.py` | `C6AEA09FE2ABDB09681F291F44D404499C432239E68901B2753BC7C64E8D810E` |
