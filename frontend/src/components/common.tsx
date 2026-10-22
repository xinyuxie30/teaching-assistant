import { Button, Empty, Select, Spin, Tag } from "antd";
import { useEffect, useState } from "react";
import { ApiClient, errorMessage } from "../api/client";
import { useResource } from "../api/hooks";
import type { Page } from "../api/types";

export function ErrorNotice({ error, retry }: { error: unknown; retry?: () => void }) {
  if (!error) return null;
  return <div className="notice notice-error" role="alert">
    <span>{errorMessage(error)}</span>
    {retry && <Button size="small" onClick={retry}>重新读取</Button>}
  </div>;
}
export function Loading() {
  return <div className="loading"><Spin /><span>正在读取记录…</span></div>;
}
export function EmptyState({ text }: { text: string }) {
  return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={text} />;
}
export function score(value: string | null | undefined): string {
  return value === null || value === undefined ? "—" : value;
}
export function dateTime(value: string | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}
export function StatusTag({ status }: { status: string }) {
  const labels: Record<string, [string, string]> = {
    queued: ["待处理", "default"], pending: ["待处理", "default"],
    running: ["初评中", "processing"], retry_wait: ["等待重试", "warning"],
    succeeded: ["初评完成", "success"], failed: ["处理失败", "error"],
    settled: ["处理结束", "default"], confirmed: ["已确认", "success"],
    awaiting_review: ["待复核", "warning"],
  };
  const [label, color] = labels[status] || [status, "default"];
  return <Tag color={color}>{label}</Tag>;
}
export function Pager({ page, first, previous, next, pageNumber, loading }: {
  page?: Page<unknown>; first: () => void; previous: () => void; next: () => void;
  pageNumber: number; loading: boolean;
}) {
  return <div className="pager">
    <span className="muted">共 {page?.total ?? "—"} 条 · 第 {pageNumber} 页</span>
    <div className="button-row">
      <Button disabled={loading || pageNumber === 1} onClick={first}>首页</Button>
      <Button disabled={loading || pageNumber === 1} onClick={previous}>上一页</Button>
      <Button disabled={loading || !page?.has_more} onClick={next}>下一页</Button>
    </div>
  </div>;
}

/** Explicit "load more" avoids silently restricting course/assignment selection to page one. */
export function PagedSelect({ api, path, value, label, onChange, disabled, placeholder }: {
  api: ApiClient; path: string | null; value?: string; label: string;
  onChange: (value: string | undefined) => void;
  disabled?: boolean; placeholder?: string;
}) {
  type OptionRecord = { id: string; name?: string; title?: string };
  const [position, setPosition] = useState<{ path: string | null; cursor?: string }>({ path });
  useEffect(() => {
    setPosition((previous) => previous.path === path ? previous : { path });
  }, [path]);
  const [options, setOptions] = useState<{ path: string | null; items: OptionRecord[] }>({ path, items: [] });
  const cursor = position.path === path ? position.cursor : undefined;
  const resource = useResource<Page<OptionRecord>>(api, path, { limit: 100, cursor });
  useEffect(() => {
    if (!resource.data) return;
    const items = resource.data.items;
    setOptions((previous) => {
      const combined = previous.path === path && cursor ? [...previous.items, ...items] : items;
      return { path, items: [...new Map(combined.map((item) => [item.id, item])).values()] };
    });
  }, [resource.data, path, cursor]);
  const items = options.path === path ? options.items : [];
  return <div className="filter-field">
    <label>{label}</label>
    <Select
      aria-label={label} allowClear showSearch optionFilterProp="label"
      placeholder={placeholder || `全部${label}`} disabled={!path || disabled} loading={resource.loading}
      value={value} onChange={onChange}
      options={items.map((item) => ({ value: item.id, label: item.name || item.title || item.id }))}
      popupRender={(menu) => <>
        {menu}
        {resource.error && <div className="select-footer"><Button onClick={resource.refresh}>读取失败，重试</Button></div>}
        {resource.data?.has_more && <div className="select-footer">
          <Button type="link" loading={resource.loading} onMouseDown={(event) => event.preventDefault()}
            onClick={() => setPosition({ path, cursor: resource.data?.next_cursor || undefined })}>
            加载更多{label}
          </Button>
        </div>}
      </>}
    />
  </div>;
}
