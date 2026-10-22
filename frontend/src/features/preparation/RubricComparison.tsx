import { Button, Tag } from "antd";
import { useState } from "react";
import type { Criterion, Rubric, RubricChangeStatus, RubricRevisionComparison, RubricRowChange } from "../../api/types";

const labels: Record<RubricChangeStatus, string> = { added: "新增", removed: "移除", modified: "修改", unchanged: "未变化" };
const colors: Record<RubricChangeStatus, string> = { added: "green", removed: "red", modified: "orange", unchanged: "default" };
const fields: Record<string, string> = {
  description: "描述", maximum_points: "满分", semantic_version: "规则版本", levels: "等级",
  points: "分值", ordinal: "等级顺序", global_instructions: "整体评分说明",
  selection_policy: "等级选择规则", rounding_policy: "总分取整规则",
};

function CriterionSide({ title, value, position, change }: {
  title: string; value?: Criterion; position: number;
  change: RubricRowChange & { levels: RubricRowChange[] };
}) {
  return <div className="rubric-comparison-side">
    <h4>{title}{value ? ` · 评分点 ${position + 1}` : ""}</h4>
    {!value ? <p className="muted">{title === "原稿" ? "原稿没有此评分点" : "新版已移除此评分点"}</p> : <>
      <p className="plain-text">{value.description}</p>
      <p className={change.changed_fields.includes("maximum_points") ? "rubric-changed-value" : ""}>满分 {value.maximum_points} 分</p>
      {change.changed_fields.includes("semantic_version") && <p className="muted small">规则版本 {value.semantic_version}</p>}
      <ol className="rubric-comparison-levels">
        {[...value.levels].sort((a, b) => a.ordinal - b.ordinal).map((level) => {
          const delta = change.levels.find((item) => item.id === level.id);
          return <li key={level.id} className={delta?.status !== "unchanged" ? "rubric-level-changed" : ""}>
            <div className="rubric-comparison-level-title"><strong>{level.points} 分</strong>
              {delta && delta.status !== "unchanged" && <Tag color={colors[delta.status]}>{labels[delta.status]}</Tag>}
              {delta?.changed_fields.length ? <span className="muted small">{delta.changed_fields.map((key) => fields[key] || key).join("、")}变化</span> : null}
            </div><p className="plain-text">{level.description}</p>
          </li>;
        })}
      </ol>
    </>}
  </div>;
}

export function RubricComparison({ comparison, body }: { comparison: RubricRevisionComparison; body: Rubric }) {
  const [showUnchanged, setShowUnchanged] = useState(false);
  const { difference, source_body: source } = comparison;
  const visible = difference.criteria.filter((change) => showUnchanged || change.status !== "unchanged");
  return <section className="rubric-comparison" aria-label="修订差异">
    <h3>与原稿的修订对照</h3>
    <p className="muted small">对照起草时保存的原稿快照。评分点和等级按相同标识匹配，标识变化显示为移除与新增。</p>
    <p className="plain-text rubric-comparison-feedback">修改意见：{comparison.feedback}</p>
    <div className="button-row rubric-comparison-summary">
      {(Object.keys(labels) as RubricChangeStatus[]).map((status) => <Tag key={status} color={colors[status]}>
        {labels[status]} {difference.summary[status]} 个评分点
      </Tag>)}
    </div>
    {!difference.has_changes && <p className="rubric-comparison-empty">新版与原稿内容一致，请检查是否满足修改意见。</p>}
    {difference.criterion_order_changed && <p className="rubric-comparison-empty">评分点排列顺序已调整，可展开未变化的评分点核对前后位置。</p>}
    {difference.global_fields.map((field) => <section key={field} className="rubric-comparison-change">
      <h4>{fields[field]}已修改</h4>
      <div className="rubric-comparison-pair">
        <div className="rubric-comparison-side"><h4>原稿</h4><p className="plain-text">{source[field]}</p></div>
        <div className="rubric-comparison-side"><h4>新版</h4><p className="plain-text">{body[field]}</p></div>
      </div>
    </section>)}
    {difference.summary.unchanged > 0 && <Button size="small" onClick={() => setShowUnchanged((value) => !value)}>
      {showUnchanged ? "隐藏未变化评分点" : `显示 ${difference.summary.unchanged} 个未变化评分点`}
    </Button>}
    {visible.map((change) => {
      const beforeIndex = source.criteria.findIndex((value) => value.id === change.id);
      const afterIndex = body.criteria.findIndex((value) => value.id === change.id);
      return <section key={change.id} className="rubric-comparison-change" aria-label={`${labels[change.status]}评分点`}>
        <div className="button-row"><Tag color={colors[change.status]}>{labels[change.status]}评分点</Tag>
          {change.changed_fields.length > 0 && <span className="muted small">{change.changed_fields.map((field) => fields[field] || field).join("、")}变化</span>}
        </div>
        <div className="rubric-comparison-pair">
          <CriterionSide title="原稿" value={source.criteria[beforeIndex]} position={beforeIndex} change={change} />
          <CriterionSide title="新版" value={body.criteria[afterIndex]} position={afterIndex} change={change} />
        </div>
      </section>;
    })}
    <p className="muted small">以上展示文字、分值和结构的变化，教学要求是否合适仍由你判断。</p>
  </section>;
}
