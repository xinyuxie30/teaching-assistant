# 通用 Harness 内核第一轮

本文件记录第一轮状态。后续已经实现确定性上下文压缩和持久子Agent调度，最新迁移及限制见 [Harness v2](HARNESS_CONTEXT_AND_SUBAGENTS_2026-10-22.md)。

用户明确本项目的重点是 Harness。本轮将持久模型循环从评分细则业务中抽出，并用不绑定题目的课程教学方案验证复用。新增能力仅提供后端 API，没有增加业务页面。

## 内核与任务边界

| 层 | 代码 | 责任 |
| --- | --- | --- |
| 持久运行时 | `backend/src/teaching_assistant/harness/worker.py` | 领取与租约、epoch 围栏、模型调用预算预留、超时与取消、恢复、成对工具回执、有界停止修复 |
| 工具契约 | `harness/tools.py` | 代码注册的工具白名单、严格 JSON、输入与输出 schema、版本、效果类型、工具超时 |
| 任务契约与目录 | `harness/tasks.py`、`harness/catalog.py` | 系统提示、资源授权、工具前置条件、持久状态变更、实际产物检查、版本和配置哈希 |
| 上下文与模型 | `harness/context.py`、`harness/models.py` | 精确保留消息，按完整请求检查字符预算，单次供应商请求与结构化回执 |
| 持久事件 | `harness/store.py` | 调用开始、成功、失败、未知结果、跳过与终态事件；提前停止时关闭剩余工具调用 |
| 任务适配器 | `harness/rubric.py`、`harness/lesson.py` | 分别定义题目细则与课程教学方案的工具、权限和产物规则 |

表中简写路径均相对于 `backend/src/teaching_assistant/`。`agent/worker.py`、`agent/models.py` 等保留兼容入口，现有细则 API 和教师确认流程继续工作。固定分项评分 Worker 仍独立存在，不会因为模型工具调用而改变已经确认的评分规则。

新增任务无需修改持久循环：实现 `TaskDefinition`，注册 typed tools、任务与模型 profile，再提供经过服务器授权的创建入口。当前两个任务都是“保存草稿 → 等待老师”的模式；任意任务图和后台自主完成策略仍未实现。

## 运行不变量

- 身份与资源范围由服务器绑定。任务的硬授权与前置条件先于扩展 Hook；Hook 只能拒绝，修改工具输出也会被拒绝。
- 工具写入、输出校验及状态归约在 savepoint 中；失败回滚后重新读取 ORM 状态，再保存错误回执。有效效果、状态、回执和等待教师的转换共同提交。
- 终止判断检查数据库中的实际产物。模型声称完成、返回一个 ID 或宽松 Hook 都不能代替检查；缺少产物只允许一次补救。
- 调用发出前占用预算，失联结果保留为 unknown；不承诺外部模型调用恰好一次。旧 epoch 或取消后的响应不能提交效果。
- 新 profile 固定任务、系统提示、输入/输出契约、工具版本/效果/超时、上下文、Hook 和停止策略版本。恢复时检查 profile 与任务类型；旧细则 policy 仅在完全匹配时继续兼容。
- 迁移 `0007` 新增任务类型、输入快照、运行状态和独立产物。任务类型/输入及产物内容由数据库触发器保护；产物只允许首次确认时间更新。已有非细则任务或产物时，降级会拒绝执行，避免丢失记录。

## API 与运行

使用 `uv run --frozen teaching-assistant harness-worker` 处理目录中的任务，支持 `--once`、`--drain`；旧 `agent-worker` 命令仍可用。启动新版 API/Worker 前需要将目标数据库迁移到 `0007`。本轮只在隔离测试 schema 执行迁移，没有迁移已有业务库。

| 请求 | 用途 |
| --- | --- |
| `GET /v1/harness/tasks` | 查看任务和工具契约 |
| `GET /v1/agent-profiles?task_type=lesson-plan` | 查看第二种任务的模拟/DeepSeek配置；省略类型仍返回细则配置 |
| `POST /v1/courses/{course_id}/lesson-agent-runs` | 创建课程任务，需 `Idempotency-Key` |
| `GET /v1/agent-runs?task_type=lesson-plan` | 查找本老师的任务 |
| `GET /v1/agent-runs/{id}`、`/{id}/events` | 查看状态与调用回执 |
| `POST /v1/agent-runs/{id}/cancel` | 取消排队或运行中的任务 |
| `GET /v1/agent-runs/{id}/artifact` | 读取教学方案草稿和内容哈希 |
| `POST /v1/agent-runs/{id}/confirm` | 教师显式确认所读哈希，需 `Idempotency-Key`；不存在模型确认工具 |

教学方案创建示例：

```json
{
  "agent_profile": "fake-lesson-agent-v1",
  "instructions": "起草一份十分钟讲解与补练方案。",
  "brief": {
    "topic": "版本与可追溯性",
    "objectives": ["区分内容版本和当前状态", "说明为何保留历史记录"],
    "duration_minutes": 10
  }
}
```

模型先调用 `read_lesson_brief`，再调用 `propose_lesson_plan`。服务器要求目标及顺序与固定简报一致、各环节分钟数相加等于指定时长；产物包含活动、练习题和参考答案。默认 fake 只验证工程流程，文本明确标记模拟。教师确认请求为 `{"content_hash":"读取产物得到的64位哈希"}`，只有等待复核的有效产物能完成确认，旧哈希返回 409。

## 与 Claude Code 参考的关系

本轮重新核对了本地参考树的 `src/Tool.ts`（schema、验证和权限）、`src/services/tools/toolExecution.ts`（执行与 Hook 顺序）、`src/query.ts` 及 `src/query/stopHooks.ts`（停止反馈与续跑）、`src/context.ts`（系统/用户上下文）。采用这些机制的设计思路，以 Python 和 PostgreSQL 自主实现，未引入 Claude Agent SDK，也不声称与 Claude Code 源码完整等价。本地参考树是恢复源码资料，不视为官方发布的 SDK。

## 验证与边界

新增回归覆盖第二任务完整流程、教师隔离、幂等确认、不可变输入/产物、非法工具参数/输出、Hook 篡改、状态归约失败回滚、预算、有界停止、取消、保存调用恢复、旧策略兼容以及迁移降级保护。实际全量验证结果记录于 [工程实施记录](IMPLEMENTATION_STATUS.md)。测试仅使用模拟模型、HTTP mock 和专用 `_test` 数据库的随机 schema。

本轮后端 **164 passed**（新增 core 18 项、lesson 12 项），Ruff 及 73 文件格式检查通过；前端 **60 passed**、类型与构建通过；现有浏览器流程 **9/9 阶段、0 错误**，再次验证旧细则起草/修订/确认、分项评分、复核、导出和班级分析。后端报告：`.local/e2e/harness-full.xml`。

目前仍缺少上下文压缩/摘要的保真协议、长期记忆、子任务和子 Agent 调度、共享预算与取消传播、跨日等待/唤醒及产品心跳。课程任务尚未接入班级分析或学生答案证据，也没有教学方案编辑 UI。教学质量和真实费用不由这些工程测试证明。

后续继续优先建设 Harness：先定义上下文分层、保留固定教学事实与工具配对的压缩恢复协议，再实现具有预算归属和取消传播的子任务；业务流程作为验证这些能力的适配器。
