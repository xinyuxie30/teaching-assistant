import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";
import { chromium } from "playwright-core";
import {
  artifactDirectory, configuration, csvRecords, downloadFile, eventually,
  postAction, readJson, selectCitation, sourceManifest,
} from "./support.mjs";

const RAW = "  😀同一句\r\n同一句，统一规则并追溯历史。  ";
const CITATION = { quote: "同一句", start: 8, end: 11 };
// Real-model runs reuse this script; the simulator stays the default so routine runs stay free.
const PROFILE = process.env.TA_E2E_PROFILE || "fake-v1";
const SIMULATION = PROFILE === "fake-v1";
const CSV_COLUMNS = [
  "student_id", "external_student_id", "student_name", "execution_status", "review_status",
  "suggested_score", "confirmed_score", "review_revision", "answer_version_id", "rubric_version_id",
  "evaluation_profile", "simulation",
];

test("one synthetic assignment: preparation, fake evaluation, review and both export scopes", { timeout: 240_000 }, async (t) => {
  const config = configuration();
  const runId = `workflow-${randomUUID()}`;
  const assignmentTitle = `回归作业-${runId}`;
  const directory = await artifactDirectory(runId);
  const redact = (value) => String(value).replaceAll(config.token, "[REDACTED]");
  const report = { runId, startedAt: new Date().toISOString(), node: process.version, status: "running", profile: PROFILE, checkpoints: [], ids: {} };
  const checkpoint = (name) => { report.checkpoints.push(name); t.diagnostic(name); };
  const browser = await chromium.launch(config.browserOptions);
  t.after(() => browser.close());
  report.browser = browser.version();
  const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1440, height: 1000 } });
  context.setDefaultTimeout(15_000);
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(redact(error.message)));
  page.on("console", (message) => {
    if (message.type() === "error" || /same key|unique.*key/i.test(message.text())) browserErrors.push(redact(message.text()));
  });
  // A UI regression must never turn this fixture into an unintended paid request.
  await page.route(`${config.origin}/v1/grading-batches`, async (route) => {
    if (route.request().method() === "POST"
      && route.request().postDataJSON()?.evaluation_profile !== PROFILE) {
      browserErrors.push(`Blocked a batch that did not explicitly select ${PROFILE}`);
      await route.abort("blockedbyclient");
    } else await route.continue();
  });
  await page.route(`${config.origin}/v1/question-versions/*/rubric-agent-runs`, async (route) => {
    if (route.request().postDataJSON()?.agent_profile !== "fake-rubric-agent-v1") {
      browserErrors.push("Blocked an agent run that did not explicitly select the simulator");
      await route.abort("blockedbyclient");
    } else await route.continue();
  });
  const click = (name) => page.getByRole("button", { name, exact: true }).click();
  const fill = (label, value) => page.getByLabel(label, { exact: true }).fill(value);
  const post = (path, action) => postAction(page, config.origin, path, action);
  const get = (path) => readJson(context, config, path);
  const stat = (label, expected) => eventually(`UI count: ${label}`, async () => {
    const card = page.locator(".stat").filter({ has: page.locator("span", { hasText: label }) });
    assert.equal(await card.locator("strong").textContent(), String(expected));
  });

  try {
    await writeFile(join(directory, "source-manifest.json"), JSON.stringify(await sourceManifest(), null, 2));
    await page.goto(config.origin);
    await fill("开发访问令牌", config.token);
    await click("进入工作台");
    await page.getByRole("button", { name: /作业准备/ }).click();
    await click("新建课程与班级");

    const students = [
      { external_id: `${runId}-1`, name: '学生甲,"甲"' },
      { external_id: `${runId}-2`, name: "=学生乙" },
    ];
    const classroomName = `回归班级-${runId}`;
    await fill("课程名称", `回归课程-${runId}`);
    await fill("班级名称", classroomName);
    await fill("第 1 位学生学号", students[0].external_id);
    await fill("第 1 位学生姓名", students[0].name);
    await click("添加学生");
    await fill("第 2 位学生学号", students[1].external_id);
    await fill("第 2 位学生姓名", students[1].name);
    const { body: workspace } = await post("/v1/workspaces", () => click("保存课程、班级和名单"));
    assert.equal(workspace.students.length, 2);
    for (const student of students) student.id = workspace.students.find((row) => row.external_id === student.external_id)?.id;
    assert(students.every((student) => student.id), "Created roster must match both external IDs");
    report.ids.course = workspace.course_id;
    await click("打开课程，创建作业");
    await fill("作业名称", assignmentTitle);
    await page.getByLabel("班级", { exact: true }).click();
    await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content").filter({ hasText: classroomName }).click();
    const { body: assignment } = await post(`/v1/courses/${workspace.course_id}/assignments`, () => click("创建作业并固定名单"));
    assert.equal(assignment.roster_size, 2);
    report.ids.assignment = assignment.id;
    await click("添加题目");
    await fill("题号", "q1");
    await fill("题目满分", "10.00");
    await fill("题干", "说明固定评分规则及保留历史版本的用途。（浏览器回归合成样本）");
    await fill("参考答案或课程材料", "统一当前评分规则，保存原版本供追溯。本样本只验证系统流程。");
    const { body: question } = await post(`/v1/assignments/${assignment.id}/question-versions`, () => click("保存题目版本"));
    assert.equal(question.version, 1);
    report.ids.question = question.id;
    await click("打开题目，准备评分细则");
    await click("用 Agent 起草");
    await click("开始起草");
    let agentRun;
    await eventually("agent run waits for the teacher", async () => {
      const listing = await get("/v1/agent-runs?limit=20");
      agentRun = listing.items.find((item) => item.question_version_id === question.id);
      assert(agentRun, "an agent run must be created for this question");
      assert.equal(agentRun.status, "waiting_teacher");
      assert.equal(agentRun.simulation, true);
      assert(agentRun.draft_id, "waiting_teacher must point at a saved draft");
    }, 60_000);
    // The simulator reads the question and proposes a draft: two model calls, two tool calls.
    assert.equal(agentRun.models_used, 2);
    assert.equal(agentRun.tools_used, 2);
    await page.getByText("等待老师确认", { exact: true }).first().waitFor();
    await page.getByText("模拟器只走通工程流程，生成的草稿不能作为教学标准。", { exact: true }).waitFor();
    const agentDraft = await get(`/v1/rubric-drafts/${agentRun.draft_id}`);
    assert.equal(agentDraft.body.criteria.length, 1);
    assert.equal(agentDraft.body.criteria[0].maximum_points, "10.00");
    await click("打开草稿并确认");
    const { body: agentRubric } = await post(`/v1/rubric-drafts/${agentRun.draft_id}/confirm`, () => click("确认并固定评分细则"));
    assert.equal(agentRubric.version, 1);
    await eventually("agent run completes after the teacher confirms", async () => {
      const detail = await get(`/v1/agent-runs/${agentRun.id}`);
      assert.equal(detail.status, "completed");
      assert.equal(detail.draft_id, agentRun.draft_id);
    });
    report.ids.agentRun = agentRun.id;
    report.ids.agentDraft = agentRun.draft_id;
    checkpoint("Agent drafted through the page, waited for the teacher, then completed with the confirmation");
    // The manual path stays the source of the two-criterion rubric used by the rest of this fixture.
    await click("手动起草");
    await fill("整体评分说明", "两个独立评分点，仅用于浏览器回归，不用于教学质量评测。");
    await fill("评分点 1 评估内容", "解释统一规则");
    await fill("评分点 1 等级 1 描述", "未解释统一规则");
    await fill("评分点 1 等级 2 描述", "明确解释统一规则");
    await fill("评分点 1 等级 2 分数", "6.00");
    await click("添加评分点");
    await fill("评分点 2 评估内容", "解释历史追溯");
    await fill("评分点 2 等级 1 描述", "未解释历史追溯");
    await fill("评分点 2 等级 2 描述", "明确解释历史追溯");
    await fill("评分点 2 等级 2 分数", "4.00");
    const { body: draft } = await post(`/v1/question-versions/${question.id}/rubric-drafts`, () => click("保存草稿，准备确认"));
    assert.equal(draft.rubric.criteria.length, 2);
    await click("查看并确认这份草稿");
    const { body: rubric } = await post(`/v1/rubric-drafts/${draft.id}/confirm`, () => click("确认并固定评分细则"));
    // Version 1 was the agent draft confirmed above; the manual rubric is version 2.
    assert.equal(rubric.version, 2);
    report.ids.rubric = rubric.id;
    checkpoint("Created isolated roster, question and confirmed rubric through the UI");
    await click("使用这份固定细则，导入答案");

    const imports = [
      { external_student_id: students[0].external_id, external_submission_id: "source:a", source_revision: "v1", answer_text: RAW },
      { external_student_id: students[0].external_id, external_submission_id: "source", source_revision: "a:v1", answer_text: "另一个提交版本，不纳入本批次。" },
      { external_student_id: students[1].external_id, external_submission_id: "source-b", source_revision: "initial", answer_text: "" },
    ];
    await page.getByText("粘贴结构化答案", { exact: true }).click();
    const originalInput = JSON.stringify(imports);
    await fill("结构化答案 JSON", originalInput);
    const previewPath = `/v1/assignments/${assignment.id}/imports/preview`;
    const preview = await post(previewPath, () => click("检查并预览本次导入"));
    assert.equal(preview.body.valid, true);
    assert.equal(preview.body.row_count, 3);
    assert.equal(preview.sent.rows[0].answer_text, RAW);
    await eventually("three distinct preview identities", async () => {
      const keys = await page.locator(".import-preview tr.ant-table-row").evaluateAll((rows) => rows.map((row) => row.dataset.rowKey));
      assert.equal(keys.length, 3);
      assert.equal(new Set(keys).size, 3);
      assert.deepEqual(keys.map((key) => JSON.parse(key)), preview.sent.rows.map((row) =>
        [row.student_id, row.question_version_id, row.external_submission_id, row.source_revision]));
    });
    await fill("结构化答案 JSON", JSON.stringify(imports.map((row, index) => index === 0 ? { ...row, answer_text: "已修改" } : row)));
    await eventually("editing invalidates the old preview", async () => {
      assert(await page.getByRole("button", { name: "确认导入预览中的答案", exact: true }).isDisabled());
      assert.equal(await page.locator(".import-preview").count(), 0);
    });
    await fill("结构化答案 JSON", originalInput);
    await post(previewPath, () => click("检查并预览本次导入"));
    const { body: imported, sent: committed } = await post(`/v1/assignments/${assignment.id}/imports`, () => click("确认导入预览中的答案"));
    assert.deepEqual(committed, preview.sent);
    assert.equal(imported.reused, 0);
    assert.equal(imported.answer_version_ids.length, 3);
    assert.equal(new Set(imported.answer_version_ids).size, 3);
    report.ids.answers = imported.answer_version_ids;
    checkpoint("Colon-bearing identities stay distinct; preview invalidation and exact raw text are asserted");
    await click("选择答案并创建批次");
    const selectedAnswers = [imported.answer_version_ids[0], imported.answer_version_ids[2]];
    for (const id of selectedAnswers) await page.locator(`tr[data-row-key="${id}"]`).getByRole("checkbox").check();
    if (!SIMULATION) {
      // The page defaults to the simulator; a real run must opt in explicitly.
      await page.getByLabel("批次评估配置", { exact: true }).click();
      await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content")
        .filter({ hasText: process.env.TA_E2E_PROFILE_LABEL || "jev-" }).first().click();
    }
    await click("检查批次并创建");
    const { body: batch, sent: batchInput } = await post("/v1/grading-batches", () => page.getByRole("dialog").getByRole("button", { name: "创建批次", exact: true }).click());
    assert.deepEqual([...batchInput.answer_version_ids].sort(), [...selectedAnswers].sort());
    assert.equal(batchInput.rubric_version_id, rubric.id);
    assert.equal(batchInput.evaluation_profile, PROFILE);
    assert.equal(batch.item_ids.length, 2);
    report.ids.batch = batch.id;
    report.ids.items = batch.item_ids;
    await click("打开批次，查看进度与复核");
    await eventually("both answers succeed (settled alone is insufficient)", async () => {
      const detail = await get(`/v1/grading-batches/${batch.id}`);
      assert.equal(detail.status, "settled");
      assert.equal(detail.outcome, "all_succeeded");
      for (const [key, value] of Object.entries({ total: 2, succeeded: 2, failed: 0, confirmed: 0, awaiting_review: 2 })) {
        assert.equal(key === "failed" ? detail.counts[key] ?? 0 : detail.counts[key], value, `batch.counts.${key}`);
      }
    }, 60_000);
    await stat("答案总数", 2);
    await stat("待老师复核", 2);
    await stat("处理失败", 0);
    const items = await Promise.all(batch.item_ids.map((id) => get(`/v1/grading-items/${id}`)));
    const primary = items.find((item) => item.student.id === students[0].id);
    const blank = items.find((item) => item.student.id === students[1].id);
    assert(primary && blank);
    const criteriaById = new Map(draft.rubric.criteria.map((criterion) => [criterion.id, criterion]));
    for (const item of items) {
      assert.equal(item.status, "succeeded");
      assert.equal(item.evaluation_profile, PROFILE);
      assert.equal(item.simulation, SIMULATION);
      assert.equal(item.question.id, question.id);
      assert.equal(item.rubric.id, rubric.id);
      assert.equal(item.assessments.length, 2);
      // The score is derived in code from the selected levels, never taken from a provider number.
      const derived = item.assessments.reduce((total, assessment) => {
        if (assessment.candidate_level_id === null) {
          assert(assessment.issues.includes("abstained"), "A missing level must be reported as abstention");
          return total;
        }
        const level = criteriaById.get(assessment.criterion_id).levels
          .find((candidate) => candidate.id === assessment.candidate_level_id);
        assert(level, `${assessment.criterion_id} must select a level of the confirmed rubric`);
        return total + Number(level.points);
      }, 0).toFixed(2);
      assert.equal(item.suggested_score, derived);
      assert.equal(item.confirmed_score, null);
    }
    assert.equal(primary.answer.id, selectedAnswers[0]);
    assert.equal(primary.answer.source_revision, "v1");
    assert.equal(primary.answer.text, RAW);
    assert.equal(blank.answer.id, selectedAnswers[1]);
    assert.equal(blank.answer.text, "");
    checkpoint(`Exactly the selected versions completed successfully with ${PROFILE}; both remain unconfirmed`);

    await page.locator(`tr[data-row-key="${primary.id}"]`).getByRole("button", { name: "开始复核", exact: true }).click();
    const criteria = draft.rubric.criteria;
    // Remove any simulator evidence before selecting the precise second occurrence.
    for (let point = 1; point <= 2; point++) {
      const remove = page.getByRole("button", { name: new RegExp(`^移除评分点 ${point} 引用 `) });
      await page.locator(`#reason-${criteria[point - 1].id}`).waitFor();
      for (let count = await remove.count(); count > 0; count--) {
        await remove.first().click();
        await eventually("previous citation removed", async () => assert.equal(await remove.count(), count - 1));
      }
    }
    await page.locator(".criterion-panel").nth(0).getByRole("radio", { name: /6\.00 分/ }).check();
    await selectCitation(page, RAW, CITATION.start, CITATION.end);
    await click("加入该评分点");
    const reasons = ["合成回归：确认第二处相同短句的引用坐标。", "合成回归：检查全文并确认第二评分点。"];
    await page.locator(`#reason-${criteria[0].id}`).fill(reasons[0]);
    await page.locator(".criterion-panel").nth(1).getByRole("radio", { name: /4\.00 分/ }).check();
    await page.locator(`#support-${criteria[1].id}`).click();
    await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content").filter({ hasText: "结合完整答案人工判断" }).click();
    await page.locator(`#reason-${criteria[1].id}`).fill(reasons[1]);
    const expectedDecisions = criteria.map((criterion, index) => ({
      criterion_id: criterion.id, level_id: "complete", reason: reasons[index],
      evidence_support: index === 0 ? "supported" : "teacher_judgment",
      citations: index === 0 ? [CITATION] : [],
    }));
    const { body: review, sent: reviewInput } = await post(`/v1/grading-items/${primary.id}/reviews`, () => click("确认全部评分点"));
    assert.deepEqual(reviewInput, { expected_revision: 0, decisions: expectedDecisions });
    assert.equal(review.revision, 1);
    assert.equal(review.confirmed_score, "10.00");
    await page.getByText("已保存 1 次复核", { exact: true }).waitFor();
    await eventually("displayed confirmed score", async () => {
      assert.equal(await page.locator(".score-summary > span").filter({ hasText: "当前已确认" }).locator("strong").textContent(), "10.00");
    });
    const confirmed = await get(`/v1/grading-items/${primary.id}`);
    assert.equal(confirmed.review_revision, 1);
    assert.equal(confirmed.confirmed_score, "10.00");
    assert.equal(confirmed.reviews.length, 1);
    assert.deepEqual(confirmed.reviews[0].decisions.map((saved) => {
      // Persisted decisions carry the answer and rubric version each one was bound to.
      assert.equal(saved.answer_version_id, primary.answer.id);
      assert.equal(saved.rubric_version_id, rubric.id);
      const { answer_version_id, rubric_version_id, ...decision } = saved;
      return decision;
    }), expectedDecisions);
    assert.equal(Array.from(confirmed.answer.text).slice(CITATION.start, CITATION.end).join(""), CITATION.quote);
    const stillPending = await get(`/v1/grading-items/${blank.id}`);
    assert.equal(stillPending.review_revision, 0);
    assert.equal(stillPending.confirmed_score, null);
    checkpoint("Review persisted score 10.00, revision 1 and second-occurrence citation [8,11); other student stays pending");
    await click("← 返回答案列表");
    await stat("已确认", 1);
    await stat("待老师复核", 1);
    await page.getByText("导出快照", { exact: true }).click();

    for (const mode of ["working", "confirmed_only"]) {
      await page.getByRole("radio", { name: mode === "working" ? "工作汇总 · 包含未确认记录" : "仅已确认结果", exact: true }).check();
      const { body: exported, sent } = await post(`/v1/grading-batches/${batch.id}/exports`, () => click("创建导出快照"));
      assert.deepEqual(sent, { mode });
      const snapshot = exported.snapshot;
      assert.equal(snapshot.schema_version, "grading-export-v1");
      assert.equal(snapshot.mode, mode);
      assert.equal(snapshot.batch_id, batch.id);
      assert.equal(snapshot.assignment_id, assignment.id);
      assert.equal(snapshot.question_version_id, question.id);
      assert.equal(snapshot.rubric_version_id, rubric.id);
      assert.equal(snapshot.rubric_hash, rubric.content_hash);
      assert.deepEqual(snapshot.counts, {
        assignment_roster: 2, selected_answers: 2, confirmed: 1, not_confirmed: 1,
        exported: mode === "working" ? 2 : 1, excluded: mode === "working" ? 0 : 1, failed: 0,
      });
      const expectedItems = mode === "working" ? [confirmed, stillPending] : [confirmed];
      assert.equal(snapshot.rows.length, expectedItems.length);
      assert.deepEqual(snapshot.rows.map((row) => row.id).sort(), expectedItems.map((item) => item.id).sort());
      for (const item of expectedItems) {
        const row = snapshot.rows.find((value) => value.id === item.id);
        assert.equal(row.student_id, item.student.id);
        assert.equal(row.external_student_id, item.student.external_id);
        assert.equal(row.student_name, item.student.name);
        assert.equal(row.review_status, item.review_revision ? "confirmed" : "pending");
        for (const field of ["answer", "question", "rubric", "assessments", "reviews", "review_revision", "suggested_score", "confirmed_score", "evaluation_profile", "simulation"]) {
          assert.deepEqual(row[field], item[field], `snapshot ${field} for ${item.id}`);
        }
      }
      const json = await downloadFile(page, exported.id, "json", directory);
      assert.deepEqual(JSON.parse(json.toString("utf8")), exported, "JSON download must be this exact snapshot");
      const csv = csvRecords(await downloadFile(page, exported.id, "csv", directory));
      assert.deepEqual(csv.columns, CSV_COLUMNS);
      const expectedCsv = expectedItems.map((item) => ({
        student_id: item.student.id, external_student_id: item.student.external_id,
        student_name: item.id === blank.id ? "'=学生乙" : '学生甲,"甲"',
        execution_status: "succeeded", review_status: item.review_revision ? "confirmed" : "pending",
        suggested_score: item.suggested_score, confirmed_score: item.review_revision ? "10.00" : "", review_revision: String(item.review_revision),
        answer_version_id: item.answer.id, rubric_version_id: rubric.id, evaluation_profile: PROFILE,
        simulation: item.simulation ? "True" : "False",
      }));
      const byStudent = (a, b) => a.student_id.localeCompare(b.student_id);
      assert.deepEqual(csv.rows.sort(byStudent), expectedCsv.sort(byStudent), "CSV identities, scores, confirmation and escaping");
      report.ids[`${mode}Export`] = exported.id;
      checkpoint(`${mode}: asserted snapshot scope, exact JSON and every CSV field from actual UI downloads`);
    }
    assert.deepEqual(browserErrors, [], "Browser errors or duplicate row keys must fail the regression");
    await page.screenshot({ path: join(directory, "completed.png"), fullPage: true });
    report.status = "passed";
  } catch (error) {
    report.status = "failed";
    report.error = redact(error.stack || error);
    // Do not capture the login form, or save traces/HARs containing authorization headers.
    if (await page.getByRole("heading", { name: assignmentTitle, exact: true }).isVisible().catch(() => false)) {
      await page.screenshot({ path: join(directory, "failed.png"), fullPage: true, timeout: 5_000 }).catch(() => {});
    }
    throw new Error(report.error);
  } finally {
    report.finishedAt = new Date().toISOString();
    report.browserErrors = browserErrors;
    await writeFile(join(directory, "result.json"), JSON.stringify(report, null, 2));
    await context.close();
  }
});
