import { Button, Collapse, Input, Segmented, Select, Table } from "antd";
import { useEffect, useRef, useState } from "react";
import { ApiClient } from "../../api/client";
import { useOperation } from "../../api/hooks";
import type { AssignmentDetail, ImportPayload, ImportPreview, ImportReceipt, QuestionDetail } from "../../api/types";
import { ErrorNotice } from "../../components/common";
import { Field, FormErrors, useAccepted, WriteFeedback } from "./common";
import { answerIdentity, charCount, emptyAnswer, formErrors, importForCommit, manualImport, pastedImport, submissionId } from "./contracts";
import type { AnswerEntry, PreviewGate } from "./contracts";
import { PreparationSession, usePreparationState } from "./session";

interface ImportForm {
  mode: "manual" | "json"; rows: AnswerEntry[]; json: string; preview?: PreviewGate; receipt?: ImportReceipt;
}
const initialImport = (): ImportForm => ({ mode: "manual", rows: [emptyAnswer()], json: "" });

export function AnswerImport({ api, store, assignment, question, onBatch }: {
  api: ApiClient; store: PreparationSession; assignment: AssignmentDetail; question: QuestionDetail; onBatch: () => void;
}) {
  const scope = `import-form:${question.id}`;
  const form = usePreparationState(store, scope, initialImport);
  const write = useOperation(api, `prepare:import:${question.id}`);
  const [errors, setErrors] = useState<string[]>([]);
  const [previewError, setPreviewError] = useState<unknown>();
  const [previewing, setPreviewing] = useState(false);
  const request = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => request.current?.abort(), []);
  useAccepted<ImportReceipt>(write, (receipt) => form.update((value) => ({ ...value, receipt }), false));
  const payloadFor = (value: ImportForm): ImportPayload => value.mode === "manual"
    ? manualImport(value.rows, assignment.roster, question.id)
    : pastedImport(value.json, assignment.roster, question.id);
  const edit = (change: (value: ImportForm) => ImportForm) => {
    form.update((value) => ({ ...change(value), preview: undefined }));
    setErrors([]); setPreviewError(undefined);
  };
  const editRow = (key: string, change: Partial<AnswerEntry>) => edit((value) => ({
    ...value, rows: value.rows.map((row) => row.key === key ? { ...row, ...change } : row),
  }));
  const preview = async () => {
    let payload: ImportPayload;
    try { payload = payloadFor(form.value); setErrors([]); }
    catch (error) { setErrors(formErrors(error)); return; }
    const body = JSON.stringify(payload);
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setPreviewing(true); setPreviewError(undefined);
    form.update((value) => ({ ...value, preview: undefined }));
    try {
      const result = await api.previewImport<ImportPreview>(assignment.id, payload, controller.signal);
      const current = store.get(scope, initialImport).value;
      if (!controller.signal.aborted && body === JSON.stringify(payloadFor(current))) {
        form.update((value) => ({ ...value, preview: { body, result } }));
      }
    } catch (error) { if (!controller.signal.aborted) setPreviewError(error); }
    finally { if (!controller.signal.aborted) setPreviewing(false); }
  };
  const commit = async () => {
    let payload: ImportPayload;
    try { payload = importForCommit(payloadFor(form.value), form.value.preview); setErrors([]); }
    catch (error) { setErrors(formErrors(error)); return; }
    try { await write.operation.run(`/v1/assignments/${assignment.id}/imports`, payload); }
    catch { /* The server revalidates every row inside its transaction. */ }
  };
  const disabled = write.locked || previewing;
  const previewRows = form.value.preview ? (JSON.parse(form.value.preview.body) as ImportPayload).rows : [];
  if (form.value.receipt) return <section className="panel prep-card"><h2>答案已导入</h2>
    <p>共 {form.value.receipt.answer_version_ids.length} 份答案，其中 {form.value.receipt.reused} 份复用了相同来源和提交版本。</p>
    <p className="muted">下一步选择要初评的具体提交版本，并确认评估配置。</p>
    <div className="button-row"><Button type="primary" onClick={onBatch}>选择答案并创建批次</Button>
      <Button onClick={() => { write.operation.reset(); form.set(initialImport(), false); }}>开始另一次导入</Button></div>
  </section>;
  return <section className="panel prep-card"><h2>导入文本答案</h2>
    <p className="muted">题号 {question.question_key} · 题目第 {question.version} 版 · 固定名单 {assignment.roster.length} 人</p>
    <Segmented disabled={disabled} value={form.value.mode} onChange={(value) => edit((v) => ({ ...v, mode: value as ImportForm["mode"] }))}
      options={[{ value: "manual", label: "逐份录入" }, { value: "json", label: "粘贴结构化答案" }]} />
    <p className="muted small prep-hint">空字符串表示已提交空白答案；未交作业的学生请不要添加答案行。修改已导入内容时请使用新的提交版本。</p>
    {form.value.mode === "manual" ? <>
      {form.value.rows.map((row, index) => <section key={row.key} className="answer-entry">
        <div className="panel-toolbar prep-toolbar"><h3>答案 {index + 1}</h3><Button type="text" disabled={disabled || form.value.rows.length === 1}
          onClick={() => edit((v) => ({ ...v, rows: v.rows.filter((value) => value.key !== row.key) }))}>移除本份答案</Button></div>
        <div className="prep-grid"><Field label="学生"><Select aria-label={`答案 ${index + 1} 学生`} showSearch optionFilterProp="label" disabled={disabled}
          value={row.student_id || undefined} placeholder="按学号或姓名选择"
          options={assignment.roster.map((student) => ({ value: student.id, label: `${student.external_id} · ${student.name}` }))}
          onChange={(id) => { const student = assignment.roster.find((value) => value.id === id)!; editRow(row.key, { student_id: id, external_submission_id: submissionId(student) }); }} /></Field>
          <Field label="提交版本"><Input aria-label={`答案 ${index + 1} 提交版本`} maxLength={80} value={row.source_revision} disabled={disabled}
            onChange={(event) => editRow(row.key, { source_revision: event.target.value })} /></Field></div>
        <Field label="学生原答案"><Input.TextArea aria-label={`答案 ${index + 1} 原文`} disabled={disabled} autoSize={{ minRows: 4, maxRows: 18 }}
          value={row.answer_text} onChange={(event) => editRow(row.key, { answer_text: event.target.value })} />
          <span className="muted small">{charCount(row.answer_text)} 字符</span></Field>
        <Collapse ghost size="small" items={[{ key: "source", label: "来源编号（可选调整）", children:
          <Field label="来源编号" hint="同一来源的后续订正保留编号，修改提交版本。"><Input aria-label={`答案 ${index + 1} 来源编号`} disabled={disabled} maxLength={120}
            value={row.external_submission_id} onChange={(event) => editRow(row.key, { external_submission_id: event.target.value })} /></Field> }]} />
      </section>)}
      <Button disabled={disabled || form.value.rows.length >= 200} onClick={() => edit((v) => ({ ...v, rows: [...v.rows, emptyAnswer()] }))}>添加一份答案</Button>
    </> : <div className="prep-json">
      <p className="muted small">使用学号关联当前名单，不需要填写数据库 ID。原文中的换行可写成 \n；需要保留 CRLF 时写成 \r\n。</p>
      <pre className="json-example">{JSON.stringify([{ external_student_id: assignment.roster[0]?.external_id || "学号", external_submission_id: "来源编号", source_revision: "initial", answer_text: "学生原文\r\n第二行" }], null, 2)}</pre>
      <Input.TextArea aria-label="结构化答案 JSON" className="json-input" autoSize={{ minRows: 8, maxRows: 24 }} disabled={disabled}
        maxLength={12_500_000} value={form.value.json} placeholder="粘贴答案对象数组，保留 answer_text 内的原文。"
        onChange={(event) => edit((v) => ({ ...v, json: event.target.value }))} />
    </div>}
    <div className="prep-actions"><Button disabled={disabled} loading={previewing} onClick={() => void preview()}>检查并预览本次导入</Button>
      <Button type="primary" disabled={disabled || !form.value.preview?.result.valid} loading={write.phase === "sending"} onClick={() => void commit()}>确认导入预览中的答案</Button></div>
    <FormErrors errors={errors} /><ErrorNotice error={previewError} /><WriteFeedback state={write} retry={() => write.operation.retry()} />
    {form.value.preview && <div className="import-preview"><div className="notice success">
      服务器预览通过：{form.value.preview.result.row_count} 份，其中 {form.value.preview.result.reused} 份可复用。尚未写入，提交时会再次校验。</div>
      <Table rowKey={answerIdentity} dataSource={previewRows} pagination={{ pageSize: 10 }} scroll={{ x: 600 }} columns={[
        { title: "学生", key: "student", render: (_, row) => { const student = assignment.roster.find((s) => s.id === row.student_id); return `${student?.external_id} · ${student?.name}`; } },
        { title: "提交版本", dataIndex: "source_revision" },
        { title: "原文", key: "text", render: (_, row) => <details><summary>{charCount(row.answer_text)} 字符 · 查看完整原文</summary><pre className="plain-text preview-answer">{row.answer_text || "（空白答案）"}</pre></details> },
      ]} />
    </div>}
  </section>;
}
