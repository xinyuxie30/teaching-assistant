import { Button, Select, Table, Tag } from "antd";
import type { TableColumnsType } from "antd";
import { useState } from "react";
import { ApiClient } from "../../api/client";
import { usePage } from "../../api/hooks";
import type { Batch } from "../../api/types";
import { dateTime, EmptyState, ErrorNotice, Pager, PagedSelect, StatusTag } from "../../components/common";

export function BatchList({ api, onOpen, onPrepare }: { api: ApiClient; onOpen: (batchId: string) => void; onPrepare: () => void }) {
  const [courseId, setCourseId] = useState<string>();
  const [assignmentId, setAssignmentId] = useState<string>();
  const [status, setStatus] = useState<string>();
  const page = usePage<Batch>(api, "/v1/grading-batches", {
    course_id: courseId, assignment_id: assignmentId, status,
  }, (data) => data.items.some((batch) => batch.status !== "settled"));
  const columns: TableColumnsType<Batch> = [
    { title: "作业", key: "assignment", render: (_, batch) => <div>
      <button className="text-link assignment-link" onClick={() => onOpen(batch.id)}>{batch.assignment_title}</button>
      <div className="muted small">建立于 {dateTime(batch.created_at)}</div>
    </div> },
    { title: "执行状态", dataIndex: "status", width: 130, render: (value) => <StatusTag status={value} /> },
    { title: "答案", key: "total", width: 90, render: (_, row) => row.counts.total ?? 0 },
    { title: "待复核", key: "pending", width: 100, render: (_, row) =>
      <span className={row.counts.awaiting_review ? "number-accent" : ""}>{row.counts.awaiting_review ?? 0}</span> },
    { title: "已确认", key: "confirmed", width: 100, render: (_, row) => row.counts.confirmed ?? 0 },
    { title: "失败", key: "failed", width: 90, render: (_, row) =>
      row.counts.failed ? <Tag color="error">{row.counts.failed}</Tag> : "—" },
    { title: "", key: "action", width: 100, render: (_, row) =>
      <Button onClick={() => onOpen(row.id)}>打开批次</Button> },
  ];
  return <>
    <div className="page-heading">
      <div><div className="eyebrow">作业 / 批次</div><h1>把每一份答案，认真看清。</h1>
        <p>查看处理进度，集中复核，再生成可追溯的成绩快照。</p></div>
      <div className="button-row"><Button onClick={page.first} loading={page.loading}>刷新批次</Button>
        <Button type="primary" onClick={onPrepare}>准备作业</Button></div>
    </div>
    <section className="panel">
      <div className="filter-bar">
        <PagedSelect api={api} path="/v1/courses" label="课程" value={courseId}
          onChange={(value) => { setCourseId(value); setAssignmentId(undefined); }} />
        <PagedSelect api={api} path={courseId ? `/v1/courses/${courseId}/assignments` : null}
          label="作业" value={assignmentId} onChange={setAssignmentId} />
        <div className="filter-field"><label>执行状态</label>
          <Select aria-label="执行状态" allowClear placeholder="全部状态" value={status} onChange={setStatus}
            options={[{ value: "queued", label: "待处理" }, { value: "running", label: "处理中" },
              { value: "settled", label: "处理结束" }]} />
        </div>
      </div>
      <ErrorNotice error={page.error} retry={page.first} />
      <Table<Batch> rowKey="id" columns={columns} dataSource={page.data?.items || []}
        pagination={false} loading={page.loading} scroll={{ x: 820 }}
        locale={{ emptyText: page.loading ? "正在读取批次…" : <EmptyState text="当前条件下没有批次，可以调整课程、作业或状态筛选。" /> }} />
      <Pager page={page.data} {...page} />
    </section>
    <div className="footnote">初评完成后仍需老师确认。页面会更新正在处理的批次，已结束的列表可手动刷新。</div>
  </>;
}
