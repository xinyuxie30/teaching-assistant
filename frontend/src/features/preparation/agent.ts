import type { AgentEvent, AgentRun, AgentStatus } from "../../api/types";

const statusText: Record<AgentStatus, string> = {
  queued: "排队中",
  running: "起草中",
  waiting_teacher: "等待老师确认",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const statusColor: Record<AgentStatus, string> = {
  queued: "default",
  running: "processing",
  waiting_teacher: "orange",
  completed: "green",
  failed: "red",
  cancelled: "default",
};

const errors: Record<string, string> = {
  agent_model_budget_exhausted: "模型调用预算已用完，草稿没有生成。可调高预算后重新发起。",
  agent_tool_budget_exhausted: "工具调用预算已用完，草稿没有生成。可调高预算后重新发起。",
  agent_context_budget_exceeded: "题目、材料或工具历史超出上下文预算，后续模型请求已停止。此前调用仍计入用量。",
  agent_missing_draft: "模型声称完成，但没有保存草稿。任务已停止，请重试或手动起草。",
  agent_deadline_exceeded: "超过执行期限，任务已停止。等待老师审阅的时间不计入期限。",
  agent_attempts_exhausted: "自动恢复次数已用完，请重新发起任务。",
  agent_profile_mismatch: "运行配置与后端 Worker 不一致，任务未被执行。请检查模型配置。",
  agent_stop_blocked: "任务未通过完成检查，请查看已保存草稿及事件后再决定如何处理。",
  agent_not_cancellable: "任务已在等待确认或已结束，不能取消。",
  provider_auth_error: "模型密钥无效或权限不足，请检查后端配置。",
  provider_model_version_mismatch: "供应商返回的模型与配置不一致，本次调用未被接受。",
  provider_invalid_response: "模型返回的结构不符合契约，请重试或更换模型。",
  provider_incomplete_response: "模型返回不完整，请重试。",
  provider_timeout: "模型调用超时。外部结果未知，额度已记录。",
  provider_transport_error: "与模型的连接中断。外部结果未知，额度已记录。",
  model_timeout: "模型调用超时。外部结果未知，额度已记录。",
  model_not_configured: "该模型尚未完成后端配置，请选择可用配置。",
  tool_timeout: "工具执行超时，本次工具写入已回滚。",
};

const events: Record<string, string> = {
  created: "任务已创建",
  claimed: "Worker 已领取",
  model_dispatched: "已请求模型",
  model_succeeded: "模型已返回",
  model_failed: "模型调用失败",
  model_unknown: "调用结果未知",
  tools_requested: "模型请求工具",
  pre_tool: "执行工具前检查",
  post_tool: "工具已返回",
  stop_hook: "完成检查",
  retry_wait: "等待重试",
  tool_skipped: "跳过工具调用",
  waiting_teacher: "等待老师确认",
  teacher_confirmed: "老师已确认细则",
  completed: "任务完成",
  failed: "任务失败",
  cancelled: "任务已取消",
};

export function agentStatusText(status: AgentStatus): string {
  return statusText[status] ?? status;
}

export function agentStatusColor(status: AgentStatus): string {
  return statusColor[status] ?? "default";
}

export function agentActive(run?: Pick<AgentRun, "status"> | null): boolean {
  return run?.status === "queued" || run?.status === "running";
}

export function agentBudget(run: AgentRun): string {
  return `模型 ${run.models_used}/${run.limits.max_model_calls} · 工具 ${run.tools_used}/${run.limits.max_tool_calls}`;
}

export function agentErrorText(code?: string | null): string | null {
  if (!code) return null;
  return errors[code] ?? `任务未完成（${code}）。`;
}

export function agentEventText(kind: string): string {
  return events[kind] ?? kind;
}

export function agentEventDetail(event: AgentEvent): string {
  const body = event.body || {};
  const output = body.output && typeof body.output === "object"
    ? body.output as Record<string, unknown> : undefined;
  const parts: string[] = [];
  if (typeof body.name === "string") parts.push(body.name);
  const code = typeof body.error_code === "string" ? body.error_code
    : output && typeof output.error_code === "string" ? output.error_code : null;
  if (code) parts.push(agentErrorText(code) ?? code);
  if (typeof body.accepted === "boolean") parts.push(body.accepted ? "通过" : "未通过");
  if (typeof body.blocked === "string" && body.blocked) parts.push("已阻止");
  if (typeof body.attempt === "number") parts.push(`第 ${body.attempt} 次`);
  return parts.join(" · ");
}
