import { describe, expect, it } from "vitest";
import type { ItemDetail, Review } from "../../api/types";
import { draftErrors, draftScore, initialDraft, reviewPayload } from "./draft";

function item(): ItemDetail {
  return {
    id: "item", status: "succeeded", error_code: null, simulation: true,
    student: { id: "student", name: "Student", external_id: "s1" },
    assignment: { id: "assignment", title: "作业", course_id: "course", classroom_id: "class" },
    answer: { id: "answer", text: "😀规则\r\n历史", content_hash: "hash", source_revision: "initial" },
    question: { id: "question", question_key: "q1", version: 1, prompt: "题目", reference: "参考" },
    rubric: { id: "rubric", version: 1, body: {
      global_instructions: "按点判断", selection_policy: "unique-argmax-v1", rounding_policy: "decimal-2",
      criteria: ["rule", "history"].map((id) => ({
        id, semantic_version: 1, description: id, maximum_points: "0.20",
        levels: [
          { id: "missing", ordinal: 0, points: "0.00", description: "未表达" },
          { id: "partial", ordinal: 1, points: "0.10", description: "部分表达" },
          { id: "complete", ordinal: 2, points: "0.20", description: "完整表达" },
        ],
      })),
    } },
    suggested_score: "0.00", confirmed_score: null, review_revision: 0, reviews: [],
    assessments: ["rule", "history"].map((criterion_id) => ({
      criterion_id, candidate_level_id: "missing", suggested_points: "0.00", issues: [],
      evidence: { citations: [{ quote: "😀规则\r\n历史", start: 0, end: 7 }], existence_verified: true },
      evaluation: { probabilities: null, provider_confidence: null, returned_model: "fake-v1" },
    })),
    evaluation_profile: "fake-v1", evaluation_profile_manifest: { model: "fake-v1", simulation: true },
    execution: { attempts: 1, calls_used: 4, max_calls: 12 },
  };
}

describe("review drafts", () => {
  it("loads the highest teacher revision, including citations, rather than model defaults", () => {
    const source = item();
    const latest: Review = {
      id: "review-2", revision: 2, confirmed_score: "0.30", created_at: "2026-10-20T00:00:00Z",
      decisions: [
        { criterion_id: "rule", level_id: "partial", evidence_support: "supported",
          reason: "老师理由", citations: [{ quote: "规则", start: 1, end: 3 }] },
        { criterion_id: "history", level_id: "complete", evidence_support: "supported",
          reason: "已核对历史", citations: [{ quote: "历史", start: 5, end: 7 }] },
      ],
    };
    source.review_revision = 2;
    source.reviews = [latest, { ...structuredClone(latest), id: "review-1", revision: 1 }];
    const draft = initialDraft(source);
    expect(draft.expected_revision).toBe(2);
    expect(reviewPayload(source, draft).decisions).toEqual(latest.decisions);
    expect(draftScore(source, draft)).toBe("0.30");
    draft.decisions[0].citations[0].quote = "changed";
    expect(source.reviews[0].decisions[0].citations[0].quote).toBe("规则");
    expect(draftErrors(source, draft).join("")).toContain("引用已与原文不一致");
  });

  it("does not convert model abstention into zero, or silently confirm a candidate", () => {
    const source = item();
    source.assessments[0].candidate_level_id = null;
    const draft = initialDraft(source);
    expect(draftScore(source, draft)).toBeNull();
    expect(draftErrors(source, draft).join("")).toContain("请选择等级");
    expect(draft.decisions[1].evidence_support).toBe("");
    expect(() => reviewPayload(source, draft)).toThrow();
  });

  it("requires supporting evidence and explicitly serializes empty citations for absence", () => {
    const source = item();
    const draft = initialDraft(source);
    for (const decision of draft.decisions) {
      decision.reason = "阅读了完整答案";
      decision.evidence_support = "absence_confirmed";
      decision.citations = [];
    }
    expect(reviewPayload(source, draft).decisions.every((decision) =>
      Object.hasOwn(decision, "citations") && decision.citations.length === 0)).toBe(true);
    draft.decisions[0].evidence_support = "supported";
    expect(() => reviewPayload(source, draft)).toThrow("请添加支持");
    draft.decisions[0].evidence_support = "teacher_judgment";
    expect(draftErrors(source, draft)).toEqual([]);
    draft.decisions[0].reason = "  ";
    expect(draftErrors(source, draft).join("")).toContain("请填写复核理由");
  });
});
