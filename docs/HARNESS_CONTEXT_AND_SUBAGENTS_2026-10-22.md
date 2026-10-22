# Harness：上下文压缩与持久子 Agent 调度

本文保留 v2 阶段的实现与验收记录。后续已完成模型摘要、多子任务并行汇总、两层委派及持久等待/唤醒，当前能力和迁移入口见 [Harness v3](HARNESS_V3_2026-10-22.md)。

用户要求继续参考本地 Claude Code 源码，优先实现上下文压缩和子 Agent 调度。本轮两项能力都已接入实际 Worker；旧业务界面保持原流程。新能力通过任务目录、运行状态、子任务和压缩检查点 API 可检查。

## 上下文压缩

`harness/compaction.py` 在完整供应商请求超过字符预算时生成一个较小的模型输入投影，原始 `AgentRun.messages` 保持完整。它只成组移除旧的失败工具交互，或与后续成功读取完全重复的交互；工具名、参数和输出的摘要都必须一致才能判定重复读取。

系统提示、全部用户指令、唯一的教学事实、成功写入和委派结果、未完成工具组以及最近两组交互原样保留。一个 assistant 发起多个工具时，将该 assistant 和所有结果视为同一组，不能只移除其中一条。压缩说明作为标明不可信历史数据的 assistant 消息，包含调用 ID、工具名、结果状态及哈希，不把工具正文升级为系统指令。

触发预算同时考虑请求消息、工具 schema 和其他请求字段，目标为预算的 85%。压缩不足以安全容纳请求时，仍返回 `agent_context_budget_exceeded`，不付费尝试超限请求，也不摘要替换固定教学事实。字符预算是当前适配器的约束，不等于精确 token 预算。

`harness/context_store.py` 在模型派发同一事务中保存 `context_compacted` 事件和不可变 `agent_context_checkpoints`。检查点记录原始历史前缀哈希、投影哈希、移除范围、调用摘要及压缩前后字符数；恢复时从原始历史重建并核对，检查点不一致则停止。重复调用 ID 检查继续扫描完整历史。模拟器调用 ID 改为请求摘要，避免压缩改变消息长度后复用旧 ID。

这是确定性的保真历史压缩，不包含模型生成的语义摘要、Claude 的缓存编辑协议或全部 auto-compact 策略。需要保留的事实本身超限时，不会强行压缩。

## 子 Agent 调度

新增 `lesson-plan-team` 父任务和 `lesson-practice` 子任务作为实际验证场景：父任务读取固定简报，调用 `request_practice_agent`；子任务独立读取同一授权快照，生成结构化练习；父任务接收真实产物回执，将其整合进教学方案，最终等待教师确认。

`harness/scheduler.py` 负责创建、继承约束和取消，`HarnessWorker` 负责等待与结果交接：

1. 委派工具只有代码注册的 handler 能返回 `ChildTaskRequest`。模型不能指定教师身份、资源范围、供应商或额外工具。
2. 创建子任务、增加子任务计数、保留父 pending 调用、父进入 `waiting_children` 并释放租约，在同一事务保存。
3. 子任务拥有独立 system、用户要求和工具历史；继承父任务完整且固定的授权输入快照，不复制父对话或压缩检查点。子任务只开放读取简报和保存练习的工具。
4. 子任务完成后，父任务核对教师、根任务、输入、允许的类型、数据库产物、哈希与未确认状态，再为原始 `tool_call_id` 生成唯一结果回执，更新父状态并恢复运行。失败或取消也有对应错误回执。
5. 父任务的正常续跑不消耗故障重试次数；重启不能重复创建子任务或重复交接结果。交接过程失败只回滚父状态，已保存的子产物保留。

子任务的 `completed` 表示内部草稿已经可交接，`confirmed_at` 仍为 null。它不能通过教师确认 API 独立确认；父方案仍要求教师明确确认。这个内部分工没有自动发布或修改评分结果的权限。

## 预算、取消与边界

新运行配置固定以下策略：

| 约束 | 首版值或语义 |
| --- | --- |
| 子任务深度 | 最多 1 层，子任务没有委派工具 |
| 每棵树累计子任务数 | 最多 2 个，失败后重新委派也计数 |
| 每棵树同时活动子任务数 | 最多 1 个；不同根任务可以并发 |
| 子任务局部调用预算 | 模型最多 3 次、工具最多 3 次，且不超过根任务限制 |
| 整树预算 | 所有父子模型/工具调用共同使用根任务原有 `max_model_calls` / `max_tool_calls` |
| 截止时间 | 子任务不能晚于父任务和根任务 |
| 取消 | 根取消原子取消所有活动子任务，关闭 pending/inflight，增加 epoch 阻断晚到响应 |

所有树写入统一先锁根任务、再锁目标任务；模型 HTTP 请求不持锁。调用在发出前扣预算，失败与未知结果不返还额度。全树预算耗尽或根截止时间到达后，树停止且取消活动后代。共享预算当前计调用次数，不是价格或精确 token 额度。

`AgentRun` 新增父/根 ID、父工具调用 ID、深度、树预算计数与恢复标记；`(parent_run_id, parent_tool_call_id)` 唯一，父子关系由数据库触发器禁止修改。运行状态新增 `waiting_children`。只读 API 中根任务的树计数统计整棵树，子任务的树计数字段为 null，局部计数仍独立可查。

## 使用入口

新版需要迁移到 `0008`；现有业务库本轮没有升级。执行迁移前保留备份，升级后重启 API 和 `harness-worker`。旧 `agent-worker` 命令仍兼容。含子任务、等待子任务的运行或压缩检查点时，降级拒绝执行，避免丢失持久状态。

| API | 用途 |
| --- | --- |
| `GET /v1/harness/tasks` | 查看目录、工具契约、子任务能力与仅内部任务标记 |
| `GET /v1/agent-profiles?task_type=lesson-plan-team` | 查看父任务模拟和 DeepSeek 配置 |
| `POST /v1/courses/{course_id}/lesson-agent-runs` | 选择 team profile 创建父任务；需要 `Idempotency-Key` |
| `GET /v1/agent-runs/{id}/children` | 查询当前老师可见的直接子任务 |
| `GET /v1/agent-runs/{id}/context-checkpoints` | 查询压缩范围、哈希和字符数；不暴露内部完整对话 |
| `GET /v1/agent-runs/{id}`、`/{id}/events` | 查看等待、交接、局部与整树调用情况 |
| `POST /v1/agent-runs/{id}/cancel` | 取消父树或单独活动子任务 |

创建示例：

```json
{
  "agent_profile": "fake-lesson-team-agent-v1",
  "instructions": "起草十分钟讲解与练习方案，由子助手提供练习草稿。",
  "brief": {
    "topic": "版本与可追溯性",
    "objectives": ["说明为什么要保留历史版本"],
    "duration_minutes": 10
  },
  "max_model_calls": 6,
  "max_tool_calls": 10
}
```

默认模拟成功路径为父 3 次模型/工具调用、子 2 次，共 5/5；原来的 `fake-lesson-agent-v1` 仍走独立的 2/2 流程。子产物包含 `practice` 和固定教学目标，父工具会验证练习内容确实来自交接回执。没有新增浏览器入口；可通过已有 API 文档使用这些接口。

新 policy 使用 `harness-runtime-v2`，固定压缩和调度版本/阈值。已有 v1 Harness 与更早的 rubric policy 继续按原工具和 exact-context 规则恢复，不悄悄换模型或扩大权限。

## Claude Code 参考与自主实现

路径相对于本地 `claude-code-main/` 参考树；只读资料，没有执行参考项目。

| 参考代码 | 借鉴点 |
| --- | --- |
| `src/query.ts:367`、`:412`，`src/services/compact/microCompact.ts` | 请求前处理上下文投影，保留近期工具结果，区分轻量压缩与自动摘要 |
| `src/tools/AgentTool/runAgent.ts:367`、`:866` | 可控上下文继承、过滤未配对工具记录 |
| 同文件 `:520`、`:687`、`:751` | 取消域、将递归约束放在运行配置中、复用主 query 循环与调用上限 |
| `src/tools/AgentTool/agentToolUtils.ts:597` | 先确定任务终态，再处理通知 |
| `src/tasks/LocalAgentTask/LocalAgentTask.tsx:197`、`:281`、`:486` | 通知去重和父子取消连接 |

本项目采用 PostgreSQL 持久等待、事务性交接、不可变压缩检查点和根任务共享调用预算，是适应教学数据与恢复要求的自主设计；不声称参考源码提供完全相同的数据库协议，也不声称已完整复现 Claude Code。

## 验证

新增测试覆盖压缩保真/配对/重建、检查点不可变及派发原子性、全历史调用 ID、任务隔离、共享预算、并发领取、取消树、父失败交接、过期清理和迁移保护。专项与全量结果见 [工程实施记录](IMPLEMENTATION_STATUS.md)。测试只使用隔离 `_test` schema、fake 模型和 HTTP mock，没有真实模型调用。

全量后端 **232 passed**（新增 68 项）、Ruff 与 85 文件格式检查通过；前端 **60 passed**、类型与构建通过；现有浏览器教学流程 **9/9 阶段、0 错误**。报告 `.local/e2e/harness-v2-full.xml`；浏览器回归覆盖原细则起草/修订、评分、复核、导出和分析，新父子任务由 PostgreSQL/API 集成测试验证。

仍未实现多层递归、同树多个活动子 Agent、自由任务图、任意工具/文件执行、模型摘要式压缩、产品心跳或跨日人工等待唤醒。语义质量和真实费用需要独立评测，不能由模拟链路推断。
