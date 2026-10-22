import { Button, Radio, Table, Tag } from "antd";
import type { TableColumnsType } from "antd";
import { useEffect, useState } from "react";
import { ApiClient } from "../../api/client";
import { useOperation, usePage } from "../../api/hooks";
import type { BatchDetail, ExportCreated, ExportMode, ExportRow } from "../../api/types";
import { dateTime, EmptyState, ErrorNotice, Pager } from "../../components/common";

export function ExportPanel({ api, batch }: { api: ApiClient; batch: BatchDetail }) {
  const [mode, setMode] = useState<ExportMode>("working");
  const [downloadError, setDownloadError] = useState<unknown>();
  const [downloading, setDownloading] = useState("");
  const write = useOperation(api, `export:${batch.id}`);
  const page = usePage<ExportRow>(api, "/v1/exports", { batch_id: batch.id });
  const result = write.phase === "succeeded" ? write.result as ExportCreated : undefined;
  useEffect(() => { if (result) page.refresh(); }, [result, page.refresh]);
  const create = async () => {
    try { await write.operation.run<ExportCreated>(`/v1/grading-batches/${batch.id}/exports`, { mode }); }
    catch { /* The persistent operation displays its error and preserves the retry ticket. */ }
  };
  const retry = async () => {
    try { await write.operation.retry<ExportCreated>(); } catch { /* Displayed below. */ }
  };
  const download = async (id: string, format: "csv" | "json") => {
    setDownloading(`${id}:${format}`);
    setDownloadError(undefined);
    try { await api.download(id, format); }
    catch (error) { setDownloadError(error); }
    finally { setDownloading(""); }
  };
  const columns: TableColumnsType<ExportRow> = [
    { title: "快照时间", dataIndex: "created_at", render: dateTime },
    { title: "范围", dataIndex: "mode", render: (value) =>
      <Tag color={value === "confirmed_only" ? "green" : "blue"}>
        {value === "confirmed_only" ? "仅已确认" : "工作汇总"}
      </Tag> },
    { title: "已确认", key: "confirmed", render: (_, row) => row.counts.confirmed ?? 0 },
    { title: "快照内待确认", key: "pending", render: (_, row) =>
      row.mode === "working" ? row.counts.not_confirmed ?? 0 : 0 },
    { title: "未纳入", key: "excluded", render: (_, row) => row.counts.excluded ?? 0 },
    { title: "下载", key: "download", render: (_, row) => <div className="inline-actions">
      {(["csv", "json"] as const).map((format) => <Button key={format} size="small"
        disabled={!!downloading} loading={downloading === `${row.id}:${format}`}
        onClick={() => void download(row.id, format)}>{format.toUpperCase()}</Button>)}
    </div> },
  ];
  return <section className="panel export-panel">
    <div className="panel-toolbar"><div><h2>保存当前复核进度</h2>
      <p className="muted">每次导出保存一个固定快照，之后的改分会体现在新的快照中。</p>
    </div></div>
    <div className="export-create">
      <Radio.Group value={mode} disabled={write.locked} onChange={(event) => setMode(event.target.value)}>
        <Radio value="working">工作汇总 · 包含未确认记录</Radio>
        <Radio value="confirmed_only">仅已确认结果</Radio>
      </Radio.Group>
      <p className="muted small">{mode === "working"
        ? "建议分数与确认分数分列保存；未确认的确认分数留空。"
        : "只导出老师已确认的答案；尚未复核与执行未完成的答案不纳入成绩。"}
        {" "}当前已确认 {batch.counts.confirmed ?? 0} / {batch.counts.total ?? 0} 份。</p>
      <Button type="primary" disabled={write.locked} loading={write.phase === "sending"}
        onClick={() => void create()}>创建导出快照</Button>
    </div>
    <ErrorNotice error={write.error} />
    {write.phase === "uncertain" && <div className="notice warning" role="alert">
      服务器可能已经保存了快照。重试会查询同一次操作，不会另建一份。
      <Button onClick={() => void retry()}>重试同一次导出</Button>
    </div>}
    {result && <div className="notice success" role="status">
      快照已保存 · {dateTime(result.snapshot.snapshot_at)}。可以从下面的记录下载。
    </div>}
    <div className="panel-toolbar"><h2>历史快照</h2>
      <Button loading={page.loading} onClick={page.first}>刷新记录</Button></div>
    <ErrorNotice error={downloadError} />
    <ErrorNotice error={page.error} retry={page.first} />
    <Table<ExportRow> rowKey="id" columns={columns} dataSource={page.data?.items || []}
      loading={page.loading} pagination={false} scroll={{ x: 650 }}
      locale={{ emptyText: page.loading ? "正在读取快照…" : <EmptyState text="还没有导出快照。" /> }} />
    <Pager page={page.data} {...page} />
  </section>;
}
