import { App, Button, Collapse, Input, InputNumber, Modal, Radio, Select, Tag } from "antd";
import { useEffect, useRef, useState } from "react";
import { ApiClient, ApiError } from "../../api/client";
import { useOperation, useResource } from "../../api/hooks";
import type { Citation, EvidenceSupport, ItemDetail, RetryPayload, ReviewReceipt } from "../../api/types";
import { dateTime, ErrorNotice, Loading, score, StatusTag } from "../../components/common";
import { draftErrors, draftScore, initialDraft, reviewPayload } from "./draft";
import type { DraftDecision, ReviewDraft } from "./draft";
import { citationMatches, selectionCitation } from "./evidence";

const supportLabels: Record<EvidenceSupport, string> = {
  supported: "原文支持该等级",
  absence_confirmed: "已检查，相关表达缺失",
  teacher_judgment: "结合完整答案人工判断",
};
const issueLabels: Record<string, string> = {
  requires_teacher_review: "需老师复核",
  simulation_not_semantic_grading: "模拟评估",
  abstained: "模型未选择等级",
  no_citation: "尚无原文引用",
  evidence_pending_manual_selection: "需老师选取引用",
};
export interface SavedDraft { draft: ReviewDraft; baseline: string }

export function ReviewPage({ api, itemId, onBack, onDirtyChange, drafts }: {
  api: ApiClient; itemId: string; onBack: () => void; onDirtyChange: (dirty: boolean) => void;
  drafts: Map<string, SavedDraft>;
}) {
  const { modal, message } = App.useApp();
  const resource = useResource<ItemDetail>(api, `/v1/grading-items/${itemId}`, {},
    (item) => !["succeeded", "failed"].includes(item.status));
  const write = useOperation(api, `review:${itemId}`);
  const retryWrite = useOperation(api, `retry:${itemId}`);
  const [saved, setSaved] = useState<SavedDraft | undefined>(() => drafts.get(itemId));
  const [active, setActive] = useState("");
  const [selection, setSelection] = useState<Citation | null>(null);
  const [selectionError, setSelectionError] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [retryOpen, setRetryOpen] = useState(false);
  const [retryForm, setRetryForm] = useState<RetryPayload>({
    reason: "", additional_attempts: 1, additional_calls: 2,
  });
  const answerRef = useRef<HTMLDivElement>(null);
  const acceptedReceipt = useRef<string | undefined>(undefined);
  const item = resource.data;
  const locked = write.locked || retryWrite.locked;
  const draft = saved?.draft;
  const dirty = !!saved && JSON.stringify(saved.draft) !== saved.baseline;
  const conflict = !!item && !!draft && item.review_revision > draft.expected_revision;

  useEffect(() => {
    if (!item || item.status !== "succeeded") return;
    setActive((value) => value || item.rubric.body.criteria[0]?.id || "");
    setSaved((previous) => {
      if (previous) return previous;
      const initial = initialDraft(item);
      return { draft: initial, baseline: JSON.stringify(initial) };
    });
  }, [item]);
  useEffect(() => {
    if (saved) drafts.set(itemId, saved);
    onDirtyChange(dirty || locked);
  }, [saved, dirty, locked, drafts, itemId, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);

  useEffect(() => {
    if (write.phase !== "succeeded" || !write.result || !write.ticket) return;
    const receipt = write.result as ReviewReceipt;
    if (acceptedReceipt.current === receipt.id) return;
    acceptedReceipt.current = receipt.id;
    // The operation can finish while this page is unmounted. Consume its receipt
    // on return too, but never overwrite edits made after that accepted revision.
    const previous = drafts.get(itemId);
    if (!previous || previous.draft.expected_revision < receipt.revision) {
      const payload = JSON.parse(write.ticket.body) as ReviewDraft;
      const accepted = { ...payload, expected_revision: receipt.revision };
      const next = { draft: accepted, baseline: JSON.stringify(accepted) };
      drafts.set(itemId, next);
      setSaved(next);
      setErrors([]);
      setSelection(null);
      void message.success(`复核已保存，确认分数 ${receipt.confirmed_score} 分`);
    }
    resource.refresh();
  }, [write.phase, write.result, write.ticket, drafts, itemId, resource.refresh, message]);

  const replaceWith = (latest: ItemDetail) => {
    const initial = initialDraft(latest);
    setSaved({ draft: initial, baseline: JSON.stringify(initial) });
    setErrors([]);
    setSelection(null);
    drafts.delete(itemId);
  };
  const update = (id: string, change: Partial<DraftDecision>) => {
    setSaved((previous) => previous && ({
      ...previous,
      draft: { ...previous.draft, decisions: previous.draft.decisions.map((decision) =>
        decision.criterion_id === id ? { ...decision, ...change } : decision) },
    }));
    setErrors([]);
  };
  const capture = () => {
    if (!answerRef.current || !item) return;
    try {
      setSelection(selectionCitation(answerRef.current, item.answer.text, window.getSelection()));
      setSelectionError("");
    } catch (error) {
      setSelection(null);
      setSelectionError(error instanceof Error ? error.message : "无法读取选区。");
    }
  };
  const addCitation = () => {
    const decision = draft?.decisions.find((value) => value.criterion_id === active);
    if (!selection || !decision || locked) return;
    if (decision.citations.length >= 20) {
      setSelectionError("每个评分点最多保留 20 处引用。");
      return;
    }
    if (!decision.citations.some((value) => value.start === selection.start && value.end === selection.end)) {
      update(active, { citations: [...decision.citations, selection], evidence_support: "supported" });
    }
    setSelection(null);
    window.getSelection()?.removeAllRanges();
  };
  const locateCitation = (citation: Citation) => {
    const container = answerRef.current;
    if (!container || !item || !citationMatches(item.answer.text, citation)) return;
    const node = container.firstChild;
    if (!node || node.nodeType !== Node.TEXT_NODE || node.nodeValue !== item.answer.text) return;
    const characters = Array.from(item.answer.text);
    const range = document.createRange();
    range.setStart(node, characters.slice(0, citation.start).join("").length);
    range.setEnd(node, characters.slice(0, citation.end).join("").length);
    const selected = window.getSelection();
    selected?.removeAllRanges();
    selected?.addRange(range);
    container.scrollIntoView({ block: "center", behavior: "smooth" });
  };
  const submit = async () => {
    if (!item || !draft) return;
    const problems = draftErrors(item, draft);
    setErrors(problems);
    if (problems.length || conflict) return;
    try {
      await write.operation.run<ReviewReceipt>(`/v1/grading-items/${itemId}/reviews`,
        reviewPayload(item, draft));
    } catch (error) {
      if (error instanceof ApiError && error.code === "revision_conflict") resource.refresh();
    }
  };
  const retryReview = async () => {
    try { await write.operation.retry<ReviewReceipt>(); }
    catch (error) {
      if (error instanceof ApiError && error.code === "revision_conflict") resource.refresh();
    }
  };
  const retryItem = async (sameOperation = false) => {
    try {
      if (sameOperation) await retryWrite.operation.retry();
      else await retryWrite.operation.run(`/v1/grading-items/${itemId}/retry`, retryForm);
      setRetryOpen(false);
      resource.refresh();
      void message.success("已安排重试，请等待后台处理。");
    } catch { /* The operation keeps its ticket until the outcome is known. */ }
  };
  const loadLatest = () => {
    if (!item) return;
    modal.confirm({
      title: "载入最新复核？", content: "当前未提交的编辑将被老师最近一次保存的决定替换。",
      okText: "载入最新复核", cancelText: "继续编辑",
      onOk: () => replaceWith(item),
    });
  };
  const rebase = () => {
    if (!item) return;
    modal.confirm({
      title: "保留我的编辑并继续？",
      content: "请先比较下面的最新复核。继续后，你的下一次提交会在最新版本上追加记录，原记录仍保留。",
      okText: "已比较，继续编辑", cancelText: "返回比较",
      onOk: () => setSaved((previous) => previous && ({
        ...previous, draft: { ...previous.draft, expected_revision: item.review_revision },
      })),
    });
  };

  if (!item) return <><Button onClick={onBack}>← 返回答案列表</Button>
    <ErrorNotice error={resource.error} retry={resource.refresh} />{resource.loading && <Loading />}</>;
  const criteria = item.rubric.body.criteria;
  const editable = item.status === "succeeded" && !locked;
  const latest = item.reviews.reduce<typeof item.reviews[number] | undefined>(
    (last, row) => !last || row.revision > last.revision ? row : last, undefined,
  );
  return <>
    <button className="back-link" onClick={onBack}>← 返回答案列表</button>
    <div className="page-heading">
      <div><div className="eyebrow">{item.assignment.title} · 题目 {item.question.question_key}</div>
        <h1>{item.student.name}<span className="heading-aside">{item.student.external_id}</span></h1>
        <p>提交版本 {item.answer.source_revision} · 题目版本 {item.question.version} · 细则版本 {item.rubric.version}</p>
      </div>
      <div className="button-row"><StatusTag status={item.status} />
        {item.review_revision > 0 && <StatusTag status="confirmed" />}
        <Button loading={resource.loading} onClick={resource.refresh}>读取最新记录</Button>
      </div>
    </div>
    {item.simulation && <div className="notice warning">
      本份答案来自模拟评估。候选等级用于演示流程，确认前请逐点评判。
    </div>}
    <ErrorNotice error={resource.error} retry={resource.refresh} />
    <ErrorNotice error={write.error} />
    {write.phase === "uncertain" && <div className="notice warning" role="alert">
      上次提交结果未知，编辑暂时锁定。重试会复用原提交标识和内容。
      <Button onClick={() => void retryReview()}>重试同一次复核</Button>
    </div>}
    <ErrorNotice error={retryWrite.error} />
    {retryWrite.phase === "uncertain" && <div className="notice warning" role="alert">
      重试任务的请求结果未知。
      <Button onClick={() => void retryItem(true)}>重发同一次任务请求</Button>
    </div>}
    {conflict && <div className="conflict-panel" role="alert">
      <h2>复核版本已更新，请先比较</h2>
      <p>当前编辑基于第 {draft?.expected_revision} 次复核，服务器已保存第 {item.review_revision} 次。
        你的等级、理由和引用仍保留。</p>
      <div className="conflict-decisions">
        {latest?.decisions.map((decision) => {
          const criterion = criteria.find((row) => row.id === decision.criterion_id);
          const level = criterion?.levels.find((row) => row.id === decision.level_id);
          return <div key={decision.criterion_id}><strong>{criterion?.description}</strong>
            <p>{level?.description} · {level?.points} 分 · {supportLabels[decision.evidence_support]}</p>
            <p>{decision.reason}</p>
            {decision.citations.map((citation, index) => <blockquote key={index}>{citation.quote}</blockquote>)}
          </div>;
        })}
      </div>
      <div className="button-row"><Button disabled={locked} onClick={loadLatest}>载入最新复核</Button>
        <Button disabled={locked} onClick={rebase}>保留我的编辑，以最新版本继续</Button></div>
    </div>}
    <div className="review-layout">
      <div className="source-column">
        <section className="panel question-panel">
          <h2>题目</h2><p className="plain-text">{item.question.prompt}</p>
          <Collapse ghost items={[{ key: "reference", label: "参考答案与材料",
            children: <p className="plain-text">{item.question.reference || "未提供"}</p> },
          { key: "rules", label: "老师确认的整体评分说明",
            children: <p className="plain-text">{item.rubric.body.global_instructions}</p> }]} />
        </section>
        <section className="panel answer-panel">
          <div className="panel-toolbar"><h2>学生原答案</h2><Tag>固定提交版本</Tag></div>
          <p className="muted small">选中原文，再加入右侧评分点的证据。空白与换行按原提交保留。</p>
          <div className="original-answer" ref={answerRef} tabIndex={0}
            aria-label="学生原答案，使用鼠标或键盘选择引用"
            onMouseUp={capture} onKeyUp={capture} onTouchEnd={capture}>{item.answer.text}</div>
          <div className="evidence-picker">
            <label htmlFor="citation-criterion">引用归属评分点</label>
            <Select id="citation-criterion" value={active || undefined} onChange={setActive} disabled={!editable}
              options={criteria.map((criterion, index) => ({
                value: criterion.id, label: `${index + 1}. ${criterion.description}`,
              }))} />
            {selection ? <blockquote className="selected-quote">{selection.quote}</blockquote>
              : <p className="muted small">尚未选择原文片段</p>}
            {selectionError && <p className="field-error" role="alert">{selectionError}</p>}
            <Button disabled={!selection || !editable || !draft}
              onMouseDown={(event) => event.preventDefault()} onClick={addCitation}>加入该评分点</Button>
          </div>
          <p className="muted small">找到原文片段只说明引用存在，还需判断它是否支持所选等级。</p>
        </section>
      </div>
      <div className="decisions-column">
        {item.status !== "succeeded" && <section className="panel">
          <h2>{item.status === "failed" ? "初评未完成" : "正在等待初评"}</h2>
          <p>完整初评保存后才能提交老师复核。</p>
          {item.error_code && <p className="muted">失败原因：{item.error_code}</p>}
          <p className="muted small">已尝试 {item.execution.attempts} 次 · 已使用 {item.execution.calls_used} / {item.execution.max_calls} 次调用额度</p>
          {item.status === "failed" && <Button disabled={retryWrite.locked}
            onClick={() => setRetryOpen(true)}>单独重试这份答案</Button>}
        </section>}
        {criteria.map((criterion, index) => {
          const decision = draft?.decisions.find((row) => row.criterion_id === criterion.id);
          const assessment = item.assessments.find((row) => row.criterion_id === criterion.id);
          const candidate = criterion.levels.find((row) => row.id === assessment?.candidate_level_id);
          return <section className={`panel criterion-panel${active === criterion.id ? " active" : ""}`}
            key={criterion.id} onFocus={() => setActive(criterion.id)}>
            <div className="criterion-title"><span className="criterion-number">{String(index + 1).padStart(2, "0")}</span>
              <h2>{criterion.description}</h2><span className="muted">{criterion.maximum_points} 分</span></div>
            <div className="candidate"><span className="muted small">初评候选</span>
              <p>{candidate ? `${candidate.description} · ${candidate.points} 分`
                : assessment ? "模型未选择等级，请老师判断" : "尚未生成初评"}</p></div>
            {assessment?.issues.length ? <p className="muted small">需关注：
              {assessment.issues.map((issue) => issueLabels[issue] || issue).join("、")}</p> : null}
            {assessment && <Collapse ghost size="small" items={[{
              key: "distribution", label: "查看模型评估信息",
              children: <div className="small">
                <p>模型：{assessment.evaluation.returned_model}</p>
                {assessment.evaluation.probabilities
                  ? <ul>{criterion.levels.map((level) => <li key={level.id}>{level.description}：
                    {((assessment.evaluation.probabilities?.[level.id] ?? 0) * 100).toFixed(1)}%</li>)}</ul>
                  : <p>此评估器未提供等级概率分布。</p>}
                <p>Confidence：{assessment.evaluation.provider_confidence ?? "未提供"}。
                  这是模型输出分布信息，不能视为评分正确率。</p>
              </div>,
            }]} />}
            <fieldset disabled={!editable || !decision}>
              <legend>老师决定</legend>
              <Radio.Group className="level-options" aria-label={`评分点 ${index + 1} 等级`}
                disabled={!editable || !decision}
                value={decision?.level_id || undefined}
                onChange={(event) => update(criterion.id, { level_id: event.target.value })}>
                {[...criterion.levels].sort((a, b) => b.ordinal - a.ordinal).map((level) =>
                  <Radio key={level.id} value={level.id}>
                    <strong>{level.points} 分</strong><span>{level.description}</span>
                  </Radio>)}
              </Radio.Group>
              <label htmlFor={`support-${criterion.id}`}>证据判断</label>
              <Select id={`support-${criterion.id}`} value={decision?.evidence_support || undefined}
                placeholder="请选择对证据的判断" disabled={!editable || !decision}
                options={Object.entries(supportLabels).map(([value, label]) => ({ value, label }))}
                onChange={(value: EvidenceSupport) => update(criterion.id, {
                  evidence_support: value, ...(value === "absence_confirmed" ? { citations: [] } : {}),
                })} />
              <div className="citations">
                {decision?.citations.map((citation, citationIndex) => <div className="citation" key={citationIndex}>
                  <blockquote>{citation.quote}</blockquote>
                  <div className="citation-footer"><span>原文位置 {citation.start}–{citation.end}</span>
                    <Button size="small" type="text" onClick={() => locateCitation(citation)}>定位原文</Button>
                    <Button size="small" type="text" disabled={!editable}
                      aria-label={`移除评分点 ${index + 1} 引用 ${citationIndex + 1}`}
                      onClick={() => update(criterion.id, {
                        citations: decision.citations.filter((_, position) => position !== citationIndex),
                      })}>移除</Button></div>
                </div>)}
                {!decision?.citations.length && <p className="muted small">尚无引用，可在左侧原答案中选择。</p>}
              </div>
              <label htmlFor={`reason-${criterion.id}`}>复核理由</label>
              <Input.TextArea id={`reason-${criterion.id}`} value={decision?.reason || ""}
                disabled={!editable || !decision} autoSize={{ minRows: 2, maxRows: 8 }} maxLength={4000}
                placeholder="说明为什么选择该等级，以及证据如何支持判断。"
                onChange={(event) => update(criterion.id, { reason: event.target.value })} />
            </fieldset>
          </section>;
        })}
        {item.reviews.length > 0 && <section className="panel history-panel">
          <h2>老师复核历史</h2>
          <Collapse items={[...item.reviews].sort((a, b) => b.revision - a.revision).map((review) => ({
            key: review.id,
            label: `第 ${review.revision} 次 · ${review.confirmed_score} 分 · ${dateTime(review.created_at)}`,
            children: <div>{review.decisions.map((decision) => {
              const criterion = criteria.find((row) => row.id === decision.criterion_id);
              const level = criterion?.levels.find((row) => row.id === decision.level_id);
              return <div className="history-decision" key={decision.criterion_id}>
                <strong>{criterion?.description} · {level?.points} 分</strong>
                <p>{level?.description} · {supportLabels[decision.evidence_support]}</p><p>{decision.reason}</p>
                {decision.citations.map((citation, position) => <blockquote key={position}>{citation.quote}</blockquote>)}
              </div>;
            })}</div>,
          }))} />
        </section>}
      </div>
    </div>
    <div className="review-footer">
      <div className="score-summary">
        <span>初评建议<strong>{score(item.suggested_score)}</strong></span>
        <span>当前已确认<strong>{score(item.confirmed_score)}</strong></span>
        <span className="score-preview">本次预览<strong>{draft ? score(draftScore(item, draft)) : "—"}</strong></span>
      </div>
      <div className="footer-action">
        <span className="muted small">{dirty ? "有未提交编辑" : item.review_revision ? `已保存 ${item.review_revision} 次复核` : "请逐点检查并填写理由"}</span>
        <Button type="primary" size="large" disabled={!editable || !draft || conflict || resource.loading
          || (draft.expected_revision > 0 && !dirty)}
          loading={write.phase === "sending"} onClick={() => void submit()}>确认全部评分点</Button>
      </div>
      {errors.length > 0 && <ul className="validation-errors" role="alert">
        {errors.map((error) => <li key={error}>{error}</li>)}
      </ul>}
    </div>
    <Modal title="单独重试这份答案" open={retryOpen} okText="安排重试" cancelText="取消"
      confirmLoading={retryWrite.phase === "sending"} closable={!retryWrite.locked}
      cancelButtonProps={{ disabled: retryWrite.locked }}
      okButtonProps={{ disabled: retryWrite.locked || !retryForm.reason.trim() }}
      onCancel={() => { if (!retryWrite.locked) setRetryOpen(false); }} onOk={() => void retryItem()}>
      <p>后台会保留已完成步骤，并在新增额度内继续处理。接入真实模型时可能产生调用费用。</p>
      <label htmlFor="retry-reason">重试原因</label>
      <Input.TextArea id="retry-reason" value={retryForm.reason} disabled={retryWrite.locked}
        maxLength={4000} onChange={(event) => setRetryForm({ ...retryForm, reason: event.target.value })} />
      <div className="retry-budgets">
        <label>新增尝试次数<InputNumber min={1} max={3} precision={0} value={retryForm.additional_attempts}
          disabled={retryWrite.locked} onChange={(value) => setRetryForm({ ...retryForm, additional_attempts: value ?? 1 })} /></label>
        <label>新增调用上限<InputNumber min={0} max={60} precision={0} value={retryForm.additional_calls}
          disabled={retryWrite.locked} onChange={(value) => setRetryForm({ ...retryForm, additional_calls: value ?? 0 })} /></label>
      </div>
      <ErrorNotice error={retryWrite.error} />
      {retryWrite.phase === "uncertain" && <Button onClick={() => void retryItem(true)}>重发同一次任务请求</Button>}
    </Modal>
  </>;
}
