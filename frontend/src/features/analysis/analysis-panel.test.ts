import { act, createElement } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiClient } from "../../api/client";
import type { AnalysisStudent, BatchAnalysis } from "../../api/types";
import { AnalysisPanel } from "./AnalysisPanel";

vi.mock("antd", () => ({
  Button: ({ children, onClick, disabled }: { children?: ReactNode; onClick?: () => void; disabled?: boolean }) =>
    createElement("button", { onClick, disabled }, children),
  Tag: ({ children }: { children?: ReactNode }) => createElement("span", null, children),
  Spin: () => null,
  Empty: ({ description }: { description: ReactNode }) => createElement("p", null, description),
  Select: ({ value, disabled, onChange, options, ...props }: {
    value: string; disabled?: boolean; onChange: (value: string) => void;
    options: { value: string; label: string }[]; "aria-label"?: string;
  }) => createElement("select", { value, disabled, "aria-label": props["aria-label"],
    onChange: (event: { target: { value: string } }) => onChange(event.target.value) },
  options.map((option) => createElement("option", { key: option.value, value: option.value }, option.label))),
  Table: ({ dataSource, columns }: {
    dataSource: AnalysisStudent[];
    columns: { key?: string; dataIndex?: keyof AnalysisStudent; render?: (value: unknown, row: AnalysisStudent) => ReactNode }[];
  }) => createElement("table", null, createElement("tbody", null, ...dataSource.map((row) =>
    createElement("tr", { key: row.id }, ...columns.map((column) =>
      createElement("td", { key: column.key || column.dataIndex }, column.render
        ? column.render(column.dataIndex ? row[column.dataIndex] : undefined, row)
        : String(row[column.dataIndex!] ?? ""))))))),
}));

let root: Root;
let container: HTMLDivElement;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  Object.defineProperty(Element.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
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

const report: BatchAnalysis = {
  schema_version: "batch-analysis-v1", snapshot_at: "2026-10-22T10:00:00Z", basis_hash: "a".repeat(64), batch_id: "batch-1",
  scope: { assignment_id: "assignment-1", assignment_title: "Synthetic assignment", classroom_id: "class-1", classroom_name: "合成班级",
    question_version_id: "question-1", question_key: "q1", question_version: 1, rubric_version_id: "rubric-1", rubric_version: 2,
    rubric_hash: "fixed", maximum_points: "10.00", selection_policy: "single-answer-current-confirmed-review-v1" },
  coverage: { assignment_roster: 3, batch_answers: 2, batch_students: 2, confirmed: 1, pending_review: 1,
    failed: 0, in_progress: 0, simulated_confirmed: 1, not_in_batch: 1, confirmed_rate: "50.00" },
  scores: { average_score: "0.00", minimum_score: "0.00", maximum_score: "0.00", reviewed_from: null, reviewed_to: null,
    distribution: [{ id: "below_60", students: 1 }, { id: "60_to_80", students: 0 }, { id: "80_to_100", students: 0 }, { id: "full", students: 0 }] },
  criteria: [{ id: "fixed_rule", description: "说明统一规则", maximum_points: "10.00", confirmed_students: 1,
    average_points: "0.00", score_rate: "0.00", below_full_credit_students: 1, below_full_credit_rate: "100.00",
    levels: [{ id: "missing", ordinal: 0, description: "未说明规则", points: "0.00", students: 1 },
      { id: "complete", ordinal: 1, description: "完整说明", points: "10.00", students: 0 }] }],
};
const student: AnalysisStudent = {
  id: "item-1", created_at: report.snapshot_at, student_id: "student-1", student_name: "合成学生甲", external_student_id: "s1",
  answer_version_id: "answer-1", source_revision: "v2", status: "succeeded", selection_status: "confirmed", review_id: "review-1",
  review_revision: 2, reviewed_at: report.snapshot_at, confirmed_score: "0.00", simulation: true,
};
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { "Content-Type": "application/json" },
});
const page = (items: AnalysisStudent[], basis_hash = report.basis_hash) => ({
  items, basis_hash, total: items.length, limit: 20, has_more: false, next_cursor: null,
});
const button = (label: string) => {
  const node = [...container.querySelectorAll("button")].find((item) => item.textContent === label);
  if (!node) throw new Error(`Missing button: ${label}`);
  return node;
};
async function render(onReview = vi.fn()) {
  await act(async () => root.render(createElement(AnalysisPanel, { api: new ApiClient("test"), batchId: "batch-1", onReview })));
  return onReview;
}

it("shows only confirmed statistics, explicit denominators and genuine zero scores", async () => {
  const fetch = vi.fn(async (path: string) => response(path.includes("/students") ? page([student]) : report));
  vi.stubGlobal("fetch", fetch);
  const onReview = await render();
  expect(container.textContent).toContain("确认均分0.00");
  expect(container.textContent).toContain("50.00%");
  expect(container.textContent).toContain("未选入本批次不等于未提交");
  expect(container.textContent).toContain("模拟初评");
  expect(container.textContent).toContain("答案 v2");
  await act(async () => button("查看原答案").click());
  expect(onReview).toHaveBeenCalledWith("item-1");
  const request = new URL(fetch.mock.calls.find(([path]) => path.includes("/students"))![0], window.location.origin);
  expect(request.searchParams.get("basis_hash")).toBe(report.basis_hash);
  expect(request.searchParams.get("status")).toBe("confirmed");
});

it("distinguishes an empty sample from a confirmed zero and disables empty drilldowns", async () => {
  const empty = structuredClone(report);
  empty.coverage.confirmed = 0;
  empty.coverage.simulated_confirmed = 0;
  empty.scores.average_score = empty.scores.minimum_score = empty.scores.maximum_score = null;
  empty.criteria[0] = { ...empty.criteria[0], average_points: null, score_rate: null, confirmed_students: 0, below_full_credit_students: 0 };
  vi.stubGlobal("fetch", vi.fn(async (path: string) => response(path.includes("/students") ? page([]) : empty)));
  await render();
  expect(container.textContent).toContain("确认均分—");
  expect(container.textContent).toContain("还没有老师确认的结果");
  expect(button("查看本项未得满分答案").disabled).toBe(true);
  expect(container.textContent).not.toContain("代表真实班级表现");
});

it("filters by the fixed criterion and displays the matching teacher reason", async () => {
  const fetch = vi.fn(async (path: string) => {
    const url = new URL(path, window.location.origin);
    return response(url.pathname.endsWith("/students") ? page([{ ...student,
      ...(url.searchParams.has("criterion_id") ? { level_id: "missing", review_reason: "老师确认：未解释统一评估。" } : {}),
    }]) : report);
  });
  vi.stubGlobal("fetch", fetch);
  await render();
  await act(async () => button("查看本项未得满分答案").click());
  expect(container.querySelector("table")?.textContent).toContain("老师确认：未解释统一评估。");
  const request = new URL(fetch.mock.calls.at(-1)![0], window.location.origin);
  expect(request.searchParams.get("criterion_id")).toBe("fixed_rule");
  expect(request.searchParams.get("below_full_credit")).toBe("true");
  expect((container.querySelector("select") as HTMLSelectElement).disabled).toBe(true);
  await act(async () => button("清除评分点筛选").click());
  expect(new URL(fetch.mock.calls.at(-1)![0], window.location.origin).searchParams.has("criterion_id")).toBe(false);
});

it("hides mismatched source rows and refreshes the report before retrying a stale drilldown", async () => {
  let changed = false;
  const updated = { ...report, basis_hash: "b".repeat(64) };
  const fetch = vi.fn(async (path: string) => {
    const url = new URL(path, window.location.origin);
    if (!url.pathname.endsWith("/students")) return response(changed ? updated : report);
    if (changed && url.searchParams.get("basis_hash") !== updated.basis_hash) return response({ code: "analysis_changed" }, 409);
    return response(page([student], changed ? updated.basis_hash : report.basis_hash));
  });
  vi.stubGlobal("fetch", fetch);
  await render();
  changed = true;
  await act(async () => button("查看本项未得满分答案").click());
  expect(container.textContent).toContain("成绩或任务状态已变化");
  expect(container.querySelector("table")?.textContent).not.toContain(student.student_name);
  await act(async () => button("刷新分析").click());
  expect(container.querySelector("table")?.textContent).toContain(student.student_name);
  expect(container.textContent).not.toContain("成绩或任务状态已变化");
  const request = new URL(fetch.mock.calls.at(-1)![0], window.location.origin);
  expect(request.searchParams.get("basis_hash")).toBe(updated.basis_hash);
});

it("reads failed and pending rows only when the teacher changes the analysis filter", async () => {
  const fetch = vi.fn(async (path: string) => response(path.includes("/students") ? page([]) : report));
  vi.stubGlobal("fetch", fetch);
  await render();
  await act(async () => {
    const select = container.querySelector("select")!;
    select.value = "failed";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(new URL(fetch.mock.calls.at(-1)![0], window.location.origin).searchParams.get("status")).toBe("failed");
  expect(container.textContent).toContain("确认均分0.00");
});
