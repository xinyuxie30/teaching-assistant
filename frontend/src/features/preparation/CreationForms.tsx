import { Button, Input, Table } from "antd";
import { useState } from "react";
import { ApiClient } from "../../api/client";
import { useOperation } from "../../api/hooks";
import type { QuestionDetail, VersionReceipt, WorkspaceReceipt } from "../../api/types";
import { PagedSelect } from "../../components/common";
import { Field, FormErrors, useAccepted, WriteFeedback } from "./common";
import { formErrors, keyFor, pointCents } from "./contracts";
import { PreparationSession, usePreparationState } from "./session";

export function WorkspaceCreator({ api, store, onOpen }: {
  api: ApiClient; store: PreparationSession; onOpen: (courseId: string) => void;
}) {
  const form = usePreparationState(store, "workspace-form", () => ({
    course: "", classroom: "", students: [{ key: keyFor("student"), external_id: "", name: "" }],
    receipt: undefined as WorkspaceReceipt | undefined,
  }));
  const write = useOperation(api, "prepare:workspace");
  const [errors, setErrors] = useState<string[]>([]);
  useAccepted<WorkspaceReceipt>(write, (receipt) => form.update((value) => ({ ...value, receipt }), false));
  const submit = async () => {
    const value = form.value;
    const problems: string[] = [];
    if (!value.course.trim() || !value.classroom.trim()) problems.push("请填写课程与班级名称。");
    if (value.students.some((student) => !student.external_id.trim() || !student.name.trim())) problems.push("请为每位学生填写学号和姓名。");
    if (new Set(value.students.map((student) => student.external_id.trim())).size !== value.students.length) problems.push("同一名单中的学号不能重复。");
    setErrors(problems);
    if (problems.length) return;
    try {
      await write.operation.run("/v1/workspaces", {
        course_name: value.course.trim(), classroom_name: value.classroom.trim(),
        students: value.students.map((student) => ({ external_id: student.external_id.trim(), name: student.name.trim() })),
      });
    } catch { /* Persistent write state supplies retry and error feedback. */ }
  };
  if (form.value.receipt) return <section className="panel prep-card">
    <h2>课程和班级已创建</h2><p>{form.value.course} · {form.value.classroom} · {form.value.receipt.students.length} 位学生</p>
    <div className="button-row"><Button type="primary" onClick={() => onOpen(form.value.receipt!.course_id)}>打开课程，创建作业</Button>
      <Button onClick={() => { write.operation.reset(); form.set({ course: "", classroom: "", students: [{ key: keyFor("student"), external_id: "", name: "" }], receipt: undefined }, false); }}>新建另一门课程</Button></div>
  </section>;
  const changeStudent = (key: string, field: "external_id" | "name", value: string) => form.update((previous) => ({
    ...previous, students: previous.students.map((student) => student.key === key ? { ...student, [field]: value } : student),
  }));
  return <section className="panel prep-card"><h2>新建课程与班级</h2>
    <div className="prep-grid"><Field label="课程名称"><Input aria-label="课程名称" maxLength={200} value={form.value.course} disabled={write.locked} onChange={(event) => form.update((v) => ({ ...v, course: event.target.value }))} /></Field>
      <Field label="班级名称"><Input aria-label="班级名称" maxLength={200} value={form.value.classroom} disabled={write.locked} onChange={(event) => form.update((v) => ({ ...v, classroom: event.target.value }))} /></Field></div>
    <h3>学生名单 <span className="muted small">最多 200 人</span></h3>
    <Table rowKey="key" pagination={false} dataSource={form.value.students} scroll={{ x: 500 }} columns={[
      { title: "学号", key: "external", render: (_, row, index) => <Input aria-label={`第 ${index + 1} 位学生学号`} maxLength={100} value={row.external_id} disabled={write.locked} onChange={(event) => changeStudent(row.key, "external_id", event.target.value)} /> },
      { title: "姓名", key: "name", render: (_, row, index) => <Input aria-label={`第 ${index + 1} 位学生姓名`} maxLength={120} value={row.name} disabled={write.locked} onChange={(event) => changeStudent(row.key, "name", event.target.value)} /> },
      { title: "", key: "remove", width: 70, render: (_, row) => <Button type="text" disabled={write.locked || form.value.students.length === 1} onClick={() => form.update((v) => ({ ...v, students: v.students.filter((s) => s.key !== row.key) }))}>移除</Button> },
    ]} />
    <div className="prep-actions"><Button disabled={write.locked || form.value.students.length >= 200} onClick={() => form.update((v) => ({ ...v, students: [...v.students, { key: keyFor("student"), external_id: "", name: "" }] }))}>添加学生</Button>
      <Button type="primary" disabled={write.locked} loading={write.phase === "sending"} onClick={() => void submit()}>保存课程、班级和名单</Button></div>
    <FormErrors errors={errors} /><WriteFeedback state={write} retry={() => write.operation.retry()} />
  </section>;
}

export function AssignmentCreator({ api, store, courseId, onOpen }: {
  api: ApiClient; store: PreparationSession; courseId: string; onOpen: (id: string) => void;
}) {
  type Receipt = { id: string; roster_size: number };
  const form = usePreparationState(store, `assignment-form:${courseId}`, () => ({ title: "", classroomId: "", receipt: undefined as Receipt | undefined }));
  const write = useOperation(api, `prepare:assignment:${courseId}`);
  const [errors, setErrors] = useState<string[]>([]);
  useAccepted<Receipt>(write, (receipt) => form.update((value) => ({ ...value, receipt }), false));
  const submit = async () => {
    if (!form.value.title.trim() || !form.value.classroomId) { setErrors(["请填写作业名称并选择班级。"]); return; }
    setErrors([]);
    try { await write.operation.run(`/v1/courses/${courseId}/assignments`, { title: form.value.title.trim(), classroom_id: form.value.classroomId }); }
    catch { /* Handled by WriteFeedback. */ }
  };
  return <section className="panel prep-card"><h2>创建作业</h2>
    {form.value.receipt ? <><p>已保存「{form.value.title}」，固定名单包含 {form.value.receipt.roster_size} 位学生。</p>
      <div className="button-row"><Button type="primary" onClick={() => onOpen(form.value.receipt!.id)}>添加题目</Button>
        <Button onClick={() => { write.operation.reset(); form.set({ title: "", classroomId: form.value.classroomId, receipt: undefined }, false); }}>创建另一份作业</Button></div></>
      : <><div className="prep-grid"><Field label="作业名称"><Input aria-label="作业名称" maxLength={200} disabled={write.locked} value={form.value.title} onChange={(event) => form.update((v) => ({ ...v, title: event.target.value }))} /></Field>
        <PagedSelect api={api} path={`/v1/courses/${courseId}/classrooms`} label="班级" placeholder="选择作业所属班级" disabled={write.locked}
          value={form.value.classroomId || undefined} onChange={(value) => form.update((v) => ({ ...v, classroomId: value || "" }))} /></div>
      <p className="muted small">创建时固定班级名单，后续答案导入使用这份作业自己的名单。</p>
      <Button type="primary" disabled={write.locked} loading={write.phase === "sending"} onClick={() => void submit()}>创建作业并固定名单</Button>
      <FormErrors errors={errors} /><WriteFeedback state={write} retry={() => write.operation.retry()} /></>}
  </section>;
}

export function QuestionCreator({ api, store, assignmentId, onOpen, source }: {
  api: ApiClient; store: PreparationSession; assignmentId: string; onOpen: (id: string) => void; source?: QuestionDetail;
}) {
  const scope = `${assignmentId}:${source?.id || "new"}`;
  const form = usePreparationState(store, `question-form:${scope}`, () => ({
    question_key: source?.question_key || "", prompt: source?.prompt || "", reference: source?.reference || "",
    maximum_points: source?.maximum_points || "10.00", receipt: undefined as VersionReceipt | undefined,
  }));
  const write = useOperation(api, `prepare:question:${scope}`);
  const [errors, setErrors] = useState<string[]>([]);
  useAccepted<VersionReceipt>(write, (receipt) => form.update((v) => ({ ...v, receipt }), false));
  const submit = async () => {
    try {
      if (!/^[A-Za-z0-9_-]{1,80}$/.test(form.value.question_key)) throw new Error("题号请使用 1–80 位字母、数字、下划线或短横线，例如 q1。");
      if (!form.value.prompt.trim() || !form.value.reference.trim()) throw new Error("请填写题干，以及参考答案或课程材料。");
      const points = pointCents(form.value.maximum_points);
      if (points === null || points <= 0) throw new Error("题目满分需大于 0 且不超过 10000，最多两位小数。");
      setErrors([]);
      const { receipt: _receipt, ...payload } = form.value;
      await write.operation.run(`/v1/assignments/${assignmentId}/question-versions`, payload);
    } catch (error) { if (!(error instanceof Error && error.name === "ApiError")) setErrors(formErrors(error)); }
  };
  return <section className="panel prep-card"><h2>{source ? `为题号 ${source.question_key} 创建新版本` : "添加题目"}</h2>
    {form.value.receipt ? <><p>题目第 {form.value.receipt.version} 版已保存。已有答案和评分规则仍关联它们原来的版本。</p>
      <div className="button-row"><Button type="primary" onClick={() => onOpen(form.value.receipt!.id)}>打开题目，准备评分细则</Button>
        {!source && <Button onClick={() => { write.operation.reset(); form.set({ question_key: "", prompt: "", reference: "", maximum_points: "10.00", receipt: undefined }, false); }}>添加另一道题</Button>}</div></>
      : <><div className="prep-grid"><Field label="题号" hint="同一题号再次保存会产生新版本。"><Input aria-label="题号" maxLength={80} disabled={write.locked || !!source} value={form.value.question_key} onChange={(event) => form.update((v) => ({ ...v, question_key: event.target.value }))} /></Field>
        <Field label="题目满分"><Input aria-label="题目满分" inputMode="decimal" maxLength={8} disabled={write.locked} value={form.value.maximum_points} onChange={(event) => form.update((v) => ({ ...v, maximum_points: event.target.value }))} /></Field></div>
      <Field label="题干"><Input.TextArea aria-label="题干" autoSize={{ minRows: 3, maxRows: 12 }} maxLength={10000} disabled={write.locked} value={form.value.prompt} onChange={(event) => form.update((v) => ({ ...v, prompt: event.target.value }))} /></Field>
      <Field label="参考答案或课程材料"><Input.TextArea aria-label="参考答案或课程材料" autoSize={{ minRows: 4, maxRows: 16 }} maxLength={10000} disabled={write.locked} value={form.value.reference} onChange={(event) => form.update((v) => ({ ...v, reference: event.target.value }))} /></Field>
      <Button type="primary" disabled={write.locked} loading={write.phase === "sending"} onClick={() => void submit()}>保存题目版本</Button>
      <FormErrors errors={errors} /><WriteFeedback state={write} retry={() => write.operation.retry()} /></>}
  </section>;
}
