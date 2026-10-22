import { App, Button, Input, Modal, Select, Table, Tag } from "antd";
import { useState } from "react";
import { ApiClient } from "../../api/client";
import { useOperation, usePage, useResource } from "../../api/hooks";
import type { AnswerDetail, AnswerRow, AssignmentDetail, BatchReceipt, ProfileList, QuestionDetail, RubricDetail } from "../../api/types";
import { dateTime, ErrorNotice, Loading, Pager } from "../../components/common";
import { FormErrors, useAccepted, WriteFeedback } from "./common";
import { PreparationSession, usePreparationState } from "./session";

function AnswerPreview({ api, id, close }: { api: ApiClient; id?: string; close: () => void }) {
  const resource = useResource<AnswerDetail>(api, id ? `/v1/answer-versions/${id}` : null);
  return <Modal open={!!id} title="固定版本的学生原答案" onCancel={close} footer={<Button onClick={close}>关闭</Button>} width={800}>
    <ErrorNotice error={resource.error} retry={resource.refresh} />
    {resource.loading && <Loading />}
    {resource.data && <><p className="muted">提交版本 {resource.data.source_revision} · 来源 {resource.data.external_submission_id}</p>
      <pre className="plain-text preview-answer">{resource.data.answer_text || "（空白答案）"}</pre></>}
  </Modal>;
}

export function BatchCreator({ api, store, assignment, question, rubric, onOpen }: {
  api: ApiClient; store: PreparationSession; assignment: AssignmentDetail; question: QuestionDetail;
  rubric: RubricDetail; onOpen: (id: string) => void;
}) {
  const { modal } = App.useApp();
  const scope = `${question.id}:${rubric.id}`;
  const form = usePreparationState(store, `batch-form:${scope}`, () => ({
    selected: {} as Record<string, AnswerRow>, profileId: "fake-v1", receipt: undefined as BatchReceipt | undefined,
  }));
  const [filter, setFilter] = useState("");
  const [previewId, setPreviewId] = useState<string>();
  const [errors, setErrors] = useState<string[]>([]);
  const profiles = useResource<ProfileList>(api, "/v1/evaluation-profiles");
  const answers = usePage<AnswerRow>(api, `/v1/assignments/${assignment.id}/answer-versions`, {
    question_version_id: question.id, source_revision: filter || undefined,
  });
  const write = useOperation(api, `prepare:batch:${scope}`);
  useAccepted<BatchReceipt>(write, (receipt) => form.update((value) => ({ ...value, receipt }), false));
  const selected = Object.values(form.value.selected);
  const profile = profiles.data?.items.find((item) => item.id === form.value.profileId && item.ready);
  const create = () => {
    const problems: string[] = [];
    if (!selected.length || selected.length > 200) problems.push("请选择 1–200 份答案。");
    if (new Set(selected.map((answer) => answer.student_id)).size !== selected.length) problems.push("同一学生存在多个提交版本，请保留本批次要评估的一个版本。");
    if (!profile?.id) problems.push("请选择当前可用的评估配置。");
    if (rubric.question_version_id !== question.id || selected.some((answer) => answer.question_version_id !== question.id)) problems.push("题目、答案和细则版本不一致，请重新选择。");
    setErrors(problems);
    if (problems.length || !profile) return;
    const payload = { rubric_version_id: rubric.id, answer_version_ids: selected.map((answer) => answer.id), evaluation_profile: profile.id };
    modal.confirm({
      title: "确认创建这个初评批次？",
      content: <div><p>{assignment.title} · {question.question_key} 第 {question.version} 版</p>
        <p>固定细则第 {rubric.version} 版 · {selected.length} 份答案 · 每份 {rubric.body.criteria.length} 个评分点</p>
        <p>评估器：{profile.manifest?.model || profile.alias}</p>
        <p>{profile.manifest?.simulation ? "模拟器仅演示处理流程，不做语义评分。" : "这会安排真实模型调用，可能产生费用；重试会增加用量。"}所有结果仍需老师复核。</p></div>,
      okText: "创建批次", cancelText: "返回检查",
      onOk: async () => { try { await write.operation.run("/v1/grading-batches", payload); } catch { /* Feedback stays in page. */ } },
    });
  };
  if (form.value.receipt) return <section className="panel prep-card"><h2>初评批次已创建</h2>
    <p>包含 {form.value.receipt.item_ids.length} 份答案。后台 Worker 会处理待办；相同评估目标可复用已有结果。</p>
    <div className="button-row"><Button type="primary" onClick={() => onOpen(form.value.receipt!.id)}>打开批次，查看进度与复核</Button>
      <Button onClick={() => { write.operation.reset(); form.set({ selected: {}, profileId: "fake-v1", receipt: undefined }, false); }}>准备另一个批次</Button></div>
  </section>;
  return <section className="panel prep-card"><h2>选择答案，创建初评批次</h2>
    <p className="muted">固定细则第 {rubric.version} 版。每位学生选择一个提交版本；选中项跨分页保留，旧结果不会被覆盖。</p>
    <div className="panel-toolbar prep-toolbar"><Input.Search aria-label="按提交版本筛选答案" placeholder="按提交版本筛选，例如 initial" allowClear
      onSearch={setFilter} style={{ maxWidth: 340 }} /><Button loading={answers.loading} onClick={answers.first}>刷新答案</Button></div>
    <ErrorNotice error={answers.error} retry={answers.first} />
    <Table<AnswerRow> rowKey="id" pagination={false} dataSource={answers.data?.items || []} loading={answers.loading} scroll={{ x: 780 }}
      rowSelection={{ selectedRowKeys: selected.map((answer) => answer.id), preserveSelectedRowKeys: true,
        getCheckboxProps: () => ({ disabled: write.locked }),
        onChange: (keys) => {
          const known = new Map([...selected, ...(answers.data?.items || [])].map((row) => [row.id, row]));
          form.update((value) => ({ ...value, selected: Object.fromEntries(keys.flatMap((key) => known.has(String(key)) ? [[String(key), known.get(String(key))!]] : [])) }));
          setErrors([]);
        },
      }} columns={[
        { title: "学生", key: "student", render: (_, row) => <>{row.student_name}<div className="muted small">{row.external_student_id}</div></> },
        { title: "提交版本", dataIndex: "source_revision" },
        { title: "来源编号", dataIndex: "external_submission_id", ellipsis: true },
        { title: "导入时间", dataIndex: "created_at", render: dateTime },
        { title: "", key: "action", render: (_, row) => <Button onClick={() => setPreviewId(row.id)}>查看原文</Button> },
      ]} locale={{ emptyText: answers.loading ? "正在读取答案…" : "当前条件下没有答案，请先导入或调整筛选。" }} />
    <Pager page={answers.data} {...answers} />
    <div className="selected-answers"><h3>已选 {selected.length} 份</h3>
      {selected.map((answer) => <Tag key={answer.id} closable={!write.locked} onClose={() => form.update((value) => ({ ...value,
        selected: Object.fromEntries(Object.entries(value.selected).filter(([id]) => id !== answer.id)) }))}>
        {answer.external_student_id} · {answer.source_revision}</Tag>)}
    </div>
    <div className="prep-grid"><div className="prep-field"><div className="prep-label">评估配置</div>
      <Select aria-label="批次评估配置" value={form.value.profileId} disabled={write.locked} loading={profiles.loading}
        onChange={(id) => form.update((value) => ({ ...value, profileId: id }))}
        options={(profiles.data?.items || []).map((item) => ({ value: item.id || `unavailable:${item.alias}`, disabled: !item.ready || !item.id,
          label: item.manifest?.simulation ? "模拟流程（不做语义评分）" : `${item.manifest?.model || item.alias}${item.ready ? "" : " · 尚未配置"}` }))} /></div>
      <div className="prep-profile-note"><p className="muted small">按所选固定配置执行。真实模型的概率与连续 Score 不直接充当学生分数。</p>
        <Button size="small" disabled={write.locked} onClick={profiles.refresh}>刷新可用配置</Button></div></div>
    <ErrorNotice error={profiles.error} retry={profiles.refresh} /><FormErrors errors={errors} />
    <Button type="primary" disabled={write.locked || profiles.loading} loading={write.phase === "sending"} onClick={create}>检查批次并创建</Button>
    <WriteFeedback state={write} retry={() => write.operation.retry()} />
    <AnswerPreview api={api} id={previewId} close={() => setPreviewId(undefined)} />
  </section>;
}
