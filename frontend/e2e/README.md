# 浏览器业务回归

最新执行（2026-10-22 Harness v4）：**1 passed，9/9 阶段、0 浏览器错误，约 22 秒**。使用迁移 `0010` 和新版 Harness，原有修订、分析及导出全流程通过。首次启动发现独立 Worker 入口循环导入，修复并加入独立进程测试后，完整浏览器回归通过。最终 [运行记录](../../.local/e2e/workflow-d100b358-5627-43da-9c30-336a41ef21ce/result.json)。使用隔离 schema 与模拟模型，结束清理临时服务；新备课/记忆/并发/问答机制由后端集成用例验证，问答界面由组件测试覆盖。以下保留历史记录。

最新执行（2026-10-22 班级分析）：**1 passed，9/9 阶段、0 浏览器错误，23.55 秒**。完成既有链路后，从“班级学情”回看原答案并修改到第 2 次复核，确认分析均分从 10.00 更新为 4.00、未确认学生仍被排除、对应评分点显示老师理由，旧导出保持第 1 次复核 10.00。同时验证待复核筛选和 390 px 布局。最终 [运行记录](../../.local/e2e/workflow-95723646-7724-43b4-9060-254155101b09/result.json)、`class-analysis.png` 与 `class-analysis-mobile.png` 位于同一目录，两张截图已检查。隔离 schema 和临时服务已清理，无真实模型调用。详见 [班级分析交付](../../docs/CLASS_ANALYSIS_2026-10-22.md)。以下为历史执行记录。

最新执行（2026-10-22）：**1 passed，8/8 阶段、0 浏览器错误，19.02 秒**。新增从手动草稿详情直接发起修订，核对请求使用所选草稿、成功后解除显式来源、原稿保持不变，并断言新版“1 新增 / 2 移除”的真实对照内容；1440 px 和 390 px 截图已查看，无横向溢出。随后返回原稿完成原有确认、导入、初评、复核和下载。运行目录 [workflow-6d017fbe…](../../.local/e2e/workflow-6d017fbe-0229-4c1f-b4cb-2e66280189b1/result.json)，详见 [草稿入口交付](../../docs/AGENT_DRAFT_ENTRY_2026-10-22.md)。本次使用 `_test` 库随机 schema 和两个模拟器，结束后清理临时服务及 schema。下方保留之前的执行记录。

最新增量（2026-10-22）：Agent 流程已扩展为“原稿 → 老师反馈 → 新版 → 独立确认”，检查来源和意见、原稿保留，以及桌面与 390 px 界面。运行前应用迁移到 `0006`；复验时在全新库 `teaching_ci_test` 上按 CI 命令跑通，本机业务库也已备份后升级。结果见 [修订交付](../../docs/AGENT_REVISION_2026-10-22.md) 与 [审查与复验](../../docs/AGENT_REVISION_REVIEW_2026-10-22.md)，下方保留原始建立记录。

复验执行：**1 passed，7/7 阶段，0 浏览器错误，21.7 秒**（全新库、CI 固定令牌、`npm --prefix frontend/e2e test`）。CI 运行 #39734792943（提交 `8f39252`）的 backend / frontend / browser 三个 job 全部通过，browser 已实际执行修订流程。此前三次 browser 失败均为“异步渲染尚未稳定”的测试竞态：确认细则后过早点击固定按钮、预览后过早点击导入、以及按钮可访问名/坐标点击偶发解析不到。前两处改为等待界面状态，第三处改用 DOM 文本定位并在节点上 `click()`；清理探针后连续 6 轮 Chromium 通过，CI #39744312579 三个 job 全部通过。详见复验记录。

本轮最终执行：**1 passed，7/7 阶段，0 浏览器错误，15.78 秒**；Node 24.16.0、Chrome 156.0.8078.47。机器记录及截图位于 [workflow-9708cf56…](../../.local/e2e/workflow-9708cf56-36d3-493a-95ed-909e004e9b62/result.json)，测试结束已清理临时 schema 和服务。

2026-10-21 修复 R2 时新增，并已于同日在本机独立数据库执行：首轮 1 项断言失败（脚本期望值缺少版本绑定字段），修正后重跑通过。这里是正式回归入口；`.local/browser-test` 中的旧脚本与截图保留为历史观察资料，不再作为自动回归入口。根前端的 `npm test` 仍只运行 Vitest，本目录有独立的依赖与命令。执行结果见文末“执行记录”。

## 覆盖的同一条链路

`workflow.test.mjs` 用 Node test runner、严格断言和 Playwright 驱动真实页面。每次创建 UUID 命名的课程、两名合成学生、作业、题目及两个评分点，通过页面确认规则、导入、建批次、复核和下载；同时先通过页面模拟 Agent 起草、填写修改意见、生成新版并确认，再走手动细则。不会选择“最新批次”或改动已有作业。

出现以下不一致会失败退出，而不是仅打印观察结果：

- 同学生的 `source:a / v1` 与 `source / a:v1` 必须呈现为不同的预览行；修改 JSON 后旧预览立即失效。
- Agent 页面必须停在“等待老师确认”，显示模拟器警示与预算（2 模型 / 2 工具）；填写意见后必须创建不同的新任务和新草稿，显示来源与反馈。确认新版后新任务变为 `completed`，原任务仍为 `waiting_teacher`，原稿正文和确认状态保持不变；未确认时不得生成固定版本。
- 手动草稿详情的修订入口必须使用显式所选草稿，不能误用历史 Agent 任务的草稿。模拟新版按 ID 对照为 1 个新增 / 2 个移除，意见、两侧描述和来源快照一致；生成稿保持未确认，返回原稿后才能确认供后续初评使用。
- 三份导入答案中只选择指定的两份版本，保留空白答案及带 emoji、CRLF、首尾空白的原文。
- 批次必须是 `settled / all_succeeded`，成功 2、失败 0、待复核 2；同时核对页面计数。
- 只复核指定学生，确认分为 `10.00`、修订为 1，另一学生保持待确认。第二处“同一句”必须提交为 Unicode code point `[8,11)`，同时检查原文与保存后的决定。
- 分别创建 `working` 和 `confirmed_only` 快照。读取页面实际下载的 JSON 和 CSV，逐项核对对象身份、版本、评分、确认范围、空确认分、模拟标志、引用、CSV 引号/逗号转义及公式前缀处理。
- 浏览器错误、重复行 key 警告会使回归失败。

所有建批次请求显式使用 `fake-v1`，浏览器拦截器会阻止改成其他配置的 POST；Agent 请求同样只允许 `fake-rubric-agent-v1`。合成答案和模拟老师决定用于工程验收，不代表教学评分正确性；DOM Range 用例也不等同于真实鼠标拖拽、键盘与触屏验收。

## 用户执行

先准备独立 PostgreSQL 测试数据库，例如 `teaching_e2e_test`，并在该数据库上应用现有迁移、运行 `teaching-assistant bootstrap`。API、批改 Worker 与 Agent Worker 均连接这份库，使用专用开发令牌；Vite 代理指向这份 API。Agent 起草需要单独启动 `teaching-assistant agent-worker`，否则任务会停在 `queued`。可复用根 README 的启动命令，更改对应进程环境变量即可，无需覆盖日常 `.env`。不要把 `TA_TEST_DATABASE_URL` 当成应用数据库配置：API/Worker 读取的是 `TA_DATABASE_URL`。

脚本只增加本次合成数据，不重置数据库、不自动清理历史记录。再次运行会新建独立作业。专用库的创建和清理由执行者管理；CI 则使用随 job 销毁的 PostgreSQL 服务。

在服务已启动的情况下，用 PowerShell 执行：

```powershell
Set-Location .\frontend\e2e
npm.cmd ci
$env:TA_E2E_BASE_URL = 'http://127.0.0.1:5173'
$env:TA_E2E_TOKEN = Read-Host '输入专用测试后端的开发令牌'
npm.cmd test
```

默认用本机安装的 Chrome，不自动下载浏览器。没有 Chrome 时，可选择安装 Playwright 对应的 Chromium：

```powershell
npx.cmd --no-install playwright-core install chromium
$env:TA_E2E_BROWSER_CHANNEL = 'chromium'
npm.cmd test
```

| 配置 | 默认 / 说明 |
| --- | --- |
| `TA_E2E_BASE_URL` | `http://127.0.0.1:5173`；仅接受不带路径和凭据的回环 HTTP(S) origin |
| `TA_E2E_TOKEN` | 必填，专用测试后端已 bootstrap 的令牌；脚本不自动读取 `.env` |
| `TA_E2E_BROWSER_CHANNEL` | `chrome`；`chromium` 使用 Playwright 安装的版本，也可选已安装的 `msedge` |
| `TA_E2E_BROWSER_PATH` | 可选，浏览器可执行文件绝对路径，优先于 channel |
| `TA_E2E_PYTHON` | 默认项目 `.venv` 的 Python；只用标准库 `csv` 解析下载，不调用应用或模型 |
| `TA_E2E_PROFILE` | `fake-v1`；设为真实 profile ID 时通过页面显式选择该配置，断言改为 `simulation: false`，会产生真实模型费用 |
| `TA_E2E_PROFILE_LABEL` | 真实配置在页面下拉中的文本，默认匹配含 `jev-` 的选项 |

默认 `fake-v1` 不产生外部调用。真实运行只做工程链路与分数派生校验，不评价模型教学准确性。

依赖锁定 `playwright-core 1.64.1`，已用 `npm ci` 按该锁文件安装成功（未下载浏览器，使用本机已安装的 Chrome）。Node 与项目 CI 使用同一版本即可。

## 产物与 CI

每次执行写入 `.local/e2e/workflow-<UUID>/`：

- `result.json`：通过/失败状态、已完成的断言阶段、合成对象 ID、Node/浏览器版本、时间与错误。
- `source-manifest.json`：所列前后端源码、迁移、锁文件、回归代码和 CI 配置的 SHA-256；它不涵盖数据库、凭据或机器环境，不是整个工作区快照。
- 实际下载的 CSV/JSON，以及完成截图；可识别为本次作业的失败页面也会保存截图。
- `rubric-comparison.png` / `rubric-comparison-mobile.png`：有实际新增/移除内容的桌面与手机对照区块；Agent 状态截图另存为 `agent-revision*.png`。

不保存包含授权头的 HAR、trace、存储状态或登录截图；异常信息中的测试令牌会替换为 `[REDACTED]`。产物目录被 Git 忽略，复盘时应附上对应 run 目录及退出结果，不能把仅有部分 checkpoint 的失败记录称为通过。

`.github/workflows/checks.yml` 新增 `browser` job，等待前后端检查后，在独立数据库中运行迁移、bootstrap、API、批改 Worker、Agent Worker、Vite 和本脚本，保存日志及合成下载，结束时停止服务。未配置真实模型凭据。CI 产物保留 14 天，需长期保留的验收记录由执行者归档。

本轮已在模拟器与真实模型两种配置下执行，GitHub Actions 的 `browser` job 尚未在真实 CI 触发。后端 42 项 pytest 与前端 22 项 Vitest 只覆盖单元与契约层，不替代本目录的浏览器断言。

## 执行记录（2026-10-21）

| 项目 | 模拟器 | 真实模型 |
| --- | --- | --- |
| 配置 | `fake-v1` | `jev-b4bd3afc947b950e99b3fd2fc99a120de917251dac3c113860f8731f161422b9`（jev-1.14.0） |
| 结果 | 1 passed，12.7 s，6/6 断言阶段 | 1 passed，25.3 s，6/6 断言阶段 |
| 外部调用 | 无 | 4 次 `evaluate`，全部 HTTP 200，另有 4 次本地证据步骤 |
| run 目录 | `workflow-fdd1c997-71b5-4e55-bd9a-dd9d62faf210/` | `workflow-5593d5d0-e577-4fae-b68d-d2958e447056/` |
| Node / 浏览器 | v22.22.2 / 本机 Chrome 156.0.8078.47 | 同左 |

- 首轮在复核保存后的决定比对处失败：脚本直接用请求体 `deepEqual` 服务端返回的 `decisions`，而 `service.review_item` 会为每条决定补记 `answer_version_id` / `rubric_version_id`（版本绑定，属设计行为）。已改为先断言这两个绑定字段、再比对其余字段；失败样本保留在 `workflow-c279fa0e-87e6-4d8a-8c7e-6909200bafed/`。
- 真实轮次另有 1 次脚本侧错误：期望值用 `String(false)` 生成 `false`，而 CSV 实际写 `False`（Python 布尔渲染），修正期望值后通过；CSV 内容本身正确。
- 真实调用记录（`model_calls`）：request id `req_01a15d4e...` 等 4 条，usage 524–549 input / 19 output，单次 3.1–4.4 s。分数由程序按所选等级派生（完整答案 10.00、空白答案 0.00），不采信供应商连续分。
- 两种配置的通过轮次都运行在已含历史合成对象的库上，说明脚本不依赖“最新批次”，也不会改动历史作业。
- 产物含 `result.json`、`source-manifest.json`、两种范围的 CSV/JSON 下载与 `completed.png`；`source-manifest.json` 中 `frontend/e2e/workflow.test.mjs` 的 SHA-256 与通过轮次源码一致。

### 真实进程终止与租约恢复（同日）

以真实模型批次演练，不模拟数据库租约：

1. 创建批次后 3 s 强杀 Worker，项停在 `running`、`model_calls` 为 `dispatched`、attempt 仍为 running。
2. 租约到期后启动新 Worker：epoch 1 的 attempt 记为 `abandoned`，在途调用记为 `unknown / lease_expired`（不假装没有发生，也不回填用量）。
3. epoch 2 由新 Worker 重新领取，2 次真实调用成功，项 `succeeded`、建议分 10.00，批次 `settled / all_succeeded`。

并发复核另以同一 `expected_revision=0` 双请求验证：一个 200（revision 1、10.00），一个 409 `revision_conflict`。

仍需后续补：浏览器双页面 409 的界面保留编辑、提交成功但响应丢失后的同 key 重试、失败项单独重试、跨分页版本选择，以及真实进程终止后的**浏览器内**恢复演练（本轮为 API + Worker 层）。

## 执行记录（2026-10-22，含 Agent）

| 项目 | 结果 |
| --- | --- |
| 配置 | `fake-v1` + `fake-rubric-agent-v1` |
| 结果 | **1 passed，15.6 s，7/7 断言阶段，0 浏览器错误** |
| Agent 轮次 | run `b8f1dffe-a70c-4f4a-8916-8ca7ff44d669`，草稿 `3a4f1a26-69ce-4562-91fb-f49f7992b679`，确认后 `completed` |
| 固定细则 | Agent 草稿为第 1 版；随后手动细则为第 2 版（`589af975-…`） |
| run 目录 | `workflow-d2396b3a-a2f4-45f2-8d6a-8e44c0b5941f/` |

- 首轮在“浏览器零错误”处失败，原因是 Ant Design 对 `Alert.message` 的弃用警告，改用 `title` 后通过；业务断言全部已通过，警告没有被忽略。
- 脚本新增 Agent 请求拦截器：非 `fake-rubric-agent-v1` 的 POST 会被阻止，避免回归意外触发真实费用。
- 本地执行需要同时启动 API、批改 Worker、Agent Worker 与 Vite；缺少 Agent Worker 时任务只会停在 `queued`。
- CI 的 `browser` job 已同步加入 Agent Worker，但首次真实运行尚未观测。
