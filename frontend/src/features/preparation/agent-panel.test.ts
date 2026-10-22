import { act, createElement } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiClient } from "../../api/client";
import type { AgentRun, DraftDetail, QuestionDetail } from "../../api/types";
import { AgentPanel } from "./AgentPanel";
import { PreparationSession } from "./session";

// Keep the real panel, hooks and HTTP client. Native controls stand in for antd's layout.
vi.mock("antd", () => ({
  Button: ({ children, onClick, disabled }: { children?: ReactNode; onClick?: () => void; disabled?: boolean }) =>
    createElement("button", { onClick, disabled }, children),
  Tag: ({ children }: { children?: ReactNode }) => createElement("span", null, children),
  Alert: ({ title }: { title?: ReactNode }) => createElement("div", null, title),
  Select: ({ id, value, disabled, onChange, options }: {
    id?: string; value?: string; disabled?: boolean; onChange: (value: string) => void;
    options: { value: string; label: string; disabled?: boolean }[];
  }) => createElement("select", { id, value: value ?? "", disabled,
    onChange: (event: { target: { value: string } }) => onChange(event.target.value) },
  createElement("option", { value: "" }, "选择"),
  ...options.map((option) => createElement("option", { key: option.value, value: option.value, disabled: option.disabled }, option.label))),
  Input: { TextArea: ({ id, value, disabled, onChange }: {
    id?: string; value?: string; disabled?: boolean; onChange?: (event: { target: { value: string } }) => void;
  }) => createElement("textarea", { id, value, disabled, onInput: onChange, readOnly: !onChange }) },
}));

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

const record: AgentRun = {
  id: "saved-run", question_version_id: "question-1", status: "waiting_teacher", error_code: null,
  draft_id: "draft-1", waiting_for: "teacher_rubric_confirmation", profile_id: "agent-fake",
  manifest: { provider: "fake", model: "fake-rubric-agent-v1" }, simulation: true,
  limits: { max_model_calls: 6, max_tool_calls: 10, max_context_characters: 40000, max_seconds: 300 },
  models_used: 2, tools_used: 2, attempts: 1, epoch: 1, event_sequence: 125,
  expires_at: "2026-10-22T03:00:00Z", created_at: "2026-10-22T02:55:00Z",
};
const response = (value: unknown) => new Response(JSON.stringify(value), {
  status: 200, headers: { "Content-Type": "application/json" },
});
const page = (items: AgentRun[]) => ({ items, total: items.length, limit: 20, has_more: false, next_cursor: null });

function serve(list: () => AgentRun[], post?: (options: RequestInit) => Response, drafts: DraftDetail[] = []) {
  const fetch = vi.fn(async (path: string, options?: RequestInit) => {
    const url = new URL(path, window.location.origin);
    if (options?.method === "POST") {
      if (!post) throw new Error("Unexpected write");
      return post(options);
    }
    if (url.pathname === "/v1/agent-profiles") return response({ default_alias: "fake-rubric-agent-v1", items: [
      { alias: "fake-rubric-agent-v1", id: "agent-fake", ready: true, manifest: record.manifest },
    ] });
    if (url.pathname === "/v1/agent-runs") {
      expect(url.searchParams.get("question_version_id")).toBe("question-1");
      return response(page(list()));
    }
    if (url.pathname.endsWith("/events")) {
      const after = Number(url.searchParams.get("after"));
      return response({ items: [{ sequence: after + 1, epoch: 1, kind: "waiting_teacher",
        created_at: record.created_at, body: {} }], next_after: after + 1, has_more: false });
    }
    if (url.pathname === "/v1/agent-runs/saved-run") return response(record);
    const saved = list().find((item) => url.pathname === `/v1/agent-runs/${item.id}`);
    if (saved) return response(saved);
    const draft = drafts.find((item) => url.pathname === `/v1/rubric-drafts/${item.id}`);
    if (draft) return response(draft);
    throw new Error(`Unexpected read: ${path}`);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function render(api: ApiClient, store = new PreparationSession(), onOpenDraft = vi.fn(),
  source: { sourceDraftId?: string; onClearSource?: () => void } = {}) {
  await act(async () => {
    root.render(createElement(AgentPanel, { api, store, question: { id: "question-1" } as QuestionDetail, onOpenDraft, ...source }));
  });
}

function button(label: string) {
  const value = [...container.querySelectorAll("button")].find((node) => node.textContent === label);
  if (!value) throw new Error(`Missing button: ${label}`);
  return value;
}

it("restores a persisted task in a fresh page session and fetches events beyond the first 100", async () => {
  const fetch = serve(() => [record]);
  await render(new ApiClient("test"));
  expect((container.querySelector("#agent-history-question-1") as HTMLSelectElement).value).toBe("saved-run");
  expect(button("打开草稿并确认")).toBeDefined();
  expect(container.textContent).toContain("重领 0 次");
  expect(fetch.mock.calls.some(([path]) => path.includes("after=117&limit=8"))).toBe(true);
  expect(fetch.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
});

it("adopts the original task after an uncertain create is retried with the same key", async () => {
  let writes = 0;
  const fetch = serve(() => writes ? [record] : [], () => {
    writes += 1;
    if (writes === 1) throw new TypeError("response lost after commit");
    return response(record);
  });
  await render(new ApiClient("test"));
  await act(async () => button("开始起草").click());
  expect(button("开始起草").disabled).toBe(true);
  await act(async () => button("重试同一次提交").click());
  expect(button("打开草稿并确认")).toBeDefined();
  const posted = fetch.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(posted).toHaveLength(2);
  expect(posted[1][1]?.body).toBe(posted[0][1]?.body);
  expect(new Headers(posted[1][1]?.headers).get("Idempotency-Key"))
    .toBe(new Headers(posted[0][1]?.headers).get("Idempotency-Key"));
});

it("keeps historical selection after remount instead of replaying an already adopted receipt", async () => {
  const old = { ...record, id: "old-run", status: "completed" as const };
  const fetch = serve(() => [record, old], () => response(record));
  const original = fetch.getMockImplementation()!;
  fetch.mockImplementation((path, options) => path === "/v1/agent-runs/old-run"
    ? Promise.resolve(response(old)) : original(path, options));
  const api = new ApiClient("test");
  const operation = api.operation("prepare:agent:question-1");
  await operation.run("/v1/question-versions/question-1/rubric-agent-runs", {});
  const store = new PreparationSession();
  store.set("agent:question-1", { runId: "old-run", acceptedCreation: operation.snapshot().ticket!.key }, false);
  await render(api, store);
  expect((container.querySelector("#agent-history-question-1") as HTMLSelectElement).value).toBe("old-run");
  expect(container.textContent).toContain("已完成");
});

async function fillFeedback(value: string) {
  await act(async () => {
    const input = container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement;
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const revision: AgentRun = {
  ...record, id: "revised-run", draft_id: "revised-draft",
  source_draft: { id: "draft-1", revision: 1, content_hash: "source-hash" },
  revision_feedback: "细化部分得分的描述",
};

it("locks revision inputs after a lost response and retries the same source and feedback", async () => {
  let writes = 0;
  const fetch = serve(() => writes ? [revision, record] : [record], () => {
    writes += 1;
    if (writes === 1) throw new TypeError("response lost after commit");
    return response(revision);
  });
  await render(new ApiClient("test"));
  expect(button("按修改意见生成新版").disabled).toBe(true);
  await fillFeedback("  \n ");
  expect(button("按修改意见生成新版").disabled).toBe(true);
  await fillFeedback("细化部分得分的描述");
  expect(button("按修改意见生成新版").disabled).toBe(false);
  await act(async () => button("按修改意见生成新版").click());
  expect(button("开始起草").disabled).toBe(true);
  expect(button("按修改意见生成新版").disabled).toBe(true);
  expect(button("打开草稿并确认").disabled).toBe(true);
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).disabled).toBe(true);
  expect((container.querySelector("#agent-history-question-1") as HTMLSelectElement).disabled).toBe(true);
  await act(async () => button("重试同一次提交").click());
  const posted = fetch.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(posted).toHaveLength(2);
  expect(JSON.parse(posted[0][1]?.body as string)).toEqual({
    agent_profile: "fake-rubric-agent-v1", source_draft_id: "draft-1", instructions: "细化部分得分的描述",
  });
  expect(posted[1][1]?.body).toBe(posted[0][1]?.body);
  expect(new Headers(posted[1][1]?.headers).get("Idempotency-Key"))
    .toBe(new Headers(posted[0][1]?.headers).get("Idempotency-Key"));
  expect((container.querySelector("#agent-history-question-1") as HTMLSelectElement).value).toBe("revised-run");
  expect(container.textContent).toContain("修改意见：细化部分得分的描述");
  // Feedback is bound to its source task, so the new draft starts with an empty form.
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).value).toBe("");
});

it("restores a revision's provenance and opens the original and new drafts separately", async () => {
  const fetch = serve(() => [revision, record]);
  const open = vi.fn();
  await render(new ApiClient("test"), new PreparationSession(), open);
  expect(container.textContent).toContain("修改意见：细化部分得分的描述");
  await act(async () => button("查看来源草稿").click());
  expect(open).toHaveBeenLastCalledWith("draft-1");
  await act(async () => button("打开草稿并确认").click());
  expect(open).toHaveBeenLastCalledWith("revised-draft");
  expect(fetch.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
});

it("allows a new revision from a completed draft and keeps feedback scoped to its source", async () => {
  const completed = { ...revision, status: "completed" as const };
  serve(() => [completed, record]);
  const store = new PreparationSession();
  store.set("agent:question-1", { runId: "revised-run" }, false);
  await render(new ApiClient("test"), store);
  await fillFeedback("修改已确认草稿的下一版");
  const select = container.querySelector("#agent-history-question-1") as HTMLSelectElement;
  await act(async () => { select.value = "saved-run"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).value).toBe("");
  await act(async () => { select.value = "revised-run"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).value).toBe("修改已确认草稿的下一版");
  expect(button("按修改意见生成新版").disabled).toBe(false);
});

it("keeps drafting available when the task history cannot be read", async () => {
  const fetch = vi.fn(async (path: string) => {
    const url = new URL(path, window.location.origin);
    if (url.pathname === "/v1/agent-profiles") {
      return response({ default_alias: "fake-rubric-agent-v1", items: [
        { alias: "fake-rubric-agent-v1", id: "agent-fake", ready: true, manifest: record.manifest },
      ] });
    }
    if (url.pathname === "/v1/agent-runs") {
      return new Response(JSON.stringify({ code: "http_500" }), { status: 500 });
    }
    throw new Error(`Unexpected read: ${path}`);
  });
  vi.stubGlobal("fetch", fetch);
  await render(new ApiClient("test"));
  expect(button("开始起草").disabled).toBe(false);
});

it("restores an earlier waiting task instead of a newer cancelled one", async () => {
  const waiting = { ...record, id: "waiting-run", created_at: "2026-10-22T02:00:00Z" };
  const cancelled = { ...record, id: "cancelled-run", status: "cancelled" as const,
    draft_id: null, created_at: "2026-10-22T02:10:00Z" };
  serve(() => [cancelled, waiting]);
  await render(new ApiClient("test"));
  expect((container.querySelector("#agent-history-question-1") as HTMLSelectElement).value).toBe("waiting-run");
});

it.each(["waiting_input", "waiting_timer"] as const)("restores and explains a %s task and allows cancellation", async (status) => {
  let paused: AgentRun = { ...record, id: "paused-run", status, draft_id: null, waiting_for: null,
    wait: { token: "wait-token", root_run_id: "paused-run", reason: "等待课堂资料",
      wake_at: status === "waiting_timer" ? "2026-10-22T04:00:00Z" : null } };
  const ended: AgentRun = { ...record, id: "ended-run", status: "cancelled", draft_id: null };
  const fetch = serve(() => [ended, paused], () => {
    paused = { ...paused, status: "cancelled", wait: null };
    return response(paused);
  });
  await render(new ApiClient("test"));
  expect((container.querySelector("#agent-history-question-1") as HTMLSelectElement).value).toBe("paused-run");
  expect(container.textContent).toContain(status === "waiting_input" ? "等待补充信息后恢复" : "等待预约时间恢复");
  expect(container.textContent).toContain("暂停期间不消耗执行时限");
  expect(container.textContent).toContain("暂停原因：等待课堂资料");
  if (status === "waiting_timer") expect(container.textContent).toContain("预约恢复时间");
  expect(button("开始起草").disabled).toBe(true);
  expect(button("取消任务").disabled).toBe(false);
  await act(async () => button("取消任务").click());
  const writes = fetch.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(writes).toHaveLength(1);
  expect(writes[0][0]).toBe("/v1/agent-runs/paused-run/cancel");
  expect(container.textContent).not.toContain("暂停原因：等待课堂资料");
  expect(button("开始起草").disabled).toBe(false);
  expect([...container.querySelectorAll("button")].some((node) => node.textContent === "取消任务")).toBe(false);
});

const manualDraft: DraftDetail = {
  id: "manual-draft", question_version_id: "question-1", revision: 1,
  confirmed_version_id: null, created_at: record.created_at,
  body: { global_instructions: "老师编写的规则", selection_policy: "unique-argmax-v1", rounding_policy: "total-half-up-2dp-v1",
    criteria: [{ id: "point", description: "说明原因", semantic_version: 1, maximum_points: "5.00", levels: [
      { id: "low", ordinal: 0, description: "没有解释", points: "0.00" },
      { id: "full", ordinal: 1, description: "完整解释", points: "5.00" },
    ] }],
  },
};

it("revises a saved manual draft without any earlier agent task and adopts its result", async () => {
  let created = false;
  const child = { ...revision, source_draft: { ...revision.source_draft!, id: manualDraft.id } };
  const fetch = serve(() => created ? [child] : [], () => { created = true; return response(child); }, [manualDraft]);
  const clear = vi.fn();
  const open = vi.fn();
  await render(new ApiClient("test"), new PreparationSession(), open, { sourceDraftId: manualDraft.id, onClearSource: clear });
  expect(container.textContent).toContain("对所选草稿的修改意见");
  expect(container.textContent).toContain("1 个评分点 · 未确认");
  expect(button("按修改意见生成新版").disabled).toBe(true);
  await act(async () => button("查看所选草稿").click());
  expect(open).toHaveBeenCalledWith(manualDraft.id);
  expect(fetch.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
  await fillFeedback("  明确部分得分  ");
  await act(async () => button("按修改意见生成新版").click());
  const writes = fetch.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(writes).toHaveLength(1);
  expect(JSON.parse(writes[0][1]?.body as string)).toEqual({
    agent_profile: "fake-rubric-agent-v1", source_draft_id: manualDraft.id, instructions: "明确部分得分",
  });
  expect(clear).toHaveBeenCalledOnce();
  expect((container.querySelector("#agent-history-question-1") as HTMLSelectElement).value).toBe(child.id);
});

it("keeps the selected draft and its feedback independent of the restored task and other drafts", async () => {
  const other = { ...manualDraft, id: "other-draft", confirmed_version_id: "fixed-version" };
  const fetch = serve(() => [record], undefined, [manualDraft, other]);
  const api = new ApiClient("test");
  const store = new PreparationSession();
  const clear = vi.fn();
  const open = vi.fn();
  await render(api, store, open, { sourceDraftId: manualDraft.id, onClearSource: clear });
  await fillFeedback("对手动草稿的意见");
  await render(api, store, open, { sourceDraftId: other.id, onClearSource: clear });
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).value).toBe("");
  expect(container.textContent).toContain("1 个评分点 · 已确认");
  await fillFeedback("对另一份草稿的意见");
  await render(api, store, open, { sourceDraftId: manualDraft.id, onClearSource: clear });
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).value).toBe("对手动草稿的意见");
  await act(async () => button("取消草稿选择").click());
  expect(clear).toHaveBeenCalledOnce();
  expect(fetch.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
  await render(api, store, open);
  expect(container.textContent).toContain("对当前草稿的修改意见");
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).value).toBe("");
});

it("locks a manual source after a lost response and retries exactly the same revision", async () => {
  let writes = 0;
  const child = { ...revision, source_draft: { ...revision.source_draft!, id: manualDraft.id } };
  const fetch = serve(() => writes ? [child, record] : [record], () => {
    if (++writes === 1) throw new TypeError("response lost after commit");
    return response(child);
  }, [manualDraft]);
  const clear = vi.fn();
  await render(new ApiClient("test"), new PreparationSession(), vi.fn(), { sourceDraftId: manualDraft.id, onClearSource: clear });
  await fillFeedback("明确部分得分");
  await act(async () => button("按修改意见生成新版").click());
  expect(button("取消草稿选择").disabled).toBe(true);
  expect(button("查看所选草稿").disabled).toBe(true);
  expect(button("开始起草").disabled).toBe(true);
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).disabled).toBe(true);
  expect(clear).not.toHaveBeenCalled();
  await act(async () => button("重试同一次提交").click());
  const posted = fetch.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(posted).toHaveLength(2);
  expect(JSON.parse(posted[0][1]?.body as string).source_draft_id).toBe(manualDraft.id);
  expect(posted[1][1]?.body).toBe(posted[0][1]?.body);
  expect(new Headers(posted[1][1]?.headers).get("Idempotency-Key"))
    .toBe(new Headers(posted[0][1]?.headers).get("Idempotency-Key"));
  expect(clear).toHaveBeenCalledOnce();
});

it("does not clear a newly selected source when remounting with an already adopted receipt", async () => {
  serve(() => [revision, record], () => response(revision), [manualDraft]);
  const api = new ApiClient("test");
  const operation = api.operation("prepare:agent:question-1");
  await operation.run("/v1/question-versions/question-1/rubric-agent-runs", {
    source_draft_id: record.draft_id, instructions: "旧的意见",
  });
  const store = new PreparationSession();
  store.set("agent:question-1", { runId: revision.id, acceptedCreation: operation.snapshot().ticket!.key }, false);
  const clear = vi.fn();
  await render(api, store, vi.fn(), { sourceDraftId: manualDraft.id, onClearSource: clear });
  expect(container.textContent).toContain("对所选草稿的修改意见");
  expect(clear).not.toHaveBeenCalled();
  await fillFeedback("新来源的意见");
  expect(button("按修改意见生成新版").disabled).toBe(false);
});

it.each(["unreadable", "wrong-question"])("blocks revision of an %s source without sending a write", async (problem) => {
  const fetch = serve(() => [record], undefined, [{ ...manualDraft, question_version_id: "another-question" }]);
  if (problem === "unreadable") {
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation((path, options) => path === `/v1/rubric-drafts/${manualDraft.id}`
      ? Promise.resolve(new Response(JSON.stringify({ code: "not_found" }), { status: 404 })) : original(path, options));
  }
  await render(new ApiClient("test"), new PreparationSession(), vi.fn(), { sourceDraftId: manualDraft.id });
  expect(button("按修改意见生成新版").disabled).toBe(true);
  expect(button("查看所选草稿").disabled).toBe(true);
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).disabled).toBe(true);
  expect(container.textContent).toContain(problem === "unreadable" ? "记录不存在" : "不属于当前题目版本");
  expect(fetch.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
});

const questionWait: AgentRun = {
  ...record, id: "question-run", status: "waiting_input", draft_id: null,
  wait: { token: "question-token-1", root_run_id: "question-run", reason: "需要课堂信息", wake_at: null,
    question: { tool_call_id: "ask-1", prompt: "学生是否已经学习过分数？", options: ["学过", "未学过"] } },
};

async function fillAnswer(value: string) {
  await act(async () => {
    const input = container.querySelector("#agent-answer-question-1") as HTMLTextAreaElement;
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

it("shows the saved question, allows suggested or free answers, and wakes with the exact response", async () => {
  let current = questionWait;
  const fetch = serve(() => [current], () => {
    current = { ...current, status: "queued", wait: null };
    return response(current);
  });
  await render(new ApiClient("question-token"));
  expect(container.textContent).toContain("学生是否已经学习过分数？");
  expect(button("提交回答并继续").disabled).toBe(true);
  await act(async () => button("学过").click());
  expect((container.querySelector("#agent-answer-question-1") as HTMLTextAreaElement).value).toBe("学过");
  expect(fetch.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
  await fillAnswer("  \n ");
  expect(button("提交回答并继续").disabled).toBe(true);
  await fillAnswer("  学过，但请先复习。  ");
  await act(async () => button("提交回答并继续").click());
  const writes = fetch.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(writes).toHaveLength(1);
  expect(writes[0][0]).toBe("/v1/agent-runs/question-run/wake");
  expect(JSON.parse(writes[0][1]?.body as string)).toEqual({
    wait_token: "question-token-1", response: "  学过，但请先复习。  ",
  });
  expect(new Headers(writes[0][1]?.headers).get("Authorization")).toBe("Bearer question-token");
  expect(new Headers(writes[0][1]?.headers).get("Idempotency-Key")).toBeTruthy();
  expect(container.querySelector("#agent-answer-question-1")).toBeNull();
  expect(container.textContent).toContain("排队中");
});

it("keeps an uncertain answer retry available after refresh observes the resumed task", async () => {
  let current = questionWait;
  let writes = 0;
  const fetch = serve(() => [current], () => {
    current = { ...current, status: "queued", wait: null };
    if (++writes === 1) throw new TypeError("answer committed but response lost");
    return response(current);
  });
  await render(new ApiClient("test"));
  await fillAnswer("未学过，请从具体例子开始。");
  await act(async () => button("提交回答并继续").click());
  expect((container.querySelector("#agent-answer-question-1") as HTMLTextAreaElement).disabled).toBe(true);
  expect(button("取消任务").disabled).toBe(true);
  await act(async () => button("刷新状态").click());
  expect(container.querySelector("#agent-answer-question-1")).toBeNull();
  expect(button("重试同一次提交")).toBeDefined();
  await act(async () => button("重试同一次提交").click());
  const posted = fetch.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(posted).toHaveLength(2);
  expect(posted[1][1]?.body).toBe(posted[0][1]?.body);
  expect(new Headers(posted[1][1]?.headers).get("Idempotency-Key"))
    .toBe(new Headers(posted[0][1]?.headers).get("Idempotency-Key"));
  expect(button("取消任务").disabled).toBe(false);
});

it("isolates answer drafts by wait token and supports questions without suggested options", async () => {
  let current = questionWait;
  const fetch = serve(() => [current]);
  await render(new ApiClient("test"));
  await fillAnswer("前一个问题的草稿");
  current = { ...questionWait, wait: { ...questionWait.wait!, token: "question-token-2",
    question: { tool_call_id: "ask-2", prompt: "需要准备哪些材料？", options: null } } };
  await act(async () => button("刷新状态").click());
  expect(container.textContent).toContain("需要准备哪些材料？");
  expect(container.textContent).not.toContain("学生是否已经学习过分数？");
  expect((container.querySelector("#agent-answer-question-1") as HTMLTextAreaElement).value).toBe("");
  expect(button("提交回答并继续").disabled).toBe(true);
  expect([...container.querySelectorAll("button")].some((item) => item.textContent === "学过")).toBe(false);
  expect(fetch.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
});

it("refreshes a resolved wait after a stale answer is rejected and does not submit again", async () => {
  let current = questionWait;
  const fetch = serve(() => [current], () => {
    current = { ...current, status: "cancelled", wait: null };
    return new Response(JSON.stringify({ code: "agent_wait_already_resolved" }), { status: 409 });
  });
  await render(new ApiClient("test"));
  await fillAnswer("迟到的回答");
  await act(async () => button("提交回答并继续").click());
  expect(container.textContent).toContain("这次等待已经被回答或取消");
  expect(container.textContent).toContain("已取消");
  expect(container.querySelector("#agent-answer-question-1")).toBeNull();
  expect(fetch.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
});
