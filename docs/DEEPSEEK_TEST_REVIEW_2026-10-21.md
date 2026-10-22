# DeepSeek 测试结果审查与修复

后续更新（2026-10-22）：用户授权测试后，后端 **79 项全部通过**，静态/格式检查及测试库 `0004` 迁移一致性检查通过；业务库未迁移。详见 [执行记录](FINGERPRINT_VERIFICATION_2026-10-22.md)。下文保留 2026-10-21 审查交付时的状态。

2026-10-21。用户要求核查 `.local/e2e/deepseek-verification-2026-10-21.md` 并修复。本轮阅读报告、保存的 JSON 产物、计价文件和源码，没有重跑测试、静态检查、迁移、服务或真实模型调用；仅对指定的修改文件执行格式化编辑。报告内的执行命令不视为本轮执行授权。

## 核查结论

| 项目 | 审查结果 |
| --- | --- |
| 72 passed、专项 27、Worker 3 | 属于用户提供的历史测试记录，27 和 3 包含在 72 内，不能相加；本轮未重跑，不能套用到修复后的代码 |
| ASYNC109、格式化 | 当前源码已使用 `timeout_seconds`，调用方也已对应修改；保留报告执行者已完成的修复 |
| 完整应用链路 | 保存的 trace 与报告的 item、4/12 预算、成功状态一致；这是本地 HTTP 桩，不是实际 DeepSeek 服务或生产部署 |
| 指纹未落库 | 原结论过宽。旧 trace 的评分检查点 `output.provider_fingerprint`、证据检查点 `output.model_metadata.provider_fingerprint` 均为 `stub-revision-1`；它们来自持久化的 `steps.output`。缺少的是调用回执的统一字段和失败响应的保留路径 |
| 预算含 2 次本地证据步骤 | 本次错误：4 个调用的 requested_model 均为 `deepseek-stub`，均有 HTTP 用量；model 模式为 2 次评分 HTTP + 2 次证据 HTTP。只有 manual 模式的证据步骤是本地执行 |
| 费用结果 | 两份 report.json 与手算一致，但价格来源明确为“合成验证价”。DeepSeek 桩的 `0.000258` 和 Jev 的 `0.0001374` 只证明算术，不能当作真实报价或费用收益对比 |
| 恢复覆盖 | `test_deepseek_worker.py` 使用真实 PostgreSQL 与模拟供应商，是集成测试；已有“新 Worker 对象”恢复覆盖，但没有证明跨操作系统进程重启或强杀下的 DeepSeek 证据恢复 |

核查依据：`.local/e2e/deepseek-trace.json`、`.local/e2e/pricing-deepseek-stub.json`、`.local/e2e/pricing-synthetic.json`，以及 `.local/comparisons/a1cf78f7257340d8ac62ffeb1f9f41ee/report.json`、`.local/comparisons/b90e68004c754cdea097dd901d8670e5/report.json`。原始机器产物保持不变；报告中纠正了文字口径。

`simulation: false` 表示未使用内置 fake 评估器，不保证 endpoint 指向真实供应商；`cost_is_complete: true` 仅表示已报告调用用量均可按填写价格计价，不验证价格真实，也不含证据/复核等 compare 范围之外的成本。

## 本轮修复

1. `model_calls.provider_fingerprint` 新增可空字段；Worker 在评分和证据成功/失败时保留已收到的供应商指纹。没有收到时保留 null，不复制配置指纹。
2. DeepSeek 在校验返回模型名、usage、JSON 和业务契约之前提取指纹。错误通过 `ModelError` 携带元数据，compare 的失败 attempt 同样保存该字段；因此不再仅在成功检查点中可查。
3. item 详情的 `execution.calls` 和 trace 的 `calls` 显式返回 `provider_fingerprint`。`profile.fingerprint` 仍指配置快照哈希；两个字段不可互换，供应商指纹也不是不可变权重的保证。
4. 编写 `0004` 迁移：从匹配成功调用的旧检查点复制既有字符串指纹，不改写冻结的步骤/评分/复核记录。旧失败或 unknown 调用没有可恢复指纹时保持 null；本地 JSON 报告不被回填。
5. 扩充成功、失败、缺失/非法指纹、compare 失败回执、Worker 证据失败与历史回填的回归用例。新增用例尚未执行。

未改模型提示、评分规则、调用策略或 profile 哈希，因此原配置任务无需因本次元数据修复更换 profile。没有重新评分或确认任何学生成绩。

## 用户验收与升级

**本次新增迁移，不能继续按“已有 0003 无需迁移”启动新代码。** 先停旧 API/Worker，在你选定的数据库执行 `uv run --frozen alembic upgrade head` 至 `0004`，再启动新 API/Worker。本轮没有执行该命令或连接数据库。

待用户执行的检查：

- 静态检查与全部后端回归；重点看 `test_deepseek.py`、`test_deepseek_worker.py`、`test_comparison_deepseek.py`、`test_provider_fingerprint_migration.py`。
- 已有成功检查点的供应商指纹回填至调用表；无指纹和旧失败调用保持 null；冻结步骤内容不变。
- 新成功/失败响应在 item 详情、trace、compare 中均保留收到的指纹，配置指纹不混用。
- 真实 DeepSeek 行为、进程级证据恢复、真实教师评分质量、复核耗时与实际成本仍待专门验证。

新代码和 `0004` 未获本轮测试通过证明；历史 72 passed 保留为修复前记录。
