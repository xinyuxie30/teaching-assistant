# 指纹修复测试记录

日期：2026-10-22，Asia/Shanghai。用户明确要求“测试一下”，随后要求继续，本轮实际执行测试。环境为 Windows、Python 3.13.5；数据库测试使用已配置的 `teaching_test`，每个测试在随机独立 schema 中运行并清理。

## 执行结果

| 检查 | 结果 |
| --- | --- |
| `ruff check backend` | 首轮发现新增测试中的 2 处 ASYNC240，修复后通过 |
| `ruff format --check backend` | 39 个文件格式检查通过 |
| 全部后端 pytest | **79 passed，0 failed，0 errors，0 skipped，47.33 秒** |
| 独立 schema 中 `alembic upgrade head` | 成功升级至 `0004` |
| 同一 schema 中 `alembic check` | `No new upgrade operations detected.` |
| 临时迁移检查 schema 清理 | 已完成 |

两处 ASYNC240 修复：compare 失败回执测试改用 `asyncio.to_thread` 读取报告；迁移测试在模块加载时解析固定脚本路径，避免异步函数内阻塞路径操作。没有修改业务逻辑以绕过测试。

全量用例包括 DeepSeek 适配器 27 项、compare 4 项、DeepSeek Worker 5 项、指纹迁移 1 项，以及既有领域、Jev、列表、准备、恢复与完整工作流回归。上述子项包含在 79 内，不额外累加。

## 重点覆盖

- DeepSeek 成功输出和解析后的失败响应均保留供应商指纹；缺失或非字符串指纹保留 null。
- Worker 的评分/证据成功与失败回执、item 详情和 trace 字段一致；配置指纹与供应商指纹不混用。
- 证据步骤重试后复用已成功的评分检查点，不重复执行已完成评分。
- compare 的失败 attempt 保留已收到的指纹和用量。
- `0004` 从匹配的历史成功检查点回填指纹，旧失败/unknown 和无有效指纹的记录保持 null，冻结步骤内容不变。
- 从空隔离 schema 升级到 `0004` 后，数据库结构与当前 SQLAlchemy 模型一致。

## 环境问题及处理

首次 pytest 被沙箱临时目录权限阻断，未完成测试。改为工作区内随机临时目录后，异步用例仍在 Python `asyncio` 初始化的本地 `socketpair/accept` 阶段卡住；诊断栈显示尚未进入业务测试。停止这些运行后，经工具授权在沙箱外重跑，使用同一测试套件、独立测试库及模拟供应商，最终 79 项全部通过。没有通过替换事件循环或删减用例来规避问题。

复现主命令（临时目录每次使用新 GUID，避免覆盖旧产物）：

```powershell
& .\.venv\Scripts\ruff.exe check backend
& .\.venv\Scripts\ruff.exe format --check backend
$testTempPath = Join-Path (Get-Location) ('.local/pytest-' + [guid]::NewGuid().ToString('N'))
& .\.venv\Scripts\pytest.exe -x -q --tb=short --basetemp=$testTempPath --junitxml=.local/e2e/fingerprint-regression-2026-10-22.xml
& .\.venv\Scripts\python.exe .local/e2e/check_fingerprint_schema.py
```

## 产物与边界

- [JUnit 结果](../.local/e2e/fingerprint-regression-2026-10-22.xml)：79 项、零失败/错误/跳过及逐用例时间。
- [迁移一致性结果](../.local/e2e/fingerprint-schema-2026-10-22.json)：测试数据库、临时 schema、`0004` 和清理状态。
- [源码 SHA-256 清单](../.local/e2e/fingerprint-source-manifest-2026-10-22.json)：本轮结果对应的后端 Python 源码。
- [隔离迁移检查脚本](../.local/e2e/check_fingerprint_schema.py)：仅接受名称以 `_test` 结尾的测试库，不输出凭据。

**业务数据库没有迁移，真实 DeepSeek/Jev 没有调用。** 启用新版 API/Worker 前，仍须对目标业务库执行 `0004` 升级；本轮仅证明测试库中的迁移与后端回归通过。没有重跑前端/浏览器测试，没有验证跨进程强杀下的 DeepSeek 证据恢复、供应商真实行为或教学准确性。
