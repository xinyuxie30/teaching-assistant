# 教学助手测试报告

> 2026-10-21 前端工作台的安装、单测、构建和浏览器全链路结果见 [工作台测试报告](FRONTEND_TEST_REPORT_2026-10-21.md)。

日期：2026-10-20
环境：Windows、Python 3.13.5、PostgreSQL 17.11、Jev `jev-1.14.0`

## 结论

验证覆盖了迁移、静态检查、自动化测试、真实 Jev 调用、列表分页与身份隔离、复核与导出快照、并列弃判、失败重试和配置/数据集边界。**未发现未修复的功能性缺陷**；发现并修复了 1 个真实缺口（供应商请求 ID 头未被采集）。

| 项目 | 结果 |
| --- | --- |
| 迁移 0002 → 0003（现有库） | 通过；24 张业务表 |
| `alembic check` | 无待生成迁移 |
| `ruff check` / `format` | 通过；25 个文件 |
| `pytest -q` | **27 passed**（原 21 + `test_jev.py` 6） |
| 真实 Jev `compare` | 2/2 assessed；1298 input / 38 output tokens |
| 真实 API+Worker 批次 | 2 项 `settled / all_succeeded` |
| 列表/详情/筛选 | 30 项断言全过 |
| 复核/导出 | 22/23 通过；1 项为测试断言写错，CSV 实际正确 |
| 并列弃判/重定向 | 9 项全过 |
| 配置/数据集边界 | 17 项全过 |

## 1. 代码与迁移

- 现有开发库从 `0002` 升到 `0003`，新增 `evaluation_profiles`、`model_calls` 的 request ID / prompt hash / HTTP 状态 / 可重试标记。
- 旧 `fake-v1` 初评全部能关联 profile，无孤儿记录。
- `evaluation_profiles` 的更新被触发器拒绝（SQLSTATE 55000）。
- 每个测试在随机 schema 中重新应用完整迁移后运行。

## 2. 自动化测试

| 文件 | 内容 | 结果 |
| --- | --- | --- |
| `backend/tests/test_domain.py` | 细则、计分、证据、候选等级 | 8 passed |
| `backend/tests/test_workflow.py` | 全链路、幂等、并发复核、权限、快照 | 8 passed |
| `backend/tests/test_reliability.py` | 租约、代次、超时、重试、证据校验 | 5 passed |
| `backend/tests/test_jev.py` | request ID（成功/错误）、无学生身份、错 legend、模型版本、输入预算 | **6 passed** |
| 合计 | | **27 passed / 12.59s** |

## 3. 真实 Jev

### 3.1 出口

| 出口 | Cloudflare `loc` | TypeSafe |
| --- | --- | --- |
| 51114（旧环境代理） | CN | 451 `not available in your region` |
| 7897（Clash） | US | 200 |

Jev 代理已写入 `.env`；代理只作用于 Jev HTTP 调用，不进入 profile manifest，不改变 profile ID。

### 3.2 真实 compare

- 命令：`teaching-assistant compare --dataset evals/samples/synthetic-v1.json --profiles jev-v1 --max-samples 1 --max-calls 4 --max-seconds 120 --max-attempts 2`
- 结果：2/2 assessed；1298 input / 38 output tokens；**费用待核算**。
- 2026-10-21 审查后更正：撤下原先缺少单价、价目来源和适用日期的美元估算。成功 run `9cac1f09a1c54f98b83a2cdf1875d514` 的机器报告中 `policy.pricing=null`、`estimated_usd_for_reported_usage=null`、`cost_is_complete=false`；本次更正没有重新调用模型或回填历史价格。上述 tokens 只属于该成功 run，其他尝试存在未知用量，整个实验总成本仍不完整。
- 两次调用均记录 `provider_request_id`（`req_...`）、`http_status=200`、`returned_model=jev-1.14.0`、usage。
- 报告：对照报告（临时产物）

### 3.3 真实 API+Worker

- 批次：`498624e6-ed1d-449a-b201-319c85c217b4`
- 2 份答案，2 个评分点/份，`settled / all_succeeded`。
- 完整答案 10.00；简答 0.00。
- 第一项有一次 `provider_transport_error` 被记为 `unknown`，随后重试成功（`calls_used=5`）；第二项 4 次调用。
- 成功调用均有 `req_...`、HTTP 200、usage；本地 `manual-evidence-v1` 步骤无 request ID（正常）。
- 初评带 `requires_teacher_review`、`no_citation`、`evidence_pending_manual_selection`。

### 3.4 失败与重试分类

用本地模拟端点验证：

| 场景 | 结果 |
| --- | --- |
| 401 | `provider_auth_error`，一次失败，`error_retryable=false` |
| 500 | `provider_temporarily_unavailable`，3 次后 failed |
| 429 + Retry-After | `retry_wait` 后重试成功 |
| 模型版本不符 | `provider_model_version_mismatch`，一次失败 |
| legend 不符 | `provider_invalid_score`，重试后 failed |
| 超时 | `model_timeout`，调用记 `unknown` |
| 307 重定向 | `provider_request_rejected`，不跟随、不重试 |
| 概率并列 | `abstained`，建议分为 null，老师可手选 |

## 4. 列表、复核与导出

- 12 个分页列表 + 4 个详情接口；游标 26 项 / 13 页无重复。
- 30 项筛选/游标/排序/隔离断言全过：`q`、`classroom_id`、`question_key`、draft 状态、answers 三筛选、batches 四筛选、items 五筛选、exports batch/mode、cursor 换筛选 422、非法枚举 422、未知 student 404。
- `%`/`_` 按字面匹配；`created_from/to` 含边界；naive datetime 422。
- 列表只回头信息/预览；详情取全文。
- 复核：`supported` 无引用 422；精确引用 `{"quote":"方便老师管理。","start":0,"end":7}` 通过；`teacher_judgment`、`absence_confirmed` 通过。
- 导出：第三次复核改为 6.00 后，旧导出仍为 3.00；`confirmed_only` 导出 1 行、排除 1 行；CSV 待确认分数为空。
- 报告中唯一 FAIL 是测试断言把待复核行的 `suggested_score` 写成 0.00，实际是 10.00；CSV 本身正确。

## 5. 配置与数据集边界

- key 轮换不改变 profile ID。
- model、endpoint、prompt、输入上限、调用超时变化生成新 profile ID。
- lease 变化不改变 profile ID。
- 非法代理（socks5、带 path/query、空串）被拒绝。
- `unlabeled` 带 gold、`teacher_confirmed` 缺 gold、未知 level、重复 `sample_id`/`answer_version_id` 被拒绝。
- 高置信度误判清单、`--severe-error-points` 统计正常。
- snapshot session 为 `REPEATABLE READ`；10 MB 数据集上限生效。

## 6. 发现并修复

**供应商请求 ID 头**：TypeSafe 返回 `x-typesafe-request-id`，原适配器只查找 `x-request-id`/`request-id`，导致 `provider_request_id` 长期为空。

- `jev.py` 已优先读取 `x-typesafe-request-id`；
- 成功与错误（429）两条路径均有回归测试；
- 修复后真实调用记录 `req_...`；
- 修复前已评分的 4 条合成调用（batch `aec2a737...`，两个 item × 两个评分点）仍为 null。原始响应头未持久化，无法真实回填；用户选择保留 null 作为“未采集”的历史状态，不伪造、不覆盖审计。

## 7. 未覆盖

- 强制中断 / kill 进程时的 `interrupted` / `dispatched` 报告；
- 真实老师标注样本的评分质量、成本与复核收益；
- 并发压测；
- 多次翻页期间的并发写入（按设计不持有跨请求快照）。

## 8. 复现命令

```powershell
uv run --frozen alembic upgrade head
uv run --frozen ruff check backend
uv run --frozen ruff format --check backend
uv run --frozen pytest -q
uv run --frozen teaching-assistant compare --dataset evals/samples/synthetic-v1.json --profiles jev-v1 --max-samples 1 --max-calls 4 --max-seconds 120 --max-attempts 2
uv run --frozen uvicorn teaching_assistant.api:create_app --factory
uv run --frozen teaching-assistant worker
```

## 9. 产物索引

| 路径 | 内容 |
| --- | --- |
| 本报告及 [最新审查](TEST_REVIEW_2026-10-21.md) | 保留历史验证结果；旧 `LISTS_AND_MODEL.md` 已删除，当前接口说明转至 [API 与对照入口](API_AND_EVALUATION.md)，未补造缺失的旧记录 |
| 综合断言输出（临时产物） | 57 项综合断言输出（列表 + Jev 失败分类） |
| 列表筛选脚本（临时产物） | 30 项列表/筛选/分页断言 |
| 并列弃判脚本（临时产物） | 并列弃判与 307 重定向 |
| 真实复核/导出脚本（临时产物） | 真实 Jev 项复核/导出 |
| 真实 Worker 脚本（临时产物） | 真实 API+Worker 批次 |
| `backend/tests/test_jev.py` | Jev 离线契约测试 |
| 最新真实 compare 报告（临时产物） | 最新真实 compare 报告 |
| 第一次真实 compare 报告（临时产物） | 第一次真实 compare（修复前，request ID 为空） |
