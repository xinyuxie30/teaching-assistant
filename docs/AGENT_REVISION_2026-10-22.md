# 老师反馈驱动的 Agent 细则修订

日期：2026-10-22。用户要求按照当前情况继续开发。本轮在已有任务历史与恢复代码上，补齐老师提出修改意见、Agent 基于原稿生成新版、老师独立确认的流程。

## 页面操作

1. 在作业准备页打开固定题目，选择“用 Agent 起草”。
2. 当前任务生成草稿后，打开草稿检查，在“对当前草稿的修改意见”填写要求，点击“按修改意见生成新版”。已经确认的草稿也可以作为下一版的来源。
3. 新任务保存后会显示来源草稿和本次意见，生成的草稿仍需点击“打开草稿并确认”后明确确认。

原稿、原任务和旧固定版本保留。原任务如果仍在等待老师确认，会继续保持该状态；老师可以从历史分别查看各次草稿。每次修订是独立任务，具有自己的有限预算、取消与恢复状态。确认新版只完成关联的新任务。

修改意见按当前来源任务暂存在页面会话中，切换历史不会串用意见。提交成功后，意见及来源快照持久保存在 PostgreSQL，新页面会话可以找回；未提交的输入仍遵循现有页面会话策略，刷新会丢失。

创建或取消结果未知时，面板锁定新起草、修订、历史切换和打开草稿操作，重试使用原内容与原 `Idempotency-Key`。来源说明、事件及输入在桌面和 390 px 宽度下均已检查。

## API 与持久化

仍使用 `POST /v1/question-versions/{question_version_id}/rubric-agent-runs`，例如：

```json
{
  "agent_profile": "fake-rubric-agent-v1",
  "source_draft_id": "替换为同题目版本的草稿 UUID",
  "instructions": "把第二个评分点拆为两个独立评分点，细化部分得分条件，保持总分不变。"
}
```

`source_draft_id` 可省略；提供来源时必须显式提供非空、最多 4000 字符的 `instructions`。接口支持同题目下手动或 Agent 生成的草稿，以及已经确认过的草稿；网页当前从选中的 Agent 任务草稿发起修订。

迁移 [0006](../backend/migrations/versions/0006_agent_rubric_revision.py) 为 `agent_runs` 增加 `source_draft_id` 外键和 `revision_context` JSONB，约束两者同时有值或同时为空。创建事务锁定来源草稿并保存：

- 原稿正文、修订号与正文摘要。
- 老师修改意见和输入格式版本 `rubric-revision-context-v1`。
- 用上述快照构造的模型上下文。

完整原稿与意见参与创建时的上下文预算预检；过大则在登记任务及调用模型前返回错误。执行和恢复使用保存的上下文；模型继续先通过 `read_question` 获取固定题目，再通过 `propose_rubric` 写入新的未确认草稿。工具权限、老师确认入口、状态机及原任务的用量记录保持原有规则。

任务响应中的 `source_draft` 返回 `id / revision / content_hash`；详情另含 `revision_feedback`，分页摘要不返回反馈正文或完整原稿。创建事件与审计保存来源摘要，完整快照保存在任务中。

| 情况 | 行为 |
| --- | --- |
| 来源不存在、跨老师或跨题目版本 | 404，不创建任务 |
| 缺少明确修改意见、意见为空或超长 | 422，不创建任务 |
| 完整原稿超过上下文预算 | 422 `agent_context_budget_exceeded`，无模型调用 |
| 同 key、同来源及同意见重试 | 返回原创建回执；当前状态通过详情读取 |
| 同 key 改来源或意见 | 409 `idempotency_conflict` |
| 取消新版或确认新版 | 仅改变该新任务；原稿与旧任务保留 |

普通起草请求在计算幂等哈希时省略空来源字段，保持升级前的请求哈希；旧创建回执仍能重放。

## 升级与验证

启用新代码前对目标数据库执行 `uv run --frozen alembic upgrade head`，升级到 `0006` 后重启 API 和 Worker。交付时仅对 `_test` 数据库中的随机隔离 schema 操作；复验时已备份并升级本机业务库到 `0006`，见 [审查与复验](AGENT_REVISION_REVIEW_2026-10-22.md)。`.env` 未修改。旧业务库的 `0005` 验证属于此前记录。

| 检查 | 实际结果 |
| --- | --- |
| 全量后端 pytest | **118 passed，83.17 秒，0 skipped** |
| 新增修订后端回归 | **6 passed**，覆盖未确认/已确认原稿、新版独立确认、幂等、快照、取消、权限和预算 |
| Ruff / 格式检查 | 通过，53 个文件 |
| 前端 Vitest | **40 passed，7 个文件**；新增 3 项真实面板/hooks/客户端测试 |
| TypeScript / 生产构建 | 通过，无体积警告 |
| 浏览器业务流程 | **7/7 断言阶段，0 浏览器错误**；模拟起草—反馈修订—确认—导入—初评—复核—两种范围导出 |
| 布局 | 检查桌面及 390 px 界面截图，窄屏无页面横向溢出 |
| 迁移 | 隔离 schema 升至 `0006`；含旧任务的 `0006 → 0005 → 0006` 往返及 Alembic 一致性检查通过 |

后端沙箱无法启动 asyncio 事件循环，经工具授权在沙箱外运行同一隔离测试。浏览器使用临时回环 API、模拟 Worker 和生产预览页，结束后清理临时进程与 schema。临时运行器的 Windows GBK 日志输出问题已修正为 UTF-8；业务断言未放宽。

可移植回归：[test_agent_revision.py](../backend/tests/test_agent_revision.py)、[agent-panel.test.ts](../frontend/src/features/preparation/agent-panel.test.ts)、[workflow.test.mjs](../frontend/e2e/workflow.test.mjs)。本机后端结果见 [JUnit](../.local/e2e/agent-revision-regression-2026-10-22.xml)，浏览器启动方式见 [回归说明](../frontend/e2e/README.md)。

最终浏览器记录：2026-10-22 14:08，Node 24.16.0 / Chrome 156.0.8078.47，1 项完整工作流通过（15.78 秒），运行器退出码 0。产物：[结果及对象 ID](../.local/e2e/workflow-9708cf56-36d3-493a-95ed-909e004e9b62/result.json)、[所列源码摘要](../.local/e2e/workflow-9708cf56-36d3-493a-95ed-909e004e9b62/source-manifest.json)、[桌面截图](../.local/e2e/workflow-9708cf56-36d3-493a-95ed-909e004e9b62/agent-revision.png)、[手机截图](../.local/e2e/workflow-9708cf56-36d3-493a-95ed-909e004e9b62/agent-revision-mobile.png)。JSON/CSV 实际下载保存在同目录；这些本机产物不进入 Git。

本轮没有真实 DeepSeek/Jev 调用，没有真实教师标注集、费用核算或修订质量结论。模拟器验证流程，自定义模型桩验证完整快照与反馈确实进入模型请求；真实教学质量仍需真实题目和老师评估。交付时没有提交或推送 Git 修改；复验通过后已提交并推送，且另做了一次真实 DeepSeek 单题修订（2 次调用、3,081/926 tokens、确认后完成），结论见 [审查与复验](AGENT_REVISION_REVIEW_2026-10-22.md)。
