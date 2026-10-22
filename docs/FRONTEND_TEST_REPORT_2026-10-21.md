# 教师复核工作台测试报告

> C 阶段作业准备入口的验证见 [作业准备说明](ASSIGNMENT_PREPARATION.md#本机验证结果)。
>
> 后续审查发现与修复见 [处理记录](TEST_REVIEW_2026-10-21.md)。含冒号行标识、正式浏览器断言与 CI 的新增修复尚未运行，下文保留原阶段结果，不代表修复后验收。
>
> 用户后续要求布局修复，第 6 节记录的确认栏遮挡已在代码中改为正常文档流，另补窄屏适配；见 [布局修复说明](FRONTEND_LAYOUT_FIX_2026-10-21.md)。本次未重测，原观察保留。

日期：2026-10-21
环境：Windows、Node 22.22.2、npm 10.9.7、Chrome、Python 3.13.5、PostgreSQL 17.11、React 19 / Vite 8 / Ant Design 6

## 结论

前端工作台已完成安装、单元测试、类型检查、生产构建、开发/预览代理和真实浏览器端到端验收。**未发现未修复的功能性缺陷**；修复了 favicon 404 与加载态空文案两处小问题。仍有三类场景未自动覆盖：并发冲突/网络失联/失败重试的浏览器交互、窄屏与键盘操作、CI 实际运行。

| 项目 | 结果 |
| --- | --- |
| `npm ci` | 147 packages |
| Vitest | **12 passed / 3 files** |
| `tsc --noEmit` | 通过 |
| `vite build` | 通过；JS 1,011 kB / gzip 323 kB，有 >500 kB 警告 |
| 开发代理 | 页面、`/v1/me`、`/health`、`/v1/courses`、`/v1/grading-batches` 均 200 |
| 预览 | 页面、构建产物、`/v1/me` 代理均 200 |
| 浏览器端到端 | 连接 → 批次 → 复核 → 提交 10.00 → 导出 → CSV/JSON 下载；控制台零错误 |
| 后端回归 | **41 passed**；Ruff 通过；`alembic check` 无待生成迁移 |

## 1. 单元测试覆盖

| 文件 | 测试数 | 覆盖 |
| --- | --- | --- |
| `src/features/review/evidence.test.ts` | 4 | CRLF、emoji、重复片段的第二次出现、UTF-16 切半字符、DOM 边界、插入标记拒绝 |
| `src/features/review/draft.test.ts` | 3 | 最高复核版本及引用、弃判不转零分、`absence_confirmed` 显式空引用、理由/引用校验 |
| `src/api/client.test.ts` | 5 | 丢失响应后复用 key 与字节、409 不自动重提、不可读回执、响应体读取超时、非同源/转义路径不带凭据 |
| 合计 | **12** | 3 个文件，1.45s |

## 2. 构建

```text
dist/index.html                     0.58 kB │ gzip: 0.37 kB
dist/assets/index-CFBBvUGR.css     11.58 kB │ gzip: 3.26 kB
dist/assets/index-C0x_UizL.js   1,011.41 kB │ gzip: 323.38 kB
✓ 1498 modules transformed
```

Vite 提示单个 chunk 超过 500 kB。当前是内部工作台，首屏可接受；若要优化，可按页面动态导入或拆分 Ant Design 组件。

## 3. 真实浏览器验收

工具：`playwright-core` + Chrome（headless），连接真实 API 与 PostgreSQL。

流程与结果：

1. 输入开发令牌进入工作台；批次列表加载 41 条记录。
2. 打开最新批次 `2faa2f41-5a06-4f48-a236-5c7a9dc4fa35`（浏览器验收用合成批次）。
3. 打开 item `1987e15d-90a4-44c9-a65d-20786875d100` 的复核页，两个评分点正常渲染。
4. 选择 `fixed_rule` 的 6.00 分、“原文支持该等级”，用 DOM Range 选中第二次出现的“同一句”，提交后引用位置为 **8–11**（Unicode code point）。
5. `traceability` 选择 4.00 分、“结合完整答案人工判断”，填写理由。
6. 点击“确认全部评分点”，页面显示“已保存 1 次复核”，当前已确认 **10.00**，历史面板出现第 1 次复核。
7. 返回批次，进入“导出快照”，创建 working 快照。
8. CSV 与 JSON 均触发下载：
   - `grades-5119c692-9147-4fb5-8cc8-8df1d99cc8c2.csv`
   - `grades-5119c692-9147-4fb5-8cc8-8df1d99cc8c2.json`
9. 控制台错误：**0**。

截图（临时产物，不入库）：连接页、批次列表、批次详情、复核页、提交后复核页。

### 3.1 390px 窄屏冒烟

在 390×844 视口下重复连接、批次列表、批次详情和复核页；四页的 `document.documentElement.scrollWidth` 均等于 390，无横向溢出。临时移动端截图未入库。键盘操作与真实鼠标拖拽选区仍未演练。

## 4. 代理与预览

| 检查 | 结果 |
| --- | --- |
| 开发代理根路径 | 200，`#root` 存在 |
| `/src/main.tsx` | 200，`text/javascript` |
| 开发代理 `/v1/me`（带令牌） | 200，开发身份 |
| 开发代理 `/health` | 200，`evaluator=fake-v1` |
| 开发代理 `/v1/courses?limit=1` | 200，返回分页信封 |
| 开发代理 `/v1/grading-batches?limit=1` | 200，返回分页信封 |
| 预览页面 | 200，引用构建后的 assets |
| 预览 `/v1/me` | 200 |
| 预览 `/assets/index-*.js` | 200，1,011,312 bytes |

## 5. 本轮修复

| 问题 | 修复 |
| --- | --- |
| 浏览器默认请求 `/favicon.ico` 返回 404，控制台一条错误 | 新增 `frontend/public/favicon.svg`，`index.html` 显式声明图标；复测控制台零错误 |
| 首次加载时表格显示“当前条件下没有批次/答案/快照”，与真实加载状态不符 | 三个表格的 `emptyText` 在 `loading` 时改为“正在读取批次/答案/快照…” |

## 6. 观察与未覆盖

**观察（非阻塞）**

- 复核页底部操作栏使用 `position: sticky; bottom: 16px`，在长页面上会覆盖视口底部的内容（如部分理由输入框/引用区）。这是“确认按钮始终可见”的设计取舍；若希望完全不遮挡，可改为普通流式底栏或增加底部留白。
- JS 单 chunk 1.01 MB（gzip 323 kB），有 Vite 体积警告；当前内部工作台可接受。
- 验证环境 Node 为 22.22.2；CI 固定 24.16.0。`package.json` engines 两者都允许，但 CI 尚未实际触发。

**未自动覆盖**

- 两个页面同时修改同一复核的 409 冲突 UI（后端与 client 单测覆盖协议，浏览器交互未演练）。
- 浏览器网络工具模拟响应丢失后的锁定/重发（client 单测覆盖）。
- 失败项填写额度并重试的页面流程。
- `confirmed_only` 导出的浏览器下载（后端测试覆盖）。
- 键盘操作与真实鼠标拖拽选区；390px 已做无横向溢出的四页冒烟，但未做完整交互。
- GitHub Actions frontend job 实际运行（CI 尚未实际触发）。

## 7. 复现命令

```powershell
# 后端
uv run --frozen alembic upgrade head
uv run --frozen ruff check backend
uv run --frozen pytest -q

# 前端
Set-Location frontend
npm.cmd ci
npm.cmd test
npm.cmd run typecheck
npm.cmd run build
npm.cmd run dev        # 开发代理
npm.cmd run preview    # 预览
```

浏览器脚本（临时资产，不入库）：

```text
e2e.mjs       首次批次/复核/导出面板冒烟
e2e_full.mjs  完整复核提交 + CSV/JSON 下载
setup_batch.py 创建一次性验收批次
screens.mjs   生成界面截图
mobile.mjs    390px 无横向溢出冒烟
```

## 8. 结论与后续

工作台已经可以连接现有 API 完成“选批次 → 逐点评判 → 引用 → 确认 → 导出下载”的完整流程。下一阶段若要进入真实教学试点，优先补浏览器冲突/失联/重试的自动化验收，再决定是否做代码分包与正式登录。
