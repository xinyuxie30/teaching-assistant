import { Alert, Button, Checkbox, Form, Input, InputNumber, Select, Tabs, Tag } from "antd";
import { useEffect, useState } from "react";
import { ApiClient, ApiError } from "../../api/client";
import { useOperation, usePage, useResource } from "../../api/hooks";
import type { AgentRun } from "../../api/types";
import { dateTime, ErrorNotice, Pager, PagedSelect } from "../../components/common";
import type { BankQuestion, Basis, Candidate, Evidence, Memory, PracticeArtifact, Preview } from "./types";

const status: Record<string, string> = { queued: "等待 Worker", running: "正在起草", waiting_teacher: "待教师确认", completed: "已确认", failed: "任务失败", cancelled: "已取消" };
const difficultyOptions = [{ value: "basic", label: "基础" }, { value: "standard", label: "标准" }, { value: "advanced", label: "拓展" }];
const tags = (text: string) => [...new Set(text.split(/[,，\n]/).map(s => s.trim()).filter(Boolean))];

function RetryWrite({ operation, phase, error, after }: ReturnType<typeof useOperation> & { after?: () => void }) {
  return <><ErrorNotice error={error} />{phase === "uncertain" && <Alert type="warning" title="提交结果未知，请先重试原请求。" action={
    <Button onClick={() => void operation.retry().then(() => after?.()).catch(() => undefined)}>重试原请求</Button>
  } />}</>;
}

function EvidenceView({ batches, evidence }: { batches: Basis[]; evidence: Evidence[] }) {
  return <div className="practice-evidence">
    {batches.map(batch => <p key={batch.batch_id}>
      <strong>{batch.scope.assignment_title} · {batch.scope.question_key}</strong>：已确认 {batch.coverage.confirmed}/{batch.coverage.batch_students} 份
      {batch.coverage.simulated_confirmed > 0 && <Tag color="orange">含模拟初评后的确认</Tag>}
    </p>)}
    <ul>{evidence.map(item => <li key={item.id}>{item.description}：已确认样本 {item.confirmed_students} 份，其中未满分 {item.below_full_credit_students} 份（{item.below_full_credit_rate}%）</li>)}</ul>
    <p className="muted small">统计分批保留，不合并为唯一学生人数；未满分是讲评线索，不直接代表特定错误原因。</p>
  </div>;
}

function QuestionView({ question }: { question: BankQuestion }) {
  return <><h4>{question.title}</h4><div className="button-row"><Tag>{question.grade}</Tag><Tag>{difficultyOptions.find(o => o.value === question.difficulty)?.label || question.difficulty}</Tag><Tag>{question.question_type}</Tag>{question.knowledge_points.map(tag => <Tag key={tag}>{tag}</Tag>)}</div>
    <p className="practice-text">{question.prompt}</p>
    <details><summary>查看参考答案</summary><p className="practice-text">{question.expected_answer}</p></details>
    <p className="muted small">来源：{question.source}</p></>;
}

function MatchView({ candidate, evidence }: { candidate: Candidate; evidence: Evidence[] }) {
  return <p className="muted small">匹配依据：{candidate.evidence_matches.map(m => `${evidence.find(e => e.id === m.evidence_id)?.description || "历史评分点"}（${m.terms.join("、")}）`).join("；")}
    {candidate.topic_matches.length > 0 && ` 教师指定知识点：${candidate.topic_matches.join("、")}`}
    {candidate.memory_ids.length > 0 && `；关联课程记忆 ${candidate.memory_ids.length} 条`}</p>;
}

function BankPanel({ api, courseId }: { api: ApiClient; courseId: string }) {
  const bank = usePage<BankQuestion>(api, `/v1/courses/${courseId}/practice-questions`);
  const write = useOperation(api, `practice-bank:${courseId}`);
  const stateWrite = useOperation(api, `practice-bank-state:${courseId}`);
  const [form] = Form.useForm();
  const [json, setJson] = useState("");
  const [localError, setLocalError] = useState<unknown>();
  useEffect(() => { if (write.phase === "succeeded" || stateWrite.phase === "succeeded") bank.first(); }, [write.result, stateWrite.result]);
  const submit = async (questions: unknown[]) => {
    setLocalError(undefined);
    try { await write.operation.run(`/v1/courses/${courseId}/practice-questions`, { questions }); } catch { /* state holds failure and retry */ }
  };
  return <div className="practice-stack"><Alert type="info" title="题库仅供当前课程使用。导入后逐题查看并确认，补练只检索已确认且未停用的题目。修改内容请导入新版本，再停用旧题。" />
    <section className="panel practice-panel"><h3>添加题目</h3><Form form={form} layout="vertical" disabled={write.locked} onFinish={values => void submit([{ ...values, knowledge_points: tags(values.knowledge_points) }])} initialValues={{ difficulty: "standard", question_type: "简答题" }}>
      <div className="practice-fields">
        <Form.Item name="title" label="题目标题" rules={[{ required: true, max: 100 }]}><Input aria-label="题目标题" maxLength={100} /></Form.Item>
        <Form.Item name="grade" label="题库年级" rules={[{ required: true, max: 100 }]}><Input aria-label="题库年级" maxLength={100} /></Form.Item>
        <Form.Item name="difficulty" label="题库难度" rules={[{ required: true }]}><Select aria-label="题库难度" options={difficultyOptions} /></Form.Item>
        <Form.Item name="question_type" label="题库题型" rules={[{ required: true, max: 100 }]}><Input aria-label="题库题型" maxLength={100} /></Form.Item>
      </div>
      <Form.Item name="knowledge_points" label="知识点（用逗号分隔，最多 12 个）" rules={[{ required: true }]}><Input aria-label="题库知识点" /></Form.Item>
      <Form.Item name="prompt" label="题干" rules={[{ required: true, max: 4000 }]}><Input.TextArea aria-label="题干" rows={3} maxLength={4000} /></Form.Item>
      <Form.Item name="expected_answer" label="参考答案" rules={[{ required: true, max: 4000 }]}><Input.TextArea aria-label="参考答案" rows={3} maxLength={4000} /></Form.Item>
      <Form.Item name="source" label="题目来源（自编或已获授权的来源）" rules={[{ required: true, max: 500 }]}><Input aria-label="题目来源" maxLength={500} /></Form.Item>
      <Button htmlType="submit" type="primary" loading={write.phase === "sending"}>保存待确认题目</Button>
    </Form><RetryWrite {...write} />
    <details><summary>批量导入 JSON</summary><p className="muted small">输入题目数组，每批 1–100 题；字段为 title、prompt、expected_answer、knowledge_points（数组）、grade、difficulty（basic/standard/advanced）、question_type、source。完全相同的题目自动复用。</p>
      <Input.TextArea aria-label="题库 JSON" rows={5} value={json} onChange={e => setJson(e.target.value)} disabled={write.locked} />
      <Button disabled={write.locked || !json.trim()} onClick={() => { try { const rows: unknown = JSON.parse(json); if (!Array.isArray(rows)) throw new Error(); void submit(rows); } catch { setLocalError(new ApiError("validation_error", 422, "Expected array")); } }}>导入题目数组</Button>
    </details><ErrorNotice error={localError} /></section>
    <section className="panel practice-panel"><h3>课程题库</h3><RetryWrite {...stateWrite} /><ErrorNotice error={bank.error} retry={bank.refresh} />
      {bank.data?.items.map(question => <article className="practice-question" key={question.id}><QuestionView question={question} />
        <div className="button-row"><Tag color={question.retired_at ? "default" : question.confirmed_at ? "green" : "orange"}>{question.retired_at ? "已停用" : question.confirmed_at ? "已确认" : "待确认"}</Tag>
          {!question.confirmed_at && !question.retired_at && <Button disabled={stateWrite.locked} onClick={() => void stateWrite.operation.run(`/v1/courses/${courseId}/practice-questions/${question.id}/confirm`, { content_hash: question.content_hash }).catch(() => undefined)}>确认入库</Button>}
          {!question.retired_at && <Button disabled={stateWrite.locked} onClick={() => void stateWrite.operation.run(`/v1/courses/${courseId}/practice-questions/${question.id}/retire`, { content_hash: question.content_hash }).catch(() => undefined)}>停用此题</Button>}
        </div></article>)}
      {bank.data?.total === 0 && <p>尚无题目，请先添加。</p>}
      <Pager page={bank.data} {...bank} loading={bank.loading} />
    </section></div>;
}

function MemoryPanel({ api, courseId }: { api: ApiClient; courseId: string }) {
  const [form] = Form.useForm();
  const memory = useResource<{ items: Memory[] }>(api, `/v1/courses/${courseId}/teaching-memories`);
  const create = useOperation(api, `practice-memory-create:${courseId}`);
  const confirm = useOperation(api, `practice-memory-confirm:${courseId}`);
  const pending = create.result as Memory | undefined;
  useEffect(() => { if (confirm.phase === "succeeded") memory.refresh(); }, [confirm.result]);
  return <section className="panel practice-panel"><h3>课程记忆</h3><p className="muted">记录课程共用的教学偏好或经验。班级差异来自补练中选定的历史批次；请勿在这里写入学生身份信息。</p>
    <Form form={form} layout="vertical" disabled={create.locked} onFinish={v => void create.operation.run(`/v1/courses/${courseId}/teaching-memories`, { kind: "experience", title: v.title, body: v.body, source: { kind: "teacher_note", note: "教师在补练工作台录入的课程经验" } }).catch(() => undefined)}>
      <Form.Item name="title" label="记忆标题" rules={[{ required: true }]}><Input aria-label="记忆标题" maxLength={200} /></Form.Item>
      <Form.Item name="body" label="教学经验或偏好" rules={[{ required: true }]}><Input.TextArea aria-label="教学经验或偏好" maxLength={2000} rows={3} /></Form.Item>
      <Button htmlType="submit" disabled={create.locked}>保存待确认记忆</Button>
    </Form><RetryWrite {...create} /><RetryWrite {...confirm} />
    {pending && <article className="practice-question"><strong>{pending.title}</strong><p>{pending.body}</p><Button disabled={confirm.locked || (confirm.result as Memory | undefined)?.id === pending.id} onClick={() => void confirm.operation.run(`/v1/courses/${courseId}/teaching-memories/${pending.id}/confirm`, { content_hash: pending.content_hash }).catch(() => undefined)}>确认这条课程记忆</Button></article>}
    <ErrorNotice error={memory.error} retry={memory.refresh} /><h4>最近的有效记忆（最多 5 条）</h4>
    {memory.data?.items.map(m => <article key={m.id}><strong>{m.title}</strong><p>{m.body}</p></article>)}
  </section>;
}

function RunView({ api, runId, onChange }: { api: ApiClient; runId: string; onChange: () => void }) {
  const run = useResource<AgentRun>(api, `/v1/agent-runs/${runId}`, {}, r => ["queued", "running"].includes(r.status));
  const artifact = useResource<PracticeArtifact>(api, run.data && ["waiting_teacher", "completed"].includes(run.data.status) ? `/v1/agent-runs/${runId}/artifact` : null);
  const confirm = useOperation(api, `practice-confirm-run:${runId}`);
  const cancel = useOperation(api, `practice-cancel-run:${runId}`);
  useEffect(() => { if (run.data) onChange(); }, [run.data?.status, onChange]);
  useEffect(() => { if (confirm.phase === "succeeded" || cancel.phase === "succeeded") { run.refresh(); artifact.refresh(); } }, [confirm.result, cancel.result]);
  return <section className="panel practice-panel"><h3>补练任务 <Tag>{status[run.data?.status || ""] || "读取中"}</Tag></h3>
    <ErrorNotice error={run.error} retry={run.refresh} /><ErrorNotice error={artifact.error} retry={artifact.refresh} />
    <RetryWrite {...confirm} /><RetryWrite {...cancel} />
    {run.data && <p className="muted small">{dateTime(run.data.created_at)} · 模型调用 {run.data.models_used}/{run.data.limits.max_model_calls} · {run.data.simulation ? "模拟模式" : "真实模型"}</p>}
    {run.data?.status === "queued" && <Alert type="info" title="任务已保存，等待后台处理；长时间不开始时请确认 Harness Worker 已启动。" />}
    {run.data?.error_code && <Alert type="error" title={`任务未完成：${run.data.error_code}`} description="可保留历史记录，调整条件后创建新任务。" />}
    {run.data && ["queued", "running"].includes(run.data.status) && <Button disabled={cancel.locked} onClick={() => void cancel.operation.run(`/v1/agent-runs/${runId}/cancel`, {}).catch(() => undefined)}>取消这次起草</Button>}
    {artifact.data && <><h3>{artifact.data.body.title}</h3>{artifact.data.body.simulation && <Alert type="warning" title="模拟草稿，仅验证流程，教学适配性尚需教师判断。" />}
      <p className="practice-text">{artifact.data.body.teaching_notes}</p><EvidenceView batches={artifact.data.body.batches} evidence={artifact.data.body.evidence} />
      {artifact.data.body.questions.map(item => <article className="practice-question" key={item.question.id}><QuestionView question={item.question} /><p>选题理由：{item.reason}</p><MatchView candidate={item} evidence={artifact.data!.body.evidence} /></article>)}
      {artifact.data.body.memories.length > 0 && <details><summary>引用的课程记忆</summary>{artifact.data.body.memories.map(m => <p key={m.id}><strong>{m.title}</strong>：{m.body}</p>)}</details>}
      <p className="muted small">本草稿使用创建任务时固定的历史与题库快照。可回到上方调整条件、选题和要求后另存新草稿，原稿保留。</p>
      <Button type="primary" disabled={run.data?.status !== "waiting_teacher" || confirm.locked} onClick={() => void confirm.operation.run(`/v1/agent-runs/${runId}/confirm`, { content_hash: artifact.data!.content_hash }).catch(() => undefined)}>{run.data?.status === "completed" ? "教师已确认" : "确认这份补练"}</Button>
    </>}
  </section>;
}

function ClassroomPractice({ api, classroomId }: { api: ApiClient; classroomId: string }) {
  const batches = usePage<{ id: string; assignment_title: string; question_key: string; question_version: number }>(api, `/v1/classrooms/${classroomId}/practice-batches`);
  const history = usePage<AgentRun>(api, `/v1/classrooms/${classroomId}/practice-runs`);
  const profiles = useResource<{ items: { alias: string; ready: boolean }[] }>(api, "/v1/agent-profiles", { task_type: "class-practice" });
  const previewWrite = useOperation(api, `practice-preview:${classroomId}`);
  const create = useOperation(api, `practice-create:${classroomId}`);
  const [batchIds, setBatchIds] = useState<string[]>([]);
  const [knowledge, setKnowledge] = useState("");
  const [grade, setGrade] = useState("");
  const [difficulty, setDifficulty] = useState<string>();
  const [questionType, setQuestionType] = useState("");
  const [memoryQuery, setMemoryQuery] = useState("");
  const [count, setCount] = useState(3);
  const [instructions, setInstructions] = useState("根据本班已确认的历史评分点表现，选择补练题并说明安排。");
  const [profile, setProfile] = useState("fake-class-practice-v1");
  const [selection, setSelection] = useState<string[]>([]);
  const [activeRun, setActiveRun] = useState<string>();
  const request = { batch_ids: [...batchIds].sort(), knowledge_points: tags(knowledge), grade: grade.trim() || null, difficulty: difficulty || null, question_type: questionType.trim() || null, memory_query: memoryQuery, question_count: count, instructions };
  const serialized = JSON.stringify(request);
  const preview = previewWrite.phase === "succeeded" && previewWrite.ticket?.body === serialized ? previewWrite.result as Preview : undefined;
  useEffect(() => { if (previewWrite.phase === "succeeded") setSelection([]); }, [previewWrite.result]);
  useEffect(() => { if (create.phase === "succeeded") { setActiveRun((create.result as AgentRun).id); history.first(); } }, [create.result]);
  const locked = create.locked || previewWrite.locked;
  return <div className="practice-stack"><section className="panel practice-panel"><h3>1. 选择历史批次与补练要求</h3><p className="muted">最多选择 10 个批次。同一作业的同一道题只选一个批次，避免重复统计。只使用教师已确认的结果。</p>
    <ErrorNotice error={batches.error} retry={batches.refresh} />
    {batches.data?.items.map(batch => <label className="practice-batch" key={batch.id}><Checkbox disabled={locked || (!batchIds.includes(batch.id) && batchIds.length >= 10)} checked={batchIds.includes(batch.id)} onChange={e => setBatchIds(old => e.target.checked ? [...old, batch.id] : old.filter(id => id !== batch.id))} />{batch.assignment_title} · {batch.question_key} · 题目版本 {batch.question_version}</label>)}
    <p>已选 {batchIds.length} 个批次 <Button size="small" disabled={locked} onClick={() => setBatchIds([])}>清空选择</Button></p><Pager page={batches.data} {...batches} loading={batches.loading} />
    <Form layout="vertical" disabled={locked}><div className="practice-fields">
      <Form.Item label="年级筛选"><Input aria-label="年级筛选" value={grade} onChange={e => setGrade(e.target.value)} maxLength={100} placeholder="留空不限" /></Form.Item>
      <Form.Item label="难度筛选"><Select aria-label="难度筛选" allowClear value={difficulty} onChange={setDifficulty} options={difficultyOptions} placeholder="不限" /></Form.Item>
      <Form.Item label="题型筛选"><Input aria-label="题型筛选" value={questionType} onChange={e => setQuestionType(e.target.value)} maxLength={100} placeholder="留空不限" /></Form.Item>
      <Form.Item label="补练题数"><InputNumber aria-label="补练题数" min={1} max={5} precision={0} value={count} onChange={v => setCount(v || 1)} /></Form.Item>
    </div>
    <Form.Item label="补充知识点（逗号分隔，可选）"><Input aria-label="补充知识点" value={knowledge} onChange={e => setKnowledge(e.target.value)} placeholder="评分点与题库表述不同时，可明确指定知识点" /></Form.Item>
    <Form.Item label="课程记忆关键词（可选）"><Input aria-label="课程记忆关键词" value={memoryQuery} maxLength={200} onChange={e => setMemoryQuery(e.target.value)} placeholder="留空读取最近 5 条有效记忆" /></Form.Item>
    <Form.Item label="本次补练要求"><Input.TextArea aria-label="本次补练要求" value={instructions} maxLength={4000} rows={3} onChange={e => setInstructions(e.target.value)} /></Form.Item>
    </Form><Button type="primary" disabled={locked || !batchIds.length || !instructions.trim()} loading={previewWrite.phase === "sending"} onClick={() => void previewWrite.operation.run(`/v1/classrooms/${classroomId}/practice-runs/preview`, request).catch(() => undefined)}>检索适配题目</Button><RetryWrite {...previewWrite} />
    </section>
    {preview && <section className="panel practice-panel"><h3>2. 查看证据与候选题</h3><EvidenceView batches={preview.batches} evidence={preview.evidence} />
      <p>匹配 {preview.matching_count} 题，展示前 {preview.candidates.length} 题。按词项与知识点匹配排序；可勾选恰好 {count} 题，或不勾选，由 Agent 从候选中选择。</p>
      {preview.candidates.length < count && <Alert type="warning" title="匹配题目不足" description="请补充并确认课程题库、调整筛选或减少题数，再重新检索。" />}
      {preview.candidates.map(candidate => <article className="practice-question" key={candidate.question.id}><Checkbox disabled={locked} checked={selection.includes(candidate.question.id)} onChange={e => setSelection(old => e.target.checked ? [...old, candidate.question.id] : old.filter(id => id !== candidate.question.id))}>选择此题：{candidate.question.title}</Checkbox><QuestionView question={candidate.question} /><MatchView candidate={candidate} evidence={preview.evidence} /></article>)}
      <details><summary>本次检索的课程记忆（{preview.memories.length} 条）</summary>{preview.memories.map(m => <p key={m.id}><strong>{m.title}</strong>：{m.body}</p>)}</details>
      <ErrorNotice error={profiles.error} retry={profiles.refresh} /><div className="practice-fields"><Form.Item label="起草模型"><Select aria-label="补练起草模型" disabled={locked} value={profile} onChange={setProfile} options={(profiles.data?.items || []).map(p => ({ value: p.alias, label: p.alias.startsWith("fake") ? "模拟模式 · 验证流程" : "DeepSeek · 真实调用", disabled: !p.ready }))} /></Form.Item></div>
      <Button type="primary" disabled={locked || preview.candidates.length < count || (selection.length > 0 && selection.length !== count) || !profiles.data?.items.some(p => p.alias === profile && p.ready)} loading={create.phase === "sending"} onClick={() => void create.operation.run(`/v1/classrooms/${classroomId}/practice-runs`, { ...request, preview_hash: preview.preview_hash, selected_question_ids: selection, agent_profile: profile }).catch(() => undefined)}>生成补练草稿</Button>
      <p className="muted small">已选 {selection.length} 题。题干、答案和来源保留题库原文；教师确认前不会发布。</p>
    </section>}<RetryWrite {...create} />
    <section className="panel practice-panel"><h3>补练历史</h3><Button onClick={history.refresh}>刷新历史</Button><ErrorNotice error={history.error} retry={history.refresh} />
      {history.data?.items.map(r => <p key={r.id}><Button type={activeRun === r.id ? "primary" : "default"} onClick={() => setActiveRun(r.id)}>{dateTime(r.created_at)} · {status[r.status] || r.status}</Button></p>)}<Pager page={history.data} {...history} loading={history.loading} />
    </section>{activeRun && <RunView api={api} runId={activeRun} key={activeRun} onChange={history.refresh} />}
  </div>;
}

export function PracticePage({ api }: { api: ApiClient }) {
  const [courseId, setCourseId] = useState<string>();
  const [classroomId, setClassroomId] = useState<string>();
  return <div className="practice-stack"><div className="page-heading"><h1>针对性补练</h1><p className="muted">从班级历史中的教师确认结果出发，检索课程题库，生成可追溯的全班补练草稿。</p></div>
    <div className="practice-fields"><PagedSelect api={api} path="/v1/courses" label="补练课程" value={courseId} onChange={id => { setCourseId(id); setClassroomId(undefined); }} /><PagedSelect api={api} path={courseId ? `/v1/courses/${courseId}/classrooms` : null} label="补练班级" value={classroomId} onChange={setClassroomId} /></div>
    {courseId ? <Tabs items={[
      { key: "practice", label: "班级补练", children: classroomId ? <ClassroomPractice key={classroomId} api={api} classroomId={classroomId} /> : <p>请选择班级以查看历史批次。</p> },
      { key: "bank", label: "课程题库", children: <BankPanel key={courseId} api={api} courseId={courseId} /> },
      { key: "memory", label: "课程记忆", children: <MemoryPanel key={courseId} api={api} courseId={courseId} /> },
    ]} /> : <p>请选择课程，或先在“作业准备”中创建课程与班级。</p>}
  </div>;
}
