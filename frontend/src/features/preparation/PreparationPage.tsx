import { Button, Collapse, Segmented, Table, Tag } from "antd";
import { useState } from "react";
import { ApiClient } from "../../api/client";
import { usePage, useResource } from "../../api/hooks";
import type { Assignment, AssignmentDetail, QuestionDetail, QuestionRow, RubricDetail } from "../../api/types";
import { dateTime, ErrorNotice, Loading, Pager, PagedSelect } from "../../components/common";
import { AnswerImport } from "./AnswerImport";
import { BatchCreator } from "./BatchCreator";
import { AssignmentCreator, QuestionCreator, WorkspaceCreator } from "./CreationForms";
import { RubricPanel } from "./RubricPanel";
import { PreparationSession, usePreparationState } from "./session";

interface Navigation { courseId?: string; assignmentId?: string; questionId?: string; createCourse: boolean }
interface QuestionFlow { tab: "rubric" | "import" | "batch"; rubricId?: string; editQuestion: boolean }

function CourseAssignments({ api, store, courseId, onOpen }: {
  api: ApiClient; store: PreparationSession; courseId: string; onOpen: (id: string) => void;
}) {
  const page = usePage<Assignment>(api, `/v1/courses/${courseId}/assignments`);
  return <div className="prep-stack"><AssignmentCreator key={courseId} api={api} store={store} courseId={courseId} onOpen={onOpen} />
    <section className="panel"><div className="panel-toolbar"><h2>已有作业</h2><Button onClick={page.first} loading={page.loading}>刷新作业</Button></div>
      <ErrorNotice error={page.error} retry={page.first} />
      <Table<Assignment> rowKey="id" dataSource={page.data?.items || []} pagination={false} loading={page.loading} scroll={{ x: 570 }} columns={[
        { title: "作业", dataIndex: "title" }, { title: "班级", dataIndex: "classroom_name" },
        { title: "创建时间", dataIndex: "created_at", render: dateTime },
        { title: "", key: "action", render: (_, row) => <Button onClick={() => onOpen(row.id)}>准备题目与答案</Button> },
      ]} locale={{ emptyText: page.loading ? "正在读取作业…" : "还没有作业，可先在上方创建。" }} />
      <Pager page={page.data} {...page} /></section>
  </div>;
}

function AssignmentQuestions({ api, store, assignment, onOpen }: {
  api: ApiClient; store: PreparationSession; assignment: AssignmentDetail; onOpen: (id: string) => void;
}) {
  const page = usePage<QuestionRow>(api, `/v1/assignments/${assignment.id}/question-versions`);
  return <div className="prep-stack"><QuestionCreator api={api} store={store} assignmentId={assignment.id} onOpen={onOpen} />
    <section className="panel"><div className="panel-toolbar"><h2>已有题目版本</h2><Button onClick={page.first} loading={page.loading}>刷新题目</Button></div>
      <ErrorNotice error={page.error} retry={page.first} />
      <Table<QuestionRow> rowKey="id" dataSource={page.data?.items || []} pagination={false} loading={page.loading} scroll={{ x: 650 }} columns={[
        { title: "题号 / 版本", key: "question", render: (_, row) => <>{row.question_key} · 第 {row.version} 版</> },
        { title: "题干", dataIndex: "prompt_preview", ellipsis: true }, { title: "满分", dataIndex: "maximum_points" },
        { title: "", key: "action", render: (_, row) => <Button onClick={() => onOpen(row.id)}>细则与答案</Button> },
      ]} locale={{ emptyText: page.loading ? "正在读取题目…" : "还没有题目，请先保存题干与参考材料。" }} />
      <Pager page={page.data} {...page} /></section>
  </div>;
}

function QuestionPreparation({ api, store, assignment, question, onQuestion, onBatch }: {
  api: ApiClient; store: PreparationSession; assignment: AssignmentDetail; question: QuestionDetail;
  onQuestion: (id: string) => void; onBatch: (id: string) => void;
}) {
  const flow = usePreparationState<QuestionFlow>(store, `question-flow:${question.id}`, () => ({ tab: "rubric", editQuestion: false }));
  const rubric = useResource<RubricDetail>(api, flow.value.rubricId ? `/v1/rubric-versions/${flow.value.rubricId}` : null);
  const ready = rubric.data?.question_version_id === question.id;
  return <div className="prep-stack"><section className="panel prep-card">
    <div className="panel-toolbar prep-toolbar"><div><div className="eyebrow">固定题目版本</div><h2>题号 {question.question_key} · 第 {question.version} 版</h2></div>
      <Tag>{question.maximum_points} 分</Tag></div>
    <p className="plain-text">{question.prompt}</p>
    <Collapse ghost items={[{ key: "reference", label: "参考答案或课程材料", children: <p className="plain-text">{question.reference}</p> }]} />
    <Button onClick={() => flow.update((value) => ({ ...value, editQuestion: !value.editQuestion }), false)}>
      {flow.value.editQuestion ? "收起新版本编辑" : "修改题目，另存新版本"}</Button>
    {flow.value.rubricId && <p className="muted small prep-hint">{ready ? `本次已选用细则第 ${rubric.data!.version} 版。` : "正在读取选定的固定细则。"}切换规则需在细则页面明确选择。</p>}
  </section>
    {flow.value.editQuestion && <QuestionCreator api={api} store={store} assignmentId={assignment.id} source={question} onOpen={onQuestion} />}
    <div className="section-toolbar"><Segmented value={flow.value.tab}
      options={[{ value: "rubric", label: "1. 确认评分细则" }, { value: "import", label: "2. 导入答案", disabled: !ready }, { value: "batch", label: "3. 创建批次", disabled: !ready }]}
      onChange={(value) => flow.update((current) => ({ ...current, tab: value as QuestionFlow["tab"] }), false)} /></div>
    <ErrorNotice error={rubric.error} retry={rubric.refresh} />
    {flow.value.tab === "rubric" ? <RubricPanel api={api} store={store} question={question} selectedId={flow.value.rubricId}
      onSelected={(id) => flow.update((value) => ({ ...value, rubricId: id, tab: "import" }), false)} />
      : !ready ? <section className="panel prep-card">{rubric.loading ? <Loading /> : <><p>请先在细则页面选择当前题目的一份已确认版本。</p>
        <Button onClick={() => flow.update((value) => ({ ...value, tab: "rubric" }), false)}>查看评分细则</Button></>}</section>
        : flow.value.tab === "import" ? <AnswerImport api={api} store={store} assignment={assignment} question={question}
          onBatch={() => flow.update((value) => ({ ...value, tab: "batch" }), false)} />
          : <BatchCreator key={rubric.data!.id} api={api} store={store} assignment={assignment} question={question} rubric={rubric.data!} onOpen={onBatch} />}
  </div>;
}

export function PreparationPage({ api, store, onBatch }: {
  api: ApiClient; store: PreparationSession; onBatch: (id: string) => void;
}) {
  const nav = usePreparationState<Navigation>(store, "navigation", () => ({ createCourse: false }));
  const assignment = useResource<AssignmentDetail>(api, nav.value.assignmentId ? `/v1/assignments/${nav.value.assignmentId}` : null);
  const question = useResource<QuestionDetail>(api, nav.value.questionId ? `/v1/question-versions/${nav.value.questionId}` : null);
  const [courseRefresh, setCourseRefresh] = useState(0);
  const openAssignment = (id: string) => nav.update((value) => ({ ...value, assignmentId: id, questionId: undefined }), false);
  const openQuestion = (id: string) => nav.update((value) => ({ ...value, questionId: id }), false);
  if (!nav.value.assignmentId) return <>
    <div className="page-heading"><div><div className="eyebrow">作业准备</div><h1>从题目到一次完整初评。</h1><p>选择课程，确认评分标准，再导入答案和创建批次。</p></div>
      <Button onClick={() => nav.update((value) => ({ ...value, createCourse: !value.createCourse }), false)}>{nav.value.createCourse ? "收起课程录入" : "新建课程与班级"}</Button></div>
    <div className="prep-stack">{nav.value.createCourse && <WorkspaceCreator api={api} store={store} onOpen={(courseId) => {
      nav.set({ courseId, createCourse: false }, false); setCourseRefresh((value) => value + 1);
    }} />}
      <section className="panel prep-card"><PagedSelect key={courseRefresh} api={api} path="/v1/courses" label="课程" placeholder="选择课程，查看或创建作业"
        value={nav.value.courseId} onChange={(courseId) => nav.set({ courseId, createCourse: nav.value.createCourse }, false)} />
        {!nav.value.courseId && <p className="muted prep-hint">首次使用可先建立课程、班级和名单，再创建作业。</p>}</section>
      {nav.value.courseId && <CourseAssignments key={nav.value.courseId} api={api} store={store} courseId={nav.value.courseId} onOpen={openAssignment} />}
    </div>
  </>;
  return <>
    <button className="back-link" onClick={() => nav.value.questionId ? nav.update((value) => ({ ...value, questionId: undefined }), false)
      : nav.update((value) => ({ ...value, assignmentId: undefined }), false)}>← {nav.value.questionId ? "返回作业题目" : "返回课程与作业"}</button>
    <ErrorNotice error={assignment.error} retry={assignment.refresh} />
    {!assignment.data ? assignment.loading && <Loading /> : <>
      <div className="page-heading"><div><div className="eyebrow">{assignment.data.course_name} · {assignment.data.classroom_name}</div>
        <h1>{assignment.data.title}</h1><p>固定名单 {assignment.data.roster.length} 人 · 创建于 {dateTime(assignment.data.created_at)}</p></div></div>
      <Collapse className="roster-collapse" items={[{ key: "roster", label: "查看这份作业的固定名单", children:
        <div className="roster-tags">{assignment.data.roster.map((student) => <Tag key={student.id}>{student.external_id} · {student.name}</Tag>)}</div> }]} />
      {!nav.value.questionId ? <AssignmentQuestions api={api} store={store} assignment={assignment.data} onOpen={openQuestion} /> : <>
        <ErrorNotice error={question.error} retry={question.refresh} />
        {question.data?.assignment_id === assignment.data.id
          ? <QuestionPreparation key={question.data.id} api={api} store={store} assignment={assignment.data} question={question.data} onQuestion={openQuestion} onBatch={onBatch} />
          : question.loading ? <Loading /> : question.data && <div className="notice notice-error">题目不属于当前作业，请返回重新选择。</div>}
      </>}
    </>}
  </>;
}
