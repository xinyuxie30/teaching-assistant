import { Alert, Button, Input, Select, Tag } from "antd";
import { useEffect } from "react";
import { ApiClient } from "../../api/client";
import { useOperation, useResource } from "../../api/hooks";
import type { AgentEventPage, AgentProfileList, AgentRun, QuestionDetail } from "../../api/types";
import { dateTime, ErrorNotice } from "../../components/common";
import {
  agentActive, agentBudget, agentErrorText, agentEventDetail, agentEventText,
  agentStatusColor, agentStatusText,
} from "./agent";
import { WriteFeedback } from "./common";
import { PreparationSession, usePreparationState } from "./session";

interface AgentState { runId?: string; profile?: string; instructions?: string }

const defaultInstructions = "请依据题干和参考答案，起草独立、可区分的评分点与等级。";

export function AgentPanel({ api, store, question, onOpenDraft }: {
  api: ApiClient; store: PreparationSession; question: QuestionDetail; onOpenDraft: (id: string) => void;
}) {
  const kept = usePreparationState<AgentState>(store, `agent:${question.id}`, () => ({}));
  const profiles = useResource<AgentProfileList>(api, "/v1/agent-profiles");
  const run = useResource<AgentRun>(
    api,
    kept.value.runId ? `/v1/agent-runs/${kept.value.runId}` : null,
    {},
    agentActive,
  );
  const events = useResource<AgentEventPage>(
    api,
    kept.value.runId ? `/v1/agent-runs/${kept.value.runId}/events` : null,
    { after: 0, limit: 100 },
    () => agentActive(run.data),
  );
  const sequence = run.data?.event_sequence;
  useEffect(() => {
    if (sequence !== undefined) events.refresh();
  }, [sequence, events.refresh]);

  const create = useOperation(api, `prepare:agent:${question.id}`);
  const cancel = useOperation(api, `prepare:agent-cancel:${kept.value.runId || "none"}`);
  const items = profiles.data?.items || [];
  const alias = kept.value.profile || profiles.data?.default_alias || "fake-rubric-agent-v1";
  const selected = items.find((item) => item.alias === alias) || items[0];
  const ready = Boolean(selected?.ready);
  const active = agentActive(run.data);

  const start = () => {
    if (!selected) return;
    const instructions = (kept.value.instructions || defaultInstructions).trim();
    void create.operation.run<AgentRun>(`/v1/question-versions/${question.id}/rubric-agent-runs`, {
      agent_profile: selected.alias,
      instructions,
    }).then((receipt) => {
      kept.update((value) => ({ ...value, runId: receipt.id }), false);
      run.refresh();
    }).catch(() => undefined);
  };

  const stop = () => {
    if (!run.data) return;
    void cancel.operation.run<AgentRun>(`/v1/agent-runs/${run.data.id}/cancel`, {
      reason: "teacher_cancelled",
    }).then(() => run.refresh()).catch(() => undefined);
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
    <div className="agent-form">
      <label className="prep-label" htmlFor={`agent-profile-${question.id}`}>起草配置</label>
      <Select id={`agent-profile-${question.id}`} value={selected?.alias} loading={profiles.loading}
        disabled={active || items.length === 0} onChange={(value) => kept.update((state) => ({ ...state, profile: value }), false)}
        options={items.map((item) => ({
          value: item.alias,
          disabled: !item.ready,
          label: `${item.alias === "fake-rubric-agent-v1" ? "模拟器" : "DeepSeek"} · ${item.manifest?.model || "未配置"}${item.ready ? "" : "（后端未配置）"}`,
        }))} />
      <label className="prep-label" htmlFor={`agent-instructions-${question.id}`}>给 Agent 的要求</label>
      <Input.TextArea id={`agent-instructions-${question.id}`} autoSize={{ minRows: 2, maxRows: 5 }} maxLength={4000}
        value={kept.value.instructions ?? defaultInstructions} disabled={active}
        onChange={(event) => kept.update((state) => ({ ...state, instructions: event.target.value }), false)} />
      {!ready && <Alert type="warning" showIcon title="所选配置尚未完成后端配置，暂时不能发起任务。" />}
      <div className="button-row">
        <Button type="primary" onClick={start} disabled={!ready || active}
          loading={create.phase === "sending"}>开始起草</Button>
        {active && <Button danger onClick={stop} loading={cancel.phase === "sending"}>取消任务</Button>}
        {run.data?.status === "waiting_teacher" && run.data.draft_id && <Button type="primary"
          onClick={() => onOpenDraft(run.data!.draft_id!)}>打开草稿并确认</Button>}
        <Button onClick={() => { run.refresh(); events.refresh(); }} disabled={!kept.value.runId}>刷新状态</Button>
      </div>
      <WriteFeedback state={create} retry={() => create.operation.retry()} />
      <WriteFeedback state={cancel} retry={() => cancel.operation.retry()} />
    </div>
    {run.data && run.data.question_version_id !== question.id && <div className="notice notice-error">
      这个任务属于另一道题目的版本，请刷新后重新发起。
    </div>}
    {run.data && <>
      <div className="agent-status">
        <span className="muted small">{agentBudget(run.data)}</span>
        <span className="muted small">第 {run.data.epoch} 次领取 · 恢复 {run.data.attempts} 次</span>
        <span className="muted small">创建于 {dateTime(run.data.created_at)}</span>
      </div>
      {run.data.error_code && <div className="notice notice-error" role="alert">{agentErrorText(run.data.error_code)}</div>}
      {run.data.simulation && <div className="notice warning">模拟器只走通工程流程，生成的草稿不能作为教学标准。</div>}
      {run.data.status === "waiting_teacher" && <div className="notice notice-success">
        草稿已保存。确认前它不会成为固定细则；确认后这个 Agent 任务会一同完成。
      </div>}
      {active && <p className="muted small">任务执行期限 300 秒，等待你审阅的时间不计入期限。</p>}
    </>}
    {events.data?.items.length ? <ol className="agent-events">
      {events.data.items.slice(-8).map((event) => <li key={event.sequence}>
        <span className="agent-event-kind">{agentEventText(event.kind)}</span>
        <span className="muted small">{agentEventDetail(event)}</span>
        <span className="agent-event-time muted small">{dateTime(event.created_at)}</span>
      </li>)}
    </ol> : null}
  </section>;
}
