# Agent 历史查询与页面恢复

日期：2026-10-22。用户在 Agent 实现过程中要求继续。本轮先对齐工作区后续提交，保留已有 DeepSeek 修复、网页入口、CSV 导入及令牌轮换，再处理页面恢复缺口。

## 已完成

- 新页面会话从 PostgreSQL 找回当前题目版本的最近任务，可以翻页选择历史任务；不会自动新建或调用模型。
- 创建请求丢失响应后，重试仍使用相同内容和 `Idempotency-Key`，成功后接回原任务。已处理的回执不会在重新进入面板时覆盖老师选择的历史任务。
- 事件区依据 `event_sequence` 查询最近八条，解决原先固定查询前 100 条导致最新状态不可见的问题。
- 创建或取消结果未确定时，禁用历史切换和相关新写操作；列表、详情、事件读取失败可见并可重试。
- 首次领取显示“重领 0 次”；上下文超限文案说明后续请求停止，不再误称此前没有调用模型。

代码：[Agent 查询服务](../backend/src/teaching_assistant/agent/service.py)、[查询契约](../backend/src/teaching_assistant/agent/contracts.py)、[Agent 面板](../frontend/src/features/preparation/AgentPanel.tsx)。

## 列表接口

`GET /v1/agent-runs` 保持 `items` 兼容，新增以下参数和通用分页字段：

| 参数 | 规则 |
| --- | --- |
| `question_version_id` | 可选；检查当前老师对题目版本的权限，越权 404 |
| `status` | 可选；queued / running / waiting_teacher / completed / failed / cancelled |
| `limit` | 默认 20，范围 1–100 |
| `cursor` | 使用上一页 `next_cursor`；不可跨老师或筛选条件复用 |
| `created_from / created_to` | 可选；ISO 8601 格式（含偏移量）的创建时间范围 |

返回 `items / total / limit / has_more / next_cursor`，使用创建时间和 ID 倒序游标。单次请求的页数据、总数及摘要在同一 REPEATABLE READ 快照读取；不同页面请求之间任务状态可以变化。列表不返回 `messages / pending_calls`。

网页当前提供按题目的历史分页；状态和时间筛选有 API 契约，尚无额外网页控件。老师仍需进入对应题目；没有跨浏览器保存身份或自动恢复路由。

## 实际验证

| 检查 | 结果 |
| --- | --- |
| 后端全量 pytest | **112 passed，0 failures/errors/skipped，73.51 秒** |
| Ruff / 格式检查 | 通过，51 个文件 |
| 前端 Vitest | **37 passed，7 个文件** |
| TypeScript / Vite 生产构建 | 通过 |

新增后端 2 项覆盖范围隔离、筛选、同时间戳分页、跨筛选/老师游标拒绝及摘要边界。新增前端 3 项使用真实 React 面板、hooks 和 HTTP 客户端，配合 mock fetch 与基础控件，覆盖新会话恢复、未知提交重试、历史选择保留及尾部事件查询。未重跑真实浏览器 E2E 或布局验收。

后端测试只用 `_test` 数据库随机 schema。本轮没有新增真实模型调用、修改 `.env`、操作业务数据、增加迁移、提交或推送。此前业务库 `0005` 与真实 DeepSeek 验证保留为历史记录，没有重复执行。

本机结果见 [JUnit](../.local/e2e/agent-history-regression-2026-10-22.xml)；可移植回归为 [test_agent.py](../backend/tests/test_agent.py) 和 [agent-panel.test.ts](../frontend/src/features/preparation/agent-panel.test.ts)。
