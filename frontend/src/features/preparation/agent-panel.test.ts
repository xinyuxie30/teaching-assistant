import { act, createElement } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiClient } from "../../api/client";
import type { AgentRun, QuestionDetail } from "../../api/types";
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

function serve(list: () => AgentRun[], post?: (options: RequestInit) => Response) {
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
    throw new Error(`Unexpected read: ${path}`);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function render(api: ApiClient, store = new PreparationSession(), onOpenDraft = vi.fn()) {
  await act(async () => {
    root.render(createElement(AgentPanel, { api, store, question: { id: "question-1" } as QuestionDetail, onOpenDraft }));
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
  await render(new ApiClient("test"));
  await fillFeedback("修改已确认草稿的下一版");
  const select = container.querySelector("#agent-history-question-1") as HTMLSelectElement;
  await act(async () => { select.value = "saved-run"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).value).toBe("");
  await act(async () => { select.value = "revised-run"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  expect((container.querySelector("#agent-feedback-question-1") as HTMLTextAreaElement).value).toBe("修改已确认草稿的下一版");
  expect(button("按修改意见生成新版").disabled).toBe(false);
});
