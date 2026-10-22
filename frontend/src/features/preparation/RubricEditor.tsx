import { Button, Input, Tag } from "antd";
import { useState } from "react";
import { ApiClient } from "../../api/client";
import { useOperation } from "../../api/hooks";
import type { Criterion, DraftReceipt, QuestionDetail, Rubric } from "../../api/types";
import { Field, FormErrors, useAccepted, WriteFeedback } from "./common";
import { editableRubric, formErrors, keyFor, newCriterion, newRubric, pointCents, pointText, rubricPayload } from "./contracts";
import { PreparationSession, usePreparationState } from "./session";

export function RubricDocument({ body }: { body: Rubric }) {
  return <div className="rubric-document"><h3>整体评分说明</h3><p className="plain-text">{body.global_instructions}</p>
    {body.criteria.map((criterion, index) => <section key={criterion.id} className="rubric-point">
      <h3>{index + 1}. {criterion.description}<Tag>{criterion.maximum_points} 分</Tag></h3>
      <ol className="rubric-levels">{[...criterion.levels].sort((a, b) => a.ordinal - b.ordinal).map((level) =>
        <li key={level.id}><strong>{level.points} 分</strong><span className="plain-text">{level.description}</span></li>)}</ol>
    </section>)}
  </div>;
}

export function RubricEditor({ api, store, question, onSaved, source }: {
  api: ApiClient; store: PreparationSession; question: QuestionDetail;
  onSaved: (draftId: string) => void; source?: { id: string; body: Rubric };
}) {
  const scope = `${question.id}:${source?.id || "new"}`;
  const form = usePreparationState(store, `rubric-form:${scope}`, () => ({
    body: source ? editableRubric(source.body) : newRubric(question.maximum_points),
    receipt: undefined as DraftReceipt | undefined,
  }));
  const write = useOperation(api, `prepare:rubric:${scope}`);
  const [errors, setErrors] = useState<string[]>([]);
  useAccepted<DraftReceipt>(write, (receipt) => form.update((value) => ({ ...value, receipt }), false));
  const updateCriterion = (id: string, change: (criterion: Criterion) => Criterion) => form.update((value) => ({
    ...value, body: { ...value.body, criteria: value.body.criteria.map((criterion) => criterion.id === id ? change(criterion) : criterion) },
  }));
  const submit = async () => {
    let rubric: Rubric;
    try { rubric = rubricPayload(form.value.body, question.maximum_points, source?.body); setErrors([]); }
    catch (error) { setErrors(formErrors(error)); return; }
    try { await write.operation.run(`/v1/question-versions/${question.id}/rubric-drafts`, { rubric }); }
    catch { /* Persistent request error is displayed below. */ }
  };
  if (form.value.receipt) return <section className="panel prep-card">
    <h2>草稿已保存，尚未确认</h2><RubricDocument body={form.value.receipt.rubric} />
    <div className="button-row"><Button type="primary" onClick={() => onSaved(form.value.receipt!.id)}>查看并确认这份草稿</Button>
      <Button onClick={() => { write.operation.reset(); form.update((value) => ({ ...value, receipt: undefined }), false); }}>继续修改，另存新草稿</Button></div>
  </section>;
  const total = form.value.body.criteria.reduce((sum, criterion) => sum + (pointCents(criterion.levels.at(-1)?.points || "") ?? 0), 0);
  return <section className="panel prep-card"><div className="panel-toolbar prep-toolbar"><div>
    <h2>{source ? "基于已有规则起草新版本" : "起草分项评分细则"}</h2>
    <p className="muted small">每个评分点独立判断；等级按由低到高排列，描述应明确且能区分。</p>
  </div><Tag color={total === pointCents(question.maximum_points) ? "green" : "orange"}>
    满分合计 {pointText(total)} / {question.maximum_points}
  </Tag></div>
    <Field label="整体评分说明"><Input.TextArea aria-label="整体评分说明" maxLength={10000} autoSize={{ minRows: 3, maxRows: 10 }}
      disabled={write.locked} value={form.value.body.global_instructions} placeholder="说明适用范围、允许的表述差异，以及需要特别注意的条件。"
      onChange={(event) => form.update((v) => ({ ...v, body: { ...v.body, global_instructions: event.target.value } }))} /></Field>
    {form.value.body.criteria.map((criterion, index) => <section className="rubric-editor-point" key={criterion.id}>
      <div className="panel-toolbar prep-toolbar"><h3>评分点 {index + 1}</h3>
        <Button type="text" danger disabled={write.locked || form.value.body.criteria.length === 1}
          onClick={() => form.update((v) => ({ ...v, body: { ...v.body, criteria: v.body.criteria.filter((row) => row.id !== criterion.id) } }))}>移除评分点</Button></div>
      <Field label="该评分点评估什么"><Input.TextArea aria-label={`评分点 ${index + 1} 评估内容`} maxLength={10000} autoSize={{ minRows: 2, maxRows: 5 }}
        value={criterion.description} disabled={write.locked} onChange={(event) => updateCriterion(criterion.id, (c) => ({ ...c, description: event.target.value }))} /></Field>
      {criterion.levels.map((level, levelIndex) => <div className="level-editor" key={level.id}>
        <span className="level-number">等级 {levelIndex + 1}</span>
        <Input aria-label={`评分点 ${index + 1} 等级 ${levelIndex + 1} 分数`} inputMode="decimal" maxLength={8}
          value={level.points} disabled={write.locked} suffix="分"
          onChange={(event) => updateCriterion(criterion.id, (c) => ({ ...c, levels: c.levels.map((l) => l.id === level.id ? { ...l, points: event.target.value } : l) }))} />
        <div className="level-description"><Input.TextArea aria-label={`评分点 ${index + 1} 等级 ${levelIndex + 1} 描述`} maxLength={10000} autoSize={{ minRows: 2, maxRows: 6 }}
          placeholder="描述答案应满足的具体条件" value={level.description} disabled={write.locked}
          onChange={(event) => updateCriterion(criterion.id, (c) => ({ ...c, levels: c.levels.map((l) => l.id === level.id ? { ...l, description: event.target.value } : l) }))} /></div>
        <Button type="text" disabled={write.locked || criterion.levels.length <= 2} aria-label={`移除评分点 ${index + 1} 等级 ${levelIndex + 1}`}
          onClick={() => updateCriterion(criterion.id, (c) => ({ ...c, levels: c.levels.filter((l) => l.id !== level.id) }))}>移除</Button>
      </div>)}
      <div className="prep-actions"><Button disabled={write.locked || criterion.levels.length >= 10}
        onClick={() => updateCriterion(criterion.id, (c) => ({ ...c, levels: [
          ...c.levels.slice(0, -1), { id: keyFor("level"), ordinal: c.levels.length - 1, description: "", points: "0.00" }, c.levels[c.levels.length - 1],
        ] }))}>增加中间等级</Button><span className="muted small">最高等级分值作为本评分点满分</span></div>
    </section>)}
    <div className="prep-actions"><Button disabled={write.locked || form.value.body.criteria.length >= 30}
      onClick={() => form.update((v) => ({ ...v, body: { ...v.body, criteria: [...v.body.criteria, newCriterion()] } }))}>添加评分点</Button>
      <Button type="primary" disabled={write.locked} loading={write.phase === "sending"} onClick={() => void submit()}>保存草稿，准备确认</Button></div>
    <FormErrors errors={errors} /><WriteFeedback state={write} retry={() => write.operation.retry()} />
  </section>;
}
