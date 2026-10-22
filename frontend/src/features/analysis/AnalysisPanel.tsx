import { Button, Select, Table, Tag } from "antd";
import type { TableColumnsType } from "antd";
import { useRef, useState } from "react";
import { ApiClient, ApiError } from "../../api/client";
import { usePage, useResource } from "../../api/hooks";
import type { AnalysisCriterion, AnalysisStatus, AnalysisStudent, BatchAnalysis } from "../../api/types";
import { dateTime, EmptyState, ErrorNotice, Loading, Pager, score } from "../../components/common";

const statuses: Record<AnalysisStatus, string> = {
  confirmed: "已纳入统计", pending_review: "待老师复核", failed: "处理失败", in_progress: "初评未完成",
};
const bands: Record<string, string> = {
  below_60: "低于满分的 60%", "60_to_80": "60% 至不足 80%", "80_to_100": "80% 至不足 100%", full: "满分",
};
const percent = (value: string | null) => value === null ? "—" : `${value}%`;

export function AnalysisPanel({ api, batchId, onReview }: {
  api: ApiClient; batchId: string; onReview: (id: string) => void;
}) {
  const report = useResource<BatchAnalysis>(api, `/v1/grading-batches/${batchId}/analysis`);
  const [filter, setFilter] = useState<{ status: AnalysisStatus | "all"; criterionId?: string }>({ status: "confirmed" });
  const studentSection = useRef<HTMLElement>(null);
  const data = report.data;
  const criterion = data?.criteria.find((point) => point.id === filter.criterionId);
  const students = usePage<AnalysisStudent>(api,
    data ? `/v1/grading-batches/${batchId}/analysis/students` : null, {
      basis_hash: data?.basis_hash, status: filter.status === "all" ? undefined : filter.status,
      criterion_id: criterion?.id, below_full_credit: criterion ? "true" : undefined,
    });
  const stale = students.error instanceof ApiError && students.error.code === "analysis_changed";
  const refresh = () => { report.refresh(); students.first(); };
  const inspect = (point: AnalysisCriterion) => {
    setFilter({ status: "confirmed", criterionId: point.id });
    studentSection.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const columns: TableColumnsType<AnalysisStudent> = [
    { title: "学生", key: "student", render: (_, row) => <div>
      <button className="text-link" onClick={() => onReview(row.id)}>{row.student_name}</button>
      <div className="muted small">{row.external_student_id} · 答案 {row.source_revision}</div>
    </div> },
    { title: "统计状态", key: "selection", width: 130, render: (_, row) =>
      <Tag color={row.selection_status === "confirmed" ? "green" : "default"}>{statuses[row.selection_status]}</Tag> },
    { title: "确认总分", dataIndex: "confirmed_score", width: 110, render: score },
    ...(criterion ? [{ title: "本项确认等级与理由", key: "decision", render: (_: unknown, row: AnalysisStudent) => <div>
      <strong>{criterion.levels.find((level) => level.id === row.level_id)?.description || "—"}</strong>
      <p className="small muted analysis-reason">{row.review_reason}</p>
    </div> }] : []),
    { title: "复核版本", key: "revision", width: 130, render: (_, row) => row.review_id
      ? <div>第 {row.review_revision} 次<div className="small muted">{dateTime(row.reviewed_at || undefined)}</div></div> : "—" },
    { title: "", key: "action", width: 140, render: (_, row) =>
      <Button onClick={() => onReview(row.id)}>查看原答案</Button> },
  ];
  if (!data) return <><ErrorNotice error={report.error} retry={report.refresh} />{report.loading && <Loading />}</>;
  const coverage = data.coverage;
  return <div className="analysis-stack">
    <section className="panel analysis-overview" aria-labelledby="analysis-heading">
      <div className="panel-toolbar">
        <div><div className="eyebrow">{data.scope.classroom_name} · 本题本批次</div>
          <h2 id="analysis-heading">班级学情分析</h2></div>
        <Button loading={report.loading || students.loading} onClick={refresh}>刷新分析</Button>
      </div>
      <div className="analysis-content">
        <ErrorNotice error={report.error} retry={refresh} />
        <p className="analysis-description">仅统计本批次所选答案的最新老师确认结果。
          题目版本 {data.scope.question_version} · 细则版本 {data.scope.rubric_version} · 满分 {data.scope.maximum_points} 分。</p>
        <div className="analysis-metrics">
          <div><span>纳入统计</span><strong>{coverage.confirmed}<small> / {coverage.batch_students} 人</small></strong></div>
          <div><span>确认均分</span><strong>{score(data.scores.average_score)}<small>分</small></strong></div>
          <div><span>最低 / 最高</span><strong className="analysis-range">{score(data.scores.minimum_score)} / {score(data.scores.maximum_score)}</strong></div>
          <div><span>确认覆盖率</span><strong>{percent(coverage.confirmed_rate)}</strong></div>
        </div>
        <div className="analysis-coverage">
          <span>作业固定名单 {coverage.assignment_roster} 人</span>
          <span>未选入本批次 {coverage.not_in_batch} 人</span>
          <span>待复核 {coverage.pending_review} 人</span>
          <span>初评未完成 {coverage.in_progress} 人</span>
          <span>处理失败 {coverage.failed} 人</span>
        </div>
        <p className="small muted">未选入本批次不等于未提交；未确认和失败记录不计为零分。
          覆盖率的分母为本批次所选学生，均分和分布的分母为已纳入统计的学生。</p>
        {coverage.simulated_confirmed > 0 && <div className="notice warning" role="note">
          纳入统计的 {coverage.simulated_confirmed} 人使用了模拟初评。确认分数来自老师复核，演示数据不能代表真实班级表现。
        </div>}
        {coverage.confirmed === 0 && <EmptyState text="还没有老师确认的结果。完成复核后，刷新即可查看分项表现。" />}
        <div className="analysis-distribution" aria-label="已确认分数分布">
          {data.scores.distribution.map((band) => <div className="analysis-band" key={band.id}>
            <span>{bands[band.id]}</span><strong>{band.students} 人</strong>
            <div className="analysis-track" aria-hidden="true"><i style={{ width: `${coverage.confirmed ? band.students / coverage.confirmed * 100 : 0}%` }} /></div>
          </div>)}
        </div>
        <p className="small muted">统计读取于 {dateTime(data.snapshot_at)}。
          {data.scores.reviewed_from && <> 所含复核时间：{dateTime(data.scores.reviewed_from)} 至 {dateTime(data.scores.reviewed_to || undefined)}。</>}
          不同题目或评分规则下的分数不直接表示学习进步。</p>
      </div>
    </section>

    <section className="panel" aria-labelledby="analysis-criteria-heading">
      <div className="panel-toolbar"><div><h2 id="analysis-criteria-heading">分项表现与讲评线索</h2>
        <p className="small muted">按未得满分人数排序；具体错因需要结合原答案和老师复核理由。</p></div></div>
      <div className="analysis-criteria">
        {data.criteria.map((point) => <article className="analysis-criterion" key={point.id}>
          <div className="analysis-point-heading"><h3>{point.description}</h3><Tag>满分 {point.maximum_points}</Tag></div>
          <div className="analysis-point-stats"><span>平均 {score(point.average_points)} 分</span>
            <span>得分率 {percent(point.score_rate)}</span>
            <span>未得满分 <strong>{point.below_full_credit_students} / {point.confirmed_students}</strong> 人</span></div>
          <ul className="analysis-levels">{point.levels.map((level) => <li key={level.id}>
            <div><strong>{level.points} 分</strong><span>{level.description}</span></div><b>{level.students} 人</b>
          </li>)}</ul>
          <Button disabled={!point.below_full_credit_students} onClick={() => inspect(point)}>查看本项未得满分答案</Button>
        </article>)}
      </div>
    </section>

    <section className="panel" ref={studentSection} aria-labelledby="analysis-students-heading">
      <div className="panel-toolbar"><div><h2 id="analysis-students-heading">对应答案与复核记录</h2>
        {criterion && <p className="small muted">{criterion.description} · 未得满分</p>}</div>
        <div className="button-row">
          {criterion && <Button onClick={() => setFilter({ status: "confirmed" })}>清除评分点筛选</Button>}
          <Select aria-label="分析答案范围" value={filter.status} className="answer-filter" disabled={!!criterion}
            onChange={(status) => setFilter({ status })} options={[
              { value: "all", label: "全部所选答案" },
              ...Object.entries(statuses).map(([value, label]) => ({ value, label })),
            ]} />
        </div>
      </div>
      <ErrorNotice error={students.error} retry={stale ? refresh : students.first} />
      <Table<AnalysisStudent> rowKey="id" columns={columns}
        dataSource={stale ? [] : students.data?.items || []} loading={students.loading} pagination={false} scroll={{ x: criterion ? 900 : 650 }}
        locale={{ emptyText: <EmptyState text={stale ? "请刷新分析，重新读取对应答案。" : "当前范围没有对应答案。"} /> }} />
      <Pager page={stale ? undefined : students.data} {...students} />
    </section>
  </div>;
}
