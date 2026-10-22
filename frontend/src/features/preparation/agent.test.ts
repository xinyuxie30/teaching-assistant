import { describe, expect, it } from "vitest";
import type { AgentRun } from "../../api/types";
import {
  agentActive, agentBudget, agentErrorText, agentEventDetail, agentEventText, agentPaused,
  agentStatusColor, agentStatusText, preferredRun,
} from "./agent";

const run = (overrides: Partial<AgentRun> = {}): AgentRun => ({
  id: "run-1",
  question_version_id: "question-1",
  status: "running",
  error_code: null,
  draft_id: null,
  waiting_for: null,
  profile_id: "agent-1",
  manifest: { provider: "fake", model: "fake-rubric-agent-v1" },
  simulation: true,
  limits: { max_model_calls: 6, max_tool_calls: 10, max_context_characters: 40000, max_seconds: 300 },
  models_used: 2,
  tools_used: 1,
  attempts: 1,
  epoch: 1,
  event_sequence: 6,
  expires_at: "2026-10-22T03:00:00Z",
  created_at: "2026-10-22T02:55:00Z",
  ...overrides,
});

describe("agent status", () => {
  it("translates every known status and keeps an unknown one visible", () => {
    expect(agentStatusText("waiting_teacher")).toBe("等待老师确认");
    expect(agentStatusText("waiting_children")).toBe("等待子任务");
    expect(agentStatusText("waiting_input")).toBe("等待补充信息");
    expect(agentStatusText("waiting_timer")).toBe("等待定时恢复");
    expect(agentStatusText("cancelled" as AgentRun["status"])).toBe("已取消");
    expect(agentStatusText("mystery" as AgentRun["status"])).toBe("mystery");
    expect(agentStatusColor("failed")).toBe("red");
    expect(agentStatusColor("mystery" as AgentRun["status"])).toBe("default");
  });

  it("keeps running and paused work cancellable while excluding review and terminal states", () => {
    expect(agentActive(run({ status: "queued" }))).toBe(true);
    expect(agentActive(run({ status: "running" }))).toBe(true);
    expect(agentActive(run({ status: "waiting_children" }))).toBe(true);
    expect(agentActive(run({ status: "waiting_input" }))).toBe(true);
    expect(agentActive(run({ status: "waiting_timer" }))).toBe(true);
    expect(agentPaused(run({ status: "waiting_children" }))).toBe(false);
    expect(agentPaused(run({ status: "waiting_input" }))).toBe(true);
    expect(agentPaused(run({ status: "waiting_timer" }))).toBe(true);
    expect(agentActive(run({ status: "waiting_teacher" }))).toBe(false);
    expect(agentActive(run({ status: "completed" }))).toBe(false);
    expect(agentActive(run({ status: "failed" }))).toBe(false);
    expect(agentActive(run({ status: "cancelled" }))).toBe(false);
    expect(agentActive(null)).toBe(false);
  });

  it("summarizes the budget actually consumed", () => {
    expect(agentBudget(run({ models_used: 3, tools_used: 4 }))).toBe("模型 3/6 · 工具 4/10");
  });

  it("prefers an actionable task over a newer dead one", () => {
    const waiting = run({ id: "waiting", status: "waiting_teacher" });
    const running = run({ id: "running", status: "running" });
    const failed = run({ id: "failed", status: "failed" });
    expect(preferredRun([failed, waiting, running])?.id).toBe("waiting");
    expect(preferredRun([failed, running])?.id).toBe("running");
    expect(preferredRun([failed, run({ id: "created", status: "completed" })])?.id).toBe("failed");
    expect(preferredRun([])).toBeUndefined();
  });
});

describe("agent errors", () => {
  it("explains known codes and keeps unknown ones traceable", () => {
    expect(agentErrorText("agent_missing_draft")).toContain("没有保存草稿");
    expect(agentErrorText("agent_model_budget_exhausted")).toContain("预算");
    expect(agentErrorText("provider_invalid_response")).toContain("结构");
    expect(agentErrorText("something_new")).toContain("something_new");
    expect(agentErrorText(null)).toBeNull();
  });
});

describe("agent events", () => {
  it("names known events and falls back to the raw kind", () => {
    expect(agentEventText("post_tool")).toBe("工具已返回");
    expect(agentEventText("custom_event")).toBe("custom_event");
  });

  it("extracts the useful detail without exposing arguments", () => {
    expect(agentEventDetail({
      sequence: 1, epoch: 1, kind: "pre_tool", created_at: "2026-10-22T02:56:00Z",
      body: { name: "read_question" },
    })).toBe("read_question");
    expect(agentEventDetail({
      sequence: 2, epoch: 1, kind: "post_tool", created_at: "2026-10-22T02:56:00Z",
      body: { name: "propose_rubric", output: { ok: false, error_code: "rubric_total_mismatch" } },
    })).toBe("propose_rubric · 任务未完成（rubric_total_mismatch）。");
    expect(agentEventDetail({
      sequence: 3, epoch: 1, kind: "stop_hook", created_at: "2026-10-22T02:56:00Z",
      body: { accepted: false },
    })).toBe("未通过");
    expect(agentEventDetail({
      sequence: 4, epoch: 2, kind: "claimed", created_at: "2026-10-22T02:56:00Z",
      body: { attempt: 2 },
    })).toBe("第 2 次");
    expect(agentEventDetail({
      sequence: 5, epoch: 1, kind: "created", created_at: "2026-10-22T02:56:00Z", body: {},
    })).toBe("");
  });
});
