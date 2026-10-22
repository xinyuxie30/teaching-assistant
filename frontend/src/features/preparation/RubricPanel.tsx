import { Button, Table, Tag } from "antd";
import { useState } from "react";
import { ApiClient } from "../../api/client";
import { useOperation, usePage, useResource } from "../../api/hooks";
import type { DraftDetail, DraftRow, QuestionDetail, Rubric, RubricDetail, RubricRow, VersionReceipt } from "../../api/types";
import { dateTime, ErrorNotice, Loading, Pager } from "../../components/common";
import { useAccepted, WriteFeedback } from "./common";
import { RubricDocument, RubricEditor } from "./RubricEditor";
import { PreparationSession, usePreparationState } from "./session";

type Selection = { kind: "editor"; source?: { id: string; body: Rubric } }
  | { kind: "draft" | "version"; id: string };

function SavedRule({ api, store, selection, question, onSelected, onEdit }: {
  api: ApiClient; store: PreparationSession; selection: { kind: "draft" | "version"; id: string };
  question: QuestionDetail; onSelected: (id: string) => void; onEdit: (source: { id: string; body: Rubric }) => void;
}) {
  const resource = useResource<DraftDetail | RubricDetail>(api, `/v1/rubric-${selection.kind === "draft" ? "drafts" : "versions"}/${selection.id}`);
  const write = useOperation(api, `prepare:confirm:${selection.id}`);
  const confirmation = usePreparationState<VersionReceipt | undefined>(store, `confirmed:${selection.id}`, () => undefined);
  useAccepted<VersionReceipt>(write, (receipt) => { confirmation.set(receipt, false); resource.refresh(); });
  const data = resource.data;
  const frozenId = selection.kind === "version" ? selection.id : confirmation.value?.id
    || (data && "confirmed_version_id" in data ? data.confirmed_version_id : undefined);
  const confirm = async () => {
    if (!data || !("revision" in data)) return;
    try { await write.operation.run(`/v1/rubric-drafts/${selection.id}/confirm`, { expected_revision: data.revision }); }
    catch { /* The same ticket is retried through WriteFeedback. */ }
  };
  if (!data) return <section className="panel prep-card"><ErrorNotice error={resource.error} retry={resource.refresh} />{resource.loading && <Loading />}</section>;
  if (data.question_version_id !== question.id) return <div className="notice notice-error">这份细则不属于当前题目版本，请重新选择。</div>;
  return <section className="panel prep-card"><div className="panel-toolbar prep-toolbar">
    <h2>{"version" in data ? `已确认细则 · 第 ${data.version} 版` : frozenId ? "草稿已确认" : "检查并确认评分细则"}</h2>
    <Tag color={frozenId ? "green" : "orange"}>{frozenId ? "固定版本" : "尚未确认"}</Tag></div>
    <ErrorNotice error={resource.error} retry={resource.refresh} /><RubricDocument body={data.body} />
    <p className="muted small">请核对每个评分点的描述及分值。确认后保持固定，修改需创建新的版本。</p>
    <div className="button-row">{frozenId
      ? <Button type="primary" onClick={() => onSelected(frozenId)}>使用这份固定细则，导入答案</Button>
      : <Button type="primary" disabled={write.locked} loading={write.phase === "sending"} onClick={() => void confirm()}>确认并固定评分细则</Button>}
      <Button disabled={write.locked} onClick={() => onEdit({ id: data.id, body: data.body })}>以此起草新版本</Button></div>
    <WriteFeedback state={write} retry={() => write.operation.retry()} />
  </section>;
}

export function RubricPanel({ api, store, question, selectedId, onSelected }: {
  api: ApiClient; store: PreparationSession; question: QuestionDetail; selectedId?: string; onSelected: (id: string) => void;
}) {
  const view = usePreparationState<Selection | undefined>(store, `rubric-view:${question.id}`, () => undefined);
  const [listTab, setListTab] = useState<"versions" | "drafts">("versions");
  const versions = usePage<RubricRow>(api, `/v1/question-versions/${question.id}/rubric-versions`);
  const drafts = usePage<DraftRow>(api, `/v1/question-versions/${question.id}/rubric-drafts`);
  return <div className="prep-stack"><section className="panel prep-card">
    <div className="panel-toolbar prep-toolbar"><div><h2>评分细则版本</h2><p className="muted small">先打开并检查一份固定规则，再明确选择它用于本次导入与批次。</p></div>
      <Button onClick={() => view.set({ kind: "editor" }, false)}>起草新细则</Button></div>
    <div className="button-row prep-list-tabs"><Button type={listTab === "versions" ? "primary" : "default"} onClick={() => { setListTab("versions"); versions.first(); }}>已确认版本</Button>
      <Button type={listTab === "drafts" ? "primary" : "default"} onClick={() => { setListTab("drafts"); drafts.first(); }}>已保存草稿</Button>
      <Button onClick={() => { versions.first(); drafts.first(); }}>刷新</Button></div>
    {listTab === "versions" ? <><ErrorNotice error={versions.error} retry={versions.first} />
      <Table<RubricRow> rowKey="id" pagination={false} loading={versions.loading} dataSource={versions.data?.items || []} columns={[
        { title: "版本", key: "version", render: (_, row) => <>第 {row.version} 版 {row.id === selectedId && <Tag color="green">当前选用</Tag>}</> },
        { title: "评分点", dataIndex: "criterion_count" }, { title: "确认时间", dataIndex: "created_at", render: dateTime },
        { title: "", key: "action", render: (_, row) => <Button onClick={() => view.set({ kind: "version", id: row.id }, false)}>查看细则</Button> },
      ]} scroll={{ x: 540 }} locale={{ emptyText: versions.loading ? "正在读取…" : "还没有已确认版本，请先起草并确认细则。" }} />
      <Pager page={versions.data} {...versions} /></> : <><ErrorNotice error={drafts.error} retry={drafts.first} />
      <Table<DraftRow> rowKey="id" pagination={false} loading={drafts.loading} dataSource={drafts.data?.items || []} columns={[
        { title: "保存时间", dataIndex: "created_at", render: dateTime },
        { title: "状态", key: "status", render: (_, row) => <Tag>{row.confirmed_version_id ? "已确认" : "待检查"}</Tag> },
        { title: "", key: "action", render: (_, row) => <Button onClick={() => view.set({ kind: "draft", id: row.id }, false)}>打开草稿</Button> },
      ]} locale={{ emptyText: drafts.loading ? "正在读取…" : "还没有已保存草稿。" }} />
      <Pager page={drafts.data} {...drafts} /></>}
  </section>
    {view.value?.kind === "editor" && <RubricEditor key={`${question.id}:${view.value.source?.id || "new"}`}
      api={api} store={store} question={question} source={view.value.source}
      onSaved={(id) => { view.set({ kind: "draft", id }, false); drafts.first(); }} />}
    {view.value && view.value.kind !== "editor" && <SavedRule key={`${view.value.kind}:${view.value.id}`} api={api} store={store}
      question={question} selection={view.value} onSelected={onSelected} onEdit={(source) => view.set({ kind: "editor", source }, false)} />}
  </div>;
}
