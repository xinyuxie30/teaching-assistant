import { Alert, Button, Input, Select, Tag } from "antd";
import { useEffect } from "react";
import { ApiClient } from "../../api/client";
import { useOperation, usePage, useResource } from "../../api/hooks";
import type { AgentEventPage, AgentProfileList, AgentRun, QuestionDetail } from "../../api/types";
import { dateTime, ErrorNotice } from "../../components/common";
import {
  agentActive, agentBudget, agentErrorText, agentEventDetail, agentEventText,
  agentStatusColor, agentStatusText, preferredRun,
} from "./agent";
import { useAccepted, WriteFeedback } from "./common";
import { PreparationSession, usePreparationState } from "./session";

interface AgentState { runId?: string; profile?: string; instructions?: string; acceptedCreation?: string }

const defaultInstructions = "请依据题干和参考答案，起草独立、可区分的评分点与等级。";

export function AgentPanel({ api, store, question, onOpenDraft }: {
  api: ApiClient; store: PreparationSession; question: QuestionDetail; onOpenDraft: (id: string) => void;
}) {
  const kept = usePreparationState<AgentState>(store, `agent:${question.id}`, () => ({}));
  const profiles = useResource<AgentProfileList>(api, "/v1/agent-profiles");
  const history = usePage<AgentRun>(api, "/v1/agent-runs", { question_version_id: question.id });
  const currentRunId = kept.value.runId;
  useEffect(() => {
    if (currentRunId || !history.data?.items.length) return;
    const preferred = preferredRun(history.data.items);
    if (!preferred) return;
    const key = `agent:${question.id}`;
    // Read the store again: a creation may have selected a run after this effect was scheduled.
    const latest = store.get<AgentState>(key, () => ({}));
    if (!latest.value.runId) store.set(key, { ...latest.value, runId: preferred.id }, false);
  }, [currentRunId, history.data, store, question.id]);
  const run = useResource<AgentRun>(
    api,
    kept.value.runId ? `/v1/agent-runs/${kept.value.runId}` : null,
    {},
    agentActive,
  );
  const events = useResource<AgentEventPage>(
    api,
    kept.value.runId ? `/v1/agent-runs/${kept.value.runId}/events` : null,
    { after: Math.max(0, (run.data?.event_sequence || 0) - 8), limit: 8 },
    () => agentActive(run.data),
  );

  const create = useOperation(api, `prepare:agent:${question.id}`);
  const cancel = useOperation(api, `prepare:agent-cancel:${kept.value.runId || "none"}`);
  const feedback = usePreparationState<string>(store, `agent-feedback:${question.id}:${kept.value.runId || "none"}`, () => "");
  useAccepted<AgentRun>(create, (receipt) => {
    const key = create.ticket?.key;
    if (!key || receipt.question_version_id !== question.id || kept.value.acceptedCreation === key) return;
    kept.update((value) => ({ ...value, runId: receipt.id, acceptedCreation: key }), false);
    history.first();
    run.refresh();
  });
  useAccepted<AgentRun>(cancel, () => { run.refresh(); history.refresh(); });
  const items = profiles.data?.items || [];
  const alias = kept.value.profile || profiles.data?.default_alias || "fake-rubric-agent-v1";
  const selected = items.find((item) => item.alias === alias) || items[0];
  const ready = Boolean(selected?.ready);
  const active = agentActive(run.data);
  const writing = create.locked || cancel.locked;
  const unknownRun = Boolean(kept.value.runId && (!run.data || run.error));
  const canRevise = Boolean(run.data?.draft_id
    && (run.data.status === "waiting_teacher" || run.data.status === "completed"));
  // A failed history read is shown with its own retry; it must not block a fresh draft.
  const cannotCreate = !ready || active || writing || unknownRun;

  const start = () => {
    if (!selected) return;
    const instructions = (kept.value.instructions || defaultInstructions).trim();
    void create.operation.run<AgentRun>(`/v1/question-versions/${question.id}/rubric-agent-runs`, {
      agent_profile: selected.alias,
      instructions,
    }).catch(() => undefined);
  };

  const revise = () => {
    if (!selected || !run.data?.draft_id || !feedback.value.trim() || cannotCreate) return;
    void create.operation.run<AgentRun>(`/v1/question-versions/${question.id}/rubric-agent-runs`, {
      agent_profile: selected.alias,
      instructions: feedback.value.trim(),
      source_draft_id: run.data.draft_id,
    }).catch(() => undefined);
  };

  const stop = () => {
    if (!run.data) return;
    void cancel.operation.run<AgentRun>(`/v1/agent-runs/${run.data.id}/cancel`, {
      reason: "teacher_cancelled",
    }).catch(() => undefined);
  };

  if (profiles.error) {
    return <section className="panel prep-card"><ErrorNotice error={profiles.error} retry={profiles.refresh} /></section>;
  }

  return <section className="panel prep-card agent-panel">
    <div className="panel-toolbar prep-toolbar">
      <div><h2>用 Agent 起草评分细则</h2>
        <p className="muted small">Agent 只读取这道固定题目并保存未确认草稿，不能确认细则或成绩。确认仍由你在草稿页显式完成。</p></div>
      {run.data && <Tag color={agentStatusColor(run.data.status)}>{agentStatusText(run.data.status)}</Tag>}
    </div>
    <div className="agent-history">
      <label className="prep-label" htmlFor={`agent-history-${question.id}`}>本题历史任务</label>
      <Select id={`agent-history-${question.id}`} style={{ width: "100%" }}
        placeholder="暂无历史任务" value={kept.value.runId} loading={history.loading}
        disabled={writing || !history.data?.items.length}
        onChange={(id) => kept.update((value) => ({ ...value, runId: id }), false)}
        options={(history.data?.items || []).map((item) => ({ value: item.id,
          label: `${dateTime(item.created_at)} · ${agentStatusText(item.id === run.data?.id ? run.data.status : item.status)} · ${item.id.slice(0, 8)}`,
        }))} />
      <div className="button-row">
        <Button disabled={writing || history.loading || history.pageNumber <= 1} onClick={history.previous}>上一页任务</Button>
        <span className="muted small">第 {history.pageNumber} 页 · 共 {history.data?.total ?? 0} 项</span>
        <Button disabled={writing || history.loading || !history.data?.has_more} onClick={history.next}>下一页任务</Button>
        <Button disabled={writing} onClick={history.first}>刷新任务列表</Button>
      </div>
      <ErrorNotice error={history.error} retry={history.refresh} />
    </div>
    <ErrorNotice error={run.error} retry={run.refresh} />
    <div className="agent-form">
      <label className="prep-label" htmlFor={`agent-profile-${question.id}`}>起草配置</label>
      <Select id={`agent-profile-${question.id}`} value={selected?.alias} loading={profiles.loading}
        disabled={active || writing || items.length === 0} onChange={(value) => kept.update((state) => ({ ...state, profile: value }), false)}
        options={items.map((item) => ({
          value: item.alias,
          disabled: !item.ready,
          label: `${item.alias === "fake-rubric-agent-v1" ? "模拟器" : "DeepSeek"} · ${item.manifest?.model || "未配置"}${item.ready ? "" : "（后端未配置）"}`,
        }))} />
      <label className="prep-label" htmlFor={`agent-instructions-${question.id}`}>给 Agent 的要求</label>
      <Input.TextArea id={`agent-instructions-${question.id}`} autoSize={{ minRows: 2, maxRows: 5 }} maxLength={4000}
        value={kept.value.instructions ?? defaultInstructions} disabled={active || writing}
        onChange={(event) => kept.update((state) => ({ ...state, instructions: event.target.value }), false)} />
      {!ready && <Alert type="warning" showIcon title="所选配置尚未完成后端配置，暂时不能发起任务。" />}
      <div className="button-row">
        <Button type="primary" onClick={start} disabled={cannotCreate || !(kept.value.instructions ?? defaultInstructions).trim()}
          loading={create.phase === "sending"}>开始起草</Button>
        {active && <Button danger onClick={stop} disabled={writing} loading={cancel.phase === "sending"}>取消任务</Button>}
        {run.data?.status === "waiting_teacher" && run.data.draft_id && <Button type="primary"
          disabled={writing || unknownRun}
          onClick={() => onOpenDraft(run.data!.draft_id!)}>打开草稿并确认</Button>}
        <Button onClick={() => { run.refresh(); events.refresh(); }} disabled={!kept.value.runId}>刷新状态</Button>
      </div>
      <WriteFeedback state={create} retry={() => create.operation.retry()} />
      <WriteFeedback state={cancel} retry={() => cancel.operation.retry()} />
    </div>
    {canRevise && <div className="agent-form agent-revision">
      <label className="prep-label" htmlFor={`agent-feedback-${question.id}`}>对当前草稿的修改意见</label>
      <p className="muted small">基于当前任务的草稿生成一份新版，原稿和已确认版本保留。每次修改单独计入调用预算，新版仍需你确认。</p>
      <Input.TextArea id={`agent-feedback-${question.id}`} autoSize={{ minRows: 2, maxRows: 6 }} maxLength={4000}
        placeholder="例如：把第二个评分点拆成两个独立评分点，并明确部分得分的条件。"
        value={feedback.value} disabled={writing || unknownRun}
        onChange={(event) => feedback.set(event.target.value, false)} />
      <div className="button-row">
        <Button type="primary" disabled={cannotCreate || !feedback.value.trim()}
          loading={create.phase === "sending"} onClick={revise}>按修改意见生成新版</Button>
        <Button disabled={writing || unknownRun} onClick={() => onOpenDraft(run.data!.draft_id!)}>查看本次草稿</Button>
      </div>
    </div>}
    {run.data && run.data.question_version_id !== question.id && <div className="notice notice-error">
      这个任务属于另一道题目的版本，请刷新后重新发起。
    </div>}
    {run.data && <>
      {run.data.source_draft && <div className="agent-revision-source">
        <p>本次以草稿 {run.data.source_draft.id.slice(0, 8)} 为基础生成新版。</p>
        {run.data.revision_feedback && <p className="agent-revision-feedback">修改意见：{run.data.revision_feedback}</p>}
        <Button disabled={writing} onClick={() => onOpenDraft(run.data!.source_draft!.id)}>查看来源草稿</Button>
      </div>}
      <div className="agent-status">
        <span className="muted small">{agentBudget(run.data)}</span>
        <span className="muted small">第 {run.data.epoch} 次领取 · 重领 {Math.max(0, run.data.attempts - 1)} 次</span>
        <span className="muted small">创建于 {dateTime(run.data.created_at)}</span>
      </div>
      {run.data.error_code && <div className="notice notice-error" role="alert">{agentErrorText(run.data.error_code)}</div>}
      {run.data.simulation && <div className="notice warning">模拟器只走通工程流程，生成的草稿不能作为教学标准。</div>}
      {run.data.status === "waiting_teacher" && <div className="notice notice-success">
        草稿已保存。确认前它不会成为固定细则；确认后这个 Agent 任务会一同完成。
      </div>}
      {active && <p className="muted small">任务执行期限 300 秒，等待你审阅的时间不计入期限。</p>}
    </>}
    <ErrorNotice error={events.error} retry={events.refresh} />
    {events.data?.items.length ? <ol className="agent-events" aria-label="最近任务事件">
      {events.data.items.slice(-8).map((event) => <li key={event.sequence}>
        <span className="agent-event-kind">{agentEventText(event.kind)}</span>
        <span className="muted small">{agentEventDetail(event)}</span>
        <span className="agent-event-time muted small">{dateTime(event.created_at)}</span>
      </li>)}
    </ol> : null}
  </section>;
}
