import { act, createElement } from "react";
import type { ComponentProps, ReactNode } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiClient } from "../../api/client";
import type { DraftDetail, QuestionDetail, Rubric, RubricRevisionComparison } from "../../api/types";
import { SavedRule } from "./RubricPanel";
import { PreparationSession } from "./session";

vi.mock("antd", () => ({
  Button: ({ children, onClick, disabled }: { children?: ReactNode; onClick?: () => void; disabled?: boolean }) =>
    createElement("button", { onClick, disabled }, children),
  Tag: ({ children }: { children?: ReactNode }) => createElement("span", null, children),
  Spin: () => createElement("span", null, "loading"),
}));

const source: Rubric = {
  global_instructions: "原有评分说明", selection_policy: "unique-argmax-v1", rounding_policy: "total-half-up-2dp-v1",
  criteria: ["a", "b"].map((id) => ({
    id, semantic_version: 1, description: `原稿评分点 ${id}`, maximum_points: "5.00",
    levels: [
      { id: "low", ordinal: 0, description: "没有体现", points: "0.00" },
      { id: "full", ordinal: 1, description: "完整解释", points: "5.00" },
    ],
  })),
};
const body: Rubric = {
  ...source, global_instructions: "新的评分说明", criteria: [
    { ...source.criteria[0], description: "新版评分点 a", levels: [
      source.criteria[0].levels[0], { ...source.criteria[0].levels[1], description: "完整解释且说明原因" },
    ] }, source.criteria[1],
  ],
};
const comparison: RubricRevisionComparison = {
  run_id: "run-1", source: { id: "source-1", revision: 1, content_hash: "source-hash" },
  target: { id: "draft-1", revision: 1, content_hash: "target-hash" }, feedback: "明确因果解释", source_body: source,
  difference: {
    has_changes: true, summary: { added: 0, removed: 0, modified: 1, unchanged: 1 },
    global_fields: ["global_instructions"], criterion_order_changed: false,
    criteria: [
      { id: "a", status: "modified", changed_fields: ["description", "levels"], levels: [
        { id: "low", status: "unchanged", changed_fields: [] },
        { id: "full", status: "modified", changed_fields: ["description"] },
      ] },
      { id: "b", status: "unchanged", changed_fields: [], levels: [
        { id: "low", status: "unchanged", changed_fields: [] },
        { id: "full", status: "unchanged", changed_fields: [] },
      ] },
    ],
  },
};
const draft: DraftDetail = {
  id: "draft-1", question_version_id: "question-1", created_at: "2026-10-22T00:00:00Z", revision: 1,
  confirmed_version_id: null, body, revision_comparison: comparison,
};
let root: Root;
let container: HTMLDivElement;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
const response = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
async function render(value: DraftDetail, props: Partial<ComponentProps<typeof SavedRule>> = {}) {
  const fetch = vi.fn(async (path: string, options?: RequestInit) => {
    if (options?.method === "POST") {
      expect(path).toBe("/v1/rubric-drafts/draft-1/confirm");
      return response({ id: "fixed-new-version", version: 2, content_hash: "hash" });
    }
    expect(path).toBe("/v1/rubric-drafts/draft-1");
    return response(value);
  });
  vi.stubGlobal("fetch", fetch);
  await mount(props);
  return fetch;
}
async function mount(props: Partial<ComponentProps<typeof SavedRule>> = {}) {
  await act(async () => root.render(createElement(SavedRule, {
    api: new ApiClient("test"), store: new PreparationSession(), selection: { kind: "draft", id: "draft-1" },
    question: { id: "question-1" } as QuestionDetail, onSelected: vi.fn(), onEdit: vi.fn(),
    ...props,
  })));
}
function button(label: string) {
  const node = [...container.querySelectorAll("button")].find((item) => item.textContent === label);
  if (!node) throw new Error(`Missing button ${label}`);
  return node;
}

it("opens the selected draft for agent feedback without confirming it or creating a task", async () => {
  const revise = vi.fn();
  const fetch = await render({ ...draft, revision_comparison: null }, { onRevise: revise });
  await act(async () => button("让 Agent 修改这份草稿").click());
  expect(revise).toHaveBeenCalledWith(draft.id);
  expect(fetch.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
});

it("prevents changing the source when an agent submission has an unknown outcome", async () => {
  const api = new ApiClient("test");
  vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("response lost"); }));
  await api.operation("prepare:agent:question-1").run("/v1/question-versions/question-1/rubric-agent-runs", {
    source_draft_id: "source-1", instructions: "原来的意见",
  }).catch(() => undefined);
  const revise = vi.fn();
  await render(draft, { api, onRevise: revise });
  expect(button("让 Agent 修改这份草稿").disabled).toBe(true);
  await act(async () => button("让 Agent 修改这份草稿").click());
  expect(revise).not.toHaveBeenCalled();
});

it("shows the captured source beside changes and confirms only the new draft", async () => {
  const fetch = await render(draft);
  const panel = container.querySelector('[aria-label="修订差异"]')!;
  expect(panel.textContent).toContain("原有评分说明");
  expect(panel.textContent).toContain("新的评分说明");
  expect(panel.textContent).toContain("明确因果解释");
  expect(panel.textContent).toContain("原稿评分点 a");
  expect(panel.textContent).toContain("新版评分点 a");
  expect(panel.textContent).toContain("完整解释且说明原因");
  expect(panel.textContent).not.toContain("原稿评分点 b");
  await act(async () => button("显示 1 个未变化评分点").click());
  expect(panel.textContent).toContain("原稿评分点 b");
  await act(async () => button("确认并固定评分细则").click());
  const writes = fetch.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(writes).toHaveLength(1);
  expect(JSON.parse(writes[0][1]?.body as string)).toEqual({ expected_revision: 1 });
  expect(container.textContent).toContain("草稿已确认");
});

it("explicitly reports a revision with no differences", async () => {
  await render({ ...draft, body: source, revision_comparison: { ...comparison, difference: {
    ...comparison.difference, has_changes: false, global_fields: [], summary: { added: 0, removed: 0, modified: 0, unchanged: 2 },
    criteria: comparison.difference.criteria.map((row) => ({ ...row, status: "unchanged", changed_fields: [] })),
  } } });
  expect(container.textContent).toContain("新版与原稿内容一致，请检查是否满足修改意见。");
  expect(button("确认并固定评分细则").disabled).toBe(false);
});

it("does not present an ordinary draft without a source as an unchanged revision", async () => {
  await render({ ...draft, revision_comparison: null });
  expect(container.querySelector('[aria-label="修订差异"]')).toBeNull();
  expect(container.textContent).not.toContain("新版与原稿内容一致");
  expect(button("确认并固定评分细则").disabled).toBe(false);
});

it("shows a read failure instead of a reassuring comparison or a confirmation action", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network lost"); }));
  await mount();
  expect(container.querySelector('[role="alert"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="修订差异"]')).toBeNull();
  expect(container.textContent).not.toContain("确认并固定评分细则");
  expect(button("重新读取")).toBeDefined();
});
