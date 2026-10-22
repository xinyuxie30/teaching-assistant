export class ApiError extends Error {
  constructor(public code: string, public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
  get uncertain() {
    return this.status === 0 || this.status >= 500 || this.code === "unreadable_response";
  }
}

export function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "操作未完成，请稍后重试。";
  const messages: Record<string, string> = {
    unauthorized: "身份已失效，请重新连接。",
    not_found: "记录不存在，或当前身份无权访问。",
    invalid_cursor: "列表条件已变化，请返回第一页。",
    revision_conflict: "另一处已更新这份复核。你的编辑仍保留，请先查看最新版本。",
    not_ready_for_review: "这份答案尚未完成初评，暂时不能确认。",
    not_retryable: "该任务的状态已变化，请刷新后查看。",
    idempotency_conflict: "提交标识与内容不一致，请重新载入操作。",
    pending_operation: "上一次提交结果尚未确定，请先重试同一次提交。",
    network_error: "连接中断或请求超时。写入可能已经完成，请重试同一次提交。",
    unreadable_response: "服务器响应无法读取。请重试同一次提交，以查询原操作结果。",
    missing_review_evidence: "选择“原文支持”时需要添加引用。",
    invalid_evidence: "引用与原答案不一致，请重新选择原文片段。",
    validation_error: "提交内容不完整或格式不正确，请检查填写的字段。",
    rubric_total_mismatch: "评分点满分之和必须等于题目满分。",
    submission_revision_conflict: "同一来源和提交版本已有不同内容。请核对原文，或使用新的提交版本。",
    invalid_import_scope: "答案中的学生或题目不属于这份作业，请重新检查固定名单和题目版本。",
    unknown_evaluation_profile: "所选评估配置已不可用，请刷新配置并重新选择。",
    model_not_configured: "该模型尚未完成后端配置，请选择可用配置。",
    agent_not_cancellable: "该任务已在等待确认或已结束，不能取消。",
    agent_model_budget_exhausted: "模型调用预算已用完，草稿没有生成。",
    agent_tool_budget_exhausted: "工具调用预算已用完，草稿没有生成。",
    agent_missing_draft: "模型声称完成但没有保存草稿，任务已停止。",
    agent_deadline_exceeded: "任务超过执行期限，已停止。",
    agent_profile_mismatch: "运行配置与后端 Worker 不一致，任务未被执行。",
  };
  return messages[error.code] || `请求未完成（${error.code || error.status}）。`;
}

export type Query = Record<string, string | number | undefined | null>;
export function apiUrl(path: string, query: Query = {}): string {
  if (!path.startsWith("/v1/")) throw new Error("Only same-origin /v1/ requests are allowed");
  const params = new URLSearchParams();
  for (const key of Object.keys(query).sort()) {
    const value = query[key];
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  return path + (params.size ? `?${params}` : "");
}

interface Ticket { path: string; body: string; key: string }
export interface OperationState {
  phase: "idle" | "sending" | "uncertain" | "failed" | "succeeded";
  ticket?: Ticket;
  result?: unknown;
  error?: ApiError;
}

/** An operation survives page changes within this authenticated in-memory session. */
export class WriteOperation {
  private state: OperationState = { phase: "idle" };
  private listeners = new Set<() => void>();
  constructor(private api: ApiClient) {}
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  private publish(state: OperationState) {
    this.state = state;
    this.listeners.forEach((listener) => listener());
  }
  async run<T>(path: string, payload: unknown): Promise<T> {
    const body = JSON.stringify(payload);
    if (typeof body !== "string") throw new ApiError("validation_error", 422, "JSON body required");
    const old = this.state;
    if (old.phase === "sending") throw new ApiError("pending_operation", 409, "Request in flight");
    const matches = old.ticket?.path === path && old.ticket.body === body;
    if (old.phase === "uncertain" && !matches) {
      throw new ApiError("pending_operation", 409, "Resolve the previous request first");
    }
    // A new click after success is a new intention (e.g. a new export snapshot).
    // A retry after an error retains both the exact bytes and the original key.
    const ticket = matches && (old.phase === "uncertain" || old.phase === "failed")
      ? old.ticket!
      : { path, body, key: crypto.randomUUID() };
    this.publish({ phase: "sending", ticket });
    try {
      const result = await this.api.postJson<T>(ticket.path, ticket.body, ticket.key);
      this.publish({ phase: "succeeded", ticket, result });
      return result;
    } catch (error) {
      const failure = error instanceof ApiError
        ? error : new ApiError("network_error", 0, "Request outcome unknown");
      this.publish({ phase: failure.uncertain ? "uncertain" : "failed", ticket, error: failure });
      throw failure;
    }
  }
  retry<T>() {
    if (!this.state.ticket) throw new Error("No operation to retry");
    return this.run<T>(this.state.ticket.path, JSON.parse(this.state.ticket.body));
  }
  reset() {
    if (["sending", "uncertain"].includes(this.state.phase)) {
      throw new ApiError("pending_operation", 409, "Resolve the previous request first");
    }
    this.publish({ phase: "idle" });
  }
}

export class ApiClient {
  private operations = new Map<string, WriteOperation>();
  constructor(private token: string) {}
  operation(name: string) {
    if (!this.operations.has(name)) this.operations.set(name, new WriteOperation(this));
    return this.operations.get(name)!;
  }
  hasUnresolvedWrites() {
    return [...this.operations.values()].some((op) =>
      ["sending", "uncertain"].includes(op.snapshot().phase));
  }
  private async request<T>(
    path: string, options: RequestInit, read: (response: Response) => Promise<T>,
  ): Promise<T> {
    const url = new URL(path, window.location.origin);
    if (!path.startsWith("/v1/") || url.origin !== window.location.origin
      || !url.pathname.startsWith("/v1/")) {
      throw new Error("Only same-origin /v1/ requests are allowed");
    }
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (options.signal?.aborted) controller.abort();
    options.signal?.addEventListener("abort", abort, { once: true });
    const timeout = setTimeout(abort, 30_000);
    try {
      const response = await fetch(path, {
        ...options,
        signal: controller.signal,
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        headers: {
          Authorization: `Bearer ${this.token}`,
          ...(options.body ? { "Content-Type": "application/json" } : {}),
          ...options.headers,
        },
      });
      if (!response.ok) {
        let code = response.status === 401 ? "unauthorized" : `http_${response.status}`;
        try {
          const body = await response.json();
          code = typeof body.code === "string" ? body.code
            : response.status === 422 ? "validation_error" : code;
        } catch { /* Do not expose proxy error pages, credentials or raw request content. */ }
        throw new ApiError(code, response.status, code);
      }
      return await read(response);
    } catch (error) {
      if (options.signal?.aborted) throw new DOMException("Request aborted", "AbortError");
      if (error instanceof ApiError) throw error;
      throw new ApiError("network_error", 0, "Request outcome unknown");
    } finally {
      clearTimeout(timeout);
      options.signal?.removeEventListener("abort", abort);
    }
  }
  private async json<T>(path: string, options: RequestInit): Promise<T> {
    return this.request(path, options, async (response) => {
      try { return await response.json() as T; }
      catch { throw new ApiError("unreadable_response", response.status, "Unreadable JSON"); }
    });
  }
  get<T>(path: string, signal?: AbortSignal) {
    return this.json<T>(path, { signal });
  }
  postJson<T>(path: string, body: string, key: string) {
    apiUrl(path); // Reject absolute or off-origin URLs before attaching credentials.
    return this.json<T>(path, { method: "POST", body, headers: { "Idempotency-Key": key } });
  }
  previewImport<T>(assignmentId: string, payload: unknown, signal?: AbortSignal) {
    return this.json<T>(`/v1/assignments/${encodeURIComponent(assignmentId)}/imports/preview`, {
      method: "POST", body: JSON.stringify(payload), signal,
    });
  }
  async download(id: string, format: "json" | "csv") {
    const blob = await this.request(
      apiUrl(`/v1/exports/${encodeURIComponent(id)}`, { format }), {}, (response) => response.blob(),
    );
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `grades-${id}.${format}`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }
}
