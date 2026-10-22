# Agent 修订版审查与复验

日期：2026-10-22。对象：工作区未提交的“老师反馈驱动修订”增量（[交付说明](AGENT_REVISION_2026-10-22.md)）。本轮做三件事：审查代码、独立复跑、核对 GitHub Actions 的 browser 失败。

## 一、结论

**未发现阻断性缺陷。** 修订链路把“来源草稿快照 + 老师意见”写入任务本身，Worker 只读快照、不回读可变草稿；新版是独立草稿，确认只完成关联任务，原稿与原任务保持不变。幂等兼容、跨题目/跨老师隔离、上下文预检都有回归覆盖。

复跑结果与交付声明一致：后端 **118 passed**、Ruff 53 文件、前端 **40 passed**、类型与构建通过；在**全新数据库**上按 CI 命令跑浏览器回归，**1 passed / 7 阶段 / 0 浏览器错误**。

GitHub 的 browser 失败出现过两次，均已定位为**测试与 React 异步状态之间的竞态**，不是产品逻辑错误：一次在确认细则后过早点击固定按钮，一次在预览响应后过早点击导入。两处都改成等待界面反映响应，本地连续两轮通过。

## 二、代码审查要点

| 关注点 | 判断 |
| --- | --- |
| 来源归属 | 先校验题目属于当前老师，再按 `id + question_version_id` 锁草稿；跨题目与跨老师均 404，且不创建任务（有测试） |
| 快照一致性 | 创建事务内锁来源草稿并保存正文、修订号、正文摘要；Worker 使用 `agent_runs.messages` 中的快照，测试模拟“创建后草稿被改”仍使用旧快照 |
| 新版独立性 | `propose_rubric` 写入新草稿；确认新版只把该任务改为 `completed`，原任务保持 `waiting_teacher` / `completed`，原草稿正文与确认指针不变 |
| 幂等兼容 | 普通起草的哈希用 `exclude_none=True`，与升级前字段一致；旧回执可重放（有迁移往返测试） |
| 权限边界 | 模型工具仍只有 `read_question` / `propose_rubric`；`source_rubric` 在提示中明确为不可信数据，不能授予权限或确认 |
| 反馈可见性 | 列表摘要只给 `source_draft`（id / revision / hash），`revision_feedback` 只在详情返回；测试断言列表不含反馈与消息 |
| 分页 | 复用 `listings.page`，游标绑定老师与筛选；页数据、总数与摘要同一快照；`question_version_id` 先做权限检查 |
| 迁移 | 两个可空列 + 外键 + “同时有值或同时为空”检查约束；`0006 → 0005 → 0006` 往返与 `alembic check` 通过 |
| 提示设计 | 修订提示明确“先 read_question、保留无需改动部分、输出完整新稿、原稿与已确认版本不可变” |

已修复的三个小问题（同日追加）：

1. **历史读取失败不再挡住新起草。** `cannotCreate` 去掉 `history.error` / `history.loading`；失败仍在原处显示并可重试。新增面板测试：历史返回 500 时“开始起草”保持可用。
2. **优先恢复可操作的任务。** 新页面会话按“等待确认 → 运行中 → 最近一条”选择，而不是无条件选最新；最新是失败/取消时不会盖住更早的待确认草稿。`preferredRun` 有独立单测和面板测试。
3. **恢复 effect 不再每次渲染执行。** 依赖改为 `runId` 与历史数据，并在写入前重新读取 store，避免覆盖刚创建的任务。

## 三、独立复跑

| 检查 | 结果 |
| --- | --- |
| `ruff check` / `format --check` | 通过，53 文件 |
| 后端 `pytest -q` | **118 passed**，83.27 s |
| 前端 `npm test` | **40 passed**（7 文件） |
| `tsc --noEmit` / `vite build` | 通过；入口 22.85 kB，无体积警告 |
| 全新库 `teaching_ci_test`：`0001 → 0006` | 成功 |
| CI 同构浏览器回归 | **1 passed，7/7 阶段，0 浏览器错误**（21.7 s） |
| 真实 DeepSeek 修订 | **completed**；2 次调用、3,081 input / 926 output tokens，草稿 2 点评分、总分 10.00 |

CI 同构 = 新建空库、CI 的固定令牌、`TA_EVALUATOR=fake-v1`、API + 批改 Worker + Agent Worker + Vite、`npm --prefix frontend/e2e test`。唯一差别是本地用 Chrome、CI 用 Chromium。

追加修复三个小问题并接入“修订对照”后复跑：后端 **127 passed**、Ruff 56 文件、前端 **47 passed**、`tsc` 与构建通过、浏览器 **7/7 阶段**。对照功能见 [修订草稿与原稿的结构化对照](AGENT_REVISION_DIFF_2026-10-22.md)。

## 四、真实供应商验证（单题）

用浏览器回归里的合成题目和已确认草稿做一次真实修订，验证“来源快照 + 老师意见”在真实供应商下能走完工具循环：

| 项目 | 结果 |
| --- | --- |
| 请求 | `deepseek-rubric-agent-v1`，`source_draft_id=240ba962-…`，意见为“细化可观察的作答特征，保留结构与满分” |
| 任务 | `e70c80e5-3a3e-4d21-bf98-a241bb97e5c7`，2 次调用 / 2 次工具，`waiting_teacher` |
| 用量 | 第 1 次 1,470 input / 33 output；第 2 次 1,611 input / 893 output；缓存命中 2,432 |
| 返回模型 | `deepseek-flash`，指纹 `e72d3c7f…`（`deepseek-chat` 别名的实际模型） |
| 草稿 | 2 个评分点，总分 10.00，未确认；来源草稿保持 1 个评分点 |
| 确认 | 固定细则第 3 版，任务 `completed` |

边界：题目与来源草稿来自浏览器回归的合成样本，没有教师标注，不能据此评价修订后的教学可用性；未配置单价，费用仍未知。原始记录在 `.local/e2e/real-deepseek-revision-2026-10-22.json`。

## 五、业务库升级

- 备份：`.local/backups/teaching-before-0006.dump`（210 KB）
- `alembic upgrade head`：`0005 → 0006`；`alembic current = 0006`，`alembic check` 无待生成操作
- 结构核对：`agent_runs.source_draft_id`、`revision_context` 存在，`ck_agent_revision_source`、`fk_agent_runs_source_draft` 存在；原有 0 条 Agent 任务不受影响

## 六、GitHub browser 失败诊断

一共出现两次 browser 失败，形态不同，但根因同类：**测试在 React 把网络响应写回界面前就继续下一步**。

### 第一次：#39716914937（`26a404a`）

- 失败信息：`locator.click: Timeout 15000ms exceeded`，等待 `getByRole('button', { name: '使用这份固定细则，导入答案', exact: true })`。
- `result.json`：`browserErrors: []`；两个 checkpoint 已完成（Agent 确认、手动细则确认）。
- `api.log`：没有 4xx/5xx；确认请求正常。
- `failed.png`：页面已显示“草稿已确认 / 固定版本”，按钮存在。
- 处置：点击前先等“固定版本”标签，把等待条件改成“确认回执已反映到界面”。

### 第二次：#39735662301（`49b50c5`，纯文档提交）

- 失败信息：`page.waitForResponse: Timeout 15000ms exceeded`，等待 `POST /v1/assignments/{id}/imports`。
- `api.log`：只收到 `imports/preview` 的 200，**完全没有收到 imports 提交**。
- 原因：预览响应到达时 React 还没提交 `preview` 状态，紧接着点“确认导入”时 `importForCommit` 判定“尚未预览”，直接显示错误并返回，因此没有发出请求。同一用例在本机通常更快，掩盖了这个竞态。
- 处置：预览后等待 `.import-preview` 出现，再点提交。

### 第三次：#39741239637（`91bef1b`，修订对照）

- 失败信息：仍是等待“使用这份固定细则，导入答案”按钮的 15 秒超时。
- `api.log`：确认请求 200，紧接一次草稿读取 200，之后没有异常；`failed.png` 里按钮已经渲染。
- 本机用 Playwright Chromium（与 CI 同款）复现：同一用例约一半概率失败，失败点在“确认细则 / 预览 / 提交导入”之间漂移。
- 探针结果：按钮存在、可见、未禁用，但点击处理函数有时没有执行；服务端日志确认请求根本没有发出。
- 原因：Playwright 的 `getByRole` 与坐标点击依赖可访问性树和元素位置，而页面在异步数据到达时会重新布局；Chromium 下偶尔解析不到目标或点空，React 的处理函数就不会运行。
- 处置：回归的 `click` 改为按 **DOM 文本** 找 `button`、校验文本与可用状态，再在节点上直接 `click()`，绕开可访问性树与坐标命中。清理探针后连续 **6 轮 Chromium** 全部通过（4 轮带探针 + 2 轮清理后）。

### 运行对照

| 运行 | 提交 | 结果 |
| --- | --- | --- |
| #39717023140 | `59fd891` | success |
| #39717055527 | `5c4a8de` | success |
| #39717088339 | `e01f1ab` | success（含 browser） |
| #39734792943 | `8f39252`（修订实现） | success：backend / frontend / browser 全部通过 |
| #39735662301 | `49b50c5`（文档） | failure：imports 提交未发出 |
| #39741239637 | `91bef1b`（修订对照） | failure：确认细则按钮点击未触发 |

判断：三次失败都属于**测试与异步渲染的竞态**，不是产品缺陷；真实老师不会在几十毫秒内抢在 React 提交或重排前点击。先前两次只处理了“等界面状态”，没有处理“定位与点击本身”；第三次改用 DOM 文本定位 + 节点点击后才稳定。最终以修复提交的 CI 结果为准。

## 七、仍未闭合

1. 真实 DeepSeek 已跑通单题修订，但只有一份合成样本，且无单价；修订后的教学可用性、严重偏差与单次成本仍未评测。
2. 教师标注集、严重误判门槛与单价仍缺。
3. 若 browser 再次失败，先看 `failed.png`、`result.json` 与 `api.log`：**请求有没有到达服务端**能直接区分“产品没发请求”和“测试没等到”。
