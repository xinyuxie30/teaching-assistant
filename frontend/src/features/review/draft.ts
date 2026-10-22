import type { Citation, Decision, EvidenceSupport, ItemDetail, ReviewPayload } from "../../api/types";
import { citationMatches } from "./evidence";

export interface DraftDecision {
  criterion_id: string;
  level_id: string;
  evidence_support: EvidenceSupport | "";
  reason: string;
  citations: Citation[];
}
export interface ReviewDraft { expected_revision: number; decisions: DraftDecision[] }
export function initialDraft(item: ItemDetail): ReviewDraft {
  const latest = item.reviews.reduce<typeof item.reviews[number] | undefined>(
    (current, review) => !current || review.revision > current.revision ? review : current, undefined,
  );
  return {
    expected_revision: item.review_revision,
    decisions: item.rubric.body.criteria.map((criterion) => {
      const previous = latest?.decisions.find((decision) => decision.criterion_id === criterion.id);
      const assessment = item.assessments.find((value) => value.criterion_id === criterion.id);
      return {
        criterion_id: criterion.id,
        level_id: previous?.level_id ?? assessment?.candidate_level_id ?? "",
        evidence_support: previous?.evidence_support ?? "",
        reason: previous?.reason ?? "",
        citations: structuredClone(previous?.citations ?? assessment?.evidence.citations ?? []),
      };
    }),
  };
}
export function draftErrors(item: ItemDetail, draft: ReviewDraft): string[] {
  const errors: string[] = [];
  for (const [index, criterion] of item.rubric.body.criteria.entries()) {
    const decision = draft.decisions.find((value) => value.criterion_id === criterion.id);
    const prefix = `评分点 ${index + 1}`;
    if (!decision || !criterion.levels.some((level) => level.id === decision.level_id)) {
      errors.push(`${prefix}：请选择等级。`);
      continue;
    }
    if (!decision.evidence_support) errors.push(`${prefix}：请选择证据判断。`);
    if (!decision.reason.trim()) errors.push(`${prefix}：请填写复核理由。`);
    if (decision.evidence_support === "supported" && !decision.citations.length) {
      errors.push(`${prefix}：请添加支持这一等级的原文引用。`);
    }
    if (decision.citations.some((citation) => !citationMatches(item.answer.text, citation))) {
      errors.push(`${prefix}：引用已与原文不一致，请重新选择。`);
    }
  }
  return errors;
}
export function reviewPayload(item: ItemDetail, draft: ReviewDraft): ReviewPayload {
  const errors = draftErrors(item, draft);
  if (errors.length) throw new Error(errors.join("\n"));
  return {
    expected_revision: draft.expected_revision,
    decisions: draft.decisions.map((decision) => ({
      ...decision,
      evidence_support: decision.evidence_support as Decision["evidence_support"],
      citations: decision.evidence_support === "absence_confirmed" ? [] : decision.citations,
    })),
  };
}

// Preview with integer cents; the server remains authoritative for accepted scores.
export function draftScore(item: ItemDetail, draft: ReviewDraft): string | null {
  let cents = 0;
  for (const criterion of item.rubric.body.criteria) {
    const selected = draft.decisions.find((value) => value.criterion_id === criterion.id)?.level_id;
    const level = criterion.levels.find((value) => value.id === selected);
    if (!level) return null;
    const [whole, fraction = ""] = level.points.split(".");
    cents += Number(whole) * 100 + Number(fraction.padEnd(2, "0").slice(0, 2));
  }
  return (cents / 100).toFixed(2);
}
