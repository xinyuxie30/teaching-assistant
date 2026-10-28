import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";
import { chromium } from "playwright-core";
import { configuration, postAction, root } from "./support.mjs";

test("class history → bank retrieval → durable draft → teacher confirmation", { timeout: 150_000 }, async () => {
  const config = configuration();
  const browser = await chromium.launch(config.browserOptions);
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const directory = join(root, ".local/e2e/class-practice");
  await mkdir(directory, { recursive: true });
  const click = name => page.getByRole("button", { name, exact: true }).click();
  const selectFirst = async label => {
    await page.getByRole("combobox", { name: label, exact: true }).click();
    const title = label === "补练课程" ? "Test course" : "Test classroom";
    await page.locator(`.ant-select-item-option[title="${title}"]`).click();
  };
  try {
    await page.goto(config.origin);
    await page.getByLabel("开发访问令牌", { exact: true }).fill(config.token);
    await click("进入工作台");
    await page.getByRole("button", { name: "针对性补练" }).click();
    await selectFirst("补练课程");
    await selectFirst("补练班级");
    // Add and explicitly confirm a second bank question through the product UI.
    await page.getByRole("tab", { name: "课程题库", exact: true }).click();
    for (const [label, value] of Object.entries({ "题目标题": "浏览器题：历史追溯", "题库年级": "七年级", "题库知识点": "历史追溯,统一规则", "题干": "保留历史版本如何帮助复核？", "参考答案": "可以追溯当时的固定规则和原始结果。", "题目来源": "教师自编浏览器合成题" })) await page.getByLabel(label, { exact: true }).fill(value);
    await click("保存待确认题目");
    await click("确认入库");
    await page.getByText("待确认", { exact: true }).waitFor({ state: "hidden" });
    await page.getByRole("tab", { name: "班级补练", exact: true }).click();
    await page.locator(".practice-batch input[type=checkbox]").first().check();
    await page.getByLabel("补练题数", { exact: true }).fill("1");
    await click("检索适配题目");
    await page.getByRole("heading", { name: "2. 查看证据与候选题", exact: true }).waitFor();
    assert.match(await page.locator(".practice-evidence").first().textContent(), /已确认 1\/2/);
    await page.getByRole("checkbox", { name: "选择此题：浏览器题：历史追溯", exact: true }).check();
    // Emulate response loss after a committed create; retry must return the same run.
    let lost = false;
    let committedId;
    await page.route("**/v1/classrooms/*/practice-runs", async route => {
      if (route.request().method() !== "POST") return route.continue();
      assert.equal(route.request().postDataJSON().agent_profile, "fake-class-practice-v1");
      if (!lost) {
        const result = await route.fetch();
        assert(result.ok());
        committedId = (await result.json()).id;
        lost = true;
        await route.abort("failed");
      } else await route.continue();
    });
    await click("生成补练草稿");
    await page.getByText("提交结果未知，请先重试原请求。", { exact: true }).waitFor();
    await click("重试原请求");
    await page.getByRole("button", { name: "确认这份补练", exact: true }).waitFor({ timeout: 30_000 });
    const path = `/v1/agent-runs/${committedId}/confirm`;
    const result = await postAction(page, config.origin, path, () => click("确认这份补练"));
    assert.equal(result.body.status, "completed");
    await page.getByRole("button", { name: "教师已确认", exact: true }).waitFor();
    await page.screenshot({ path: join(directory, "desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), "No horizontal overflow on mobile");
    await page.screenshot({ path: join(directory, "mobile.png"), fullPage: true });
    // Reload and locate the saved task via server-backed history.
    await page.reload();
    await page.getByLabel("开发访问令牌", { exact: true }).fill(config.token);
    await click("进入工作台");
    await page.getByRole("button", { name: "针对性补练" }).click();
    await selectFirst("补练课程");
    await selectFirst("补练班级");
    await page.getByRole("button").filter({ hasText: /· 已确认$/ }).click();
    await page.getByRole("button", { name: "教师已确认", exact: true }).waitFor();
    assert.deepEqual(errors, []);
    await writeFile(join(directory, "report.json"), JSON.stringify({ status: "passed", runId: committedId, responseLossRecovered: lost, widths: [1440, 390], browserErrors: errors }, null, 2));
  } catch (error) {
    await page.screenshot({ path: join(directory, "failure.png"), fullPage: true });
    throw error;
  } finally {
    await browser.close();
  }
});
