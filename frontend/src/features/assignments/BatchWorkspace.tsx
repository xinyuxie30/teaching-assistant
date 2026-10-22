import { Button, Segmented, Select, Table, Tag } from "antd";
import type { TableColumnsType } from "antd";
import { useEffect, useState } from "react";
import { ApiClient } from "../../api/client";
import { usePage, useResource } from "../../api/hooks";
import type { BatchDetail, ItemRow } from "../../api/types";
import { dateTime, EmptyState, ErrorNotice, Loading, Pager, score, StatusTag } from "../../components/common";
import { ExportPanel } from "../exports/ExportPanel";

export function BatchWorkspace({ api, batchId, onBack, onReview }: {
  api: ApiClient; batchId: string; onBack: () => void; onReview: (id: string) => void;
}) {
  const [tab, setTab] = useState("answers");
  const [filter, setFilter] = useState("ready");
  const batch = useResource<BatchDetail>(api, `/v1/grading-batches/${batchId}`, {}, (data) => data.status !== "settled");
  const filters = {
    batch_id: batchId,
    status: filter === "ready" ? "succeeded" : filter === "failed" ? "failed" : undefined,
    review_status: filter === "ready" ? "pending" : filter === "confirmed" ? "confirmed" : undefined,
  };
  const page = usePage<ItemRow>(api, tab === "answers" ? "/v1/grading-items" : null, filters,
    () => batch.data?.status !== "settled");
  useEffect(() => { page.refresh(); }, [batch.data?.status, page.refresh]);
  const columns: TableColumnsType<ItemRow> = [
    { title: "学生", key: "student", render: (_, row) => <div>
      <button className="text-link" onClick={() => onReview(row.id)}>{row.student_name}</button>
      <div className="muted small">{row.external_student_id}</div>
    </div> },
    { title: "执行", dataIndex: "status", width: 120, render: (value) => <StatusTag status={value} /> },
    { title: "复核", key: "review", width: 120, render: (_, row) =>
      row.review_revision ? <StatusTag status="confirmed" /> : row.needs_review
        ? <StatusTag status="awaiting_review" /> : <span className="muted">尚未就绪</span> },
    { title: "建议分数", dataIndex: "suggested_score", width: 115, render: score },
    { title: "确认分数", dataIndex: "confirmed_score", width: 115, render: (value) =>
      <strong>{score(value)}</strong> },
    { title: "评估来源", key: "source", width: 110, render: (_, row) =>
      <Tag color={row.simulation ? "orange" : "default"}>{row.simulation ? "模拟评估" : "模型初评"}</Tag> },
    { title: "", key: "action", width: 110, render: (_, row) =>
      <Button type={row.needs_review ? "primary" : "default"} onClick={() => onReview(row.id)}>
        {row.needs_review ? "开始复核" : "查看答案"}
      </Button> },
  ];
  const data = batch.data;
  if (!data) return <><Button onClick={onBack}>← 返回批次</Button>
    <ErrorNotice error={batch.error} retry={batch.refresh} />{batch.loading && <Loading />}</>;
  const counts = data.counts;
  return <>
    <button className="back-link" onClick={onBack}>← 返回批次列表</button>
    <div className="page-heading">
      <div><div className="eyebrow">作业批次 · {dateTime(data.created_at)}</div>
        <h1>{data.assignment.title}</h1>
        <p>题目 {data.question.question_key} · 题目版本 {data.question.version} · 评分细则版本 {data.rubric_version}</p>
      </div><StatusTag status={data.status} />
    </div>
    <ErrorNotice error={batch.error} retry={batch.refresh} />
    <div className="stat-grid">
      {[["答案总数", counts.total ?? 0], ["待老师复核", counts.awaiting_review ?? 0],
        ["已确认", counts.confirmed ?? 0], ["处理失败", counts.failed ?? 0]].map(([label, value], index) =>
        <div key={label} className={`stat stat-${index}`}><span>{label}</span><strong>{value}</strong><small>份</small></div>)}
    </div>
    <div className="section-toolbar">
      <Segmented value={tab} onChange={(value) => setTab(String(value))}
        options={[{ value: "answers", label: "答案与复核" }, { value: "exports", label: "导出快照" }]} />
      <span className="muted small">批次 {batchId.slice(0, 8)}</span>
    </div>
    {tab === "exports" ? <ExportPanel api={api} batch={data} /> : <section className="panel">
      <div className="panel-toolbar">
        <Select aria-label="答案筛选" value={filter} onChange={setFilter} className="answer-filter"
          options={[{ value: "ready", label: "待老师复核" }, { value: "all", label: "全部答案" },
            { value: "confirmed", label: "已确认" }, { value: "failed", label: "处理失败" }]} />
        <Button onClick={() => { batch.refresh(); page.first(); }} loading={page.loading}>刷新答案</Button>
      </div>
      <ErrorNotice error={page.error} retry={page.first} />
      <Table<ItemRow> rowKey="id" columns={columns} dataSource={page.data?.items || []}
        pagination={false} loading={page.loading} scroll={{ x: 850 }}
        locale={{ emptyText: page.loading ? "正在读取答案…" : <EmptyState text={filter === "ready" ? "当前没有待复核的完整初评，可切换到全部答案。" : "当前筛选下没有答案。"} /> }} />
      <Pager page={page.data} {...page} />
    </section>}
  </>;
}
