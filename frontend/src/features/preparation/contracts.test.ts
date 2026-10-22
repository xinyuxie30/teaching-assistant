import { describe, expect, it } from "vitest";
import type { ImportPreview, RosterStudent, Rubric } from "../../api/types";
import { answerIdentity, editableRubric, importForCommit, manualImport, pastedImport, POLICY, rubricPayload } from "./contracts";

const roster: RosterStudent[] = [{ id: "student-a", external_id: "001", name: "甲" }];
const preview: ImportPreview = { valid: true, row_count: 1, reused: 0, notice: "Revalidate at commit" };
const raw = "  😀第一行\r\n第二行\n  ";
const answer = {
  key: "local-row", student_id: "student-a", external_submission_id: "submission-001",
  source_revision: "initial", answer_text: raw,
};
const rubric: Rubric = {
  ...POLICY, global_instructions: "按评分点独立判断。",
  criteria: [{ id: "point-a", semantic_version: 3, description: "解释版本的用途", maximum_points: "10.00",
    // Intentionally unsorted, as permitted by the backend contract.
    levels: [
      { id: "complete", ordinal: 2, description: "说明一致性及可追溯性", points: "10.00" },
      { id: "missing", ordinal: 0, description: "未说明上述用途", points: "0.00" },
      { id: "partial", ordinal: 1, description: "只说明其中一项", points: "5.00" },
    ],
  }],
};

describe("teacher-confirmed rubric preparation", () => {
  it("preserves stable identities and semantics when copying an unsorted saved rule", () => {
    const draft = editableRubric(rubric);
    draft.criteria[0].levels[1].points = "5";
    const payload = rubricPayload(draft, "10.00", rubric);
    expect(payload.criteria[0].levels.map((level) => level.id)).toEqual(["missing", "partial", "complete"]);
    expect(payload.criteria[0].semantic_version).toBe(3);
    expect(rubric.criteria[0].levels[0].id).toBe("complete");
    expect(rubric.criteria[0].levels[2].points).toBe("5.00");
  });

  it("increments a changed point's semantic version while retaining its identity", () => {
    const draft = editableRubric(rubric);
    draft.criteria[0].levels[1].description = "必须解释版本与一致性的联系";
    const payload = rubricPayload(draft, "10.00", rubric);
    expect(payload.criteria[0]).toMatchObject({ id: "point-a", semantic_version: 4 });
    expect(payload.criteria[0].levels[1].id).toBe("partial");
    expect(rubric.criteria[0].semantic_version).toBe(3);
  });

  it("rejects decreasing points, incorrect totals and missing level descriptions", () => {
    const draft = editableRubric(rubric);
    draft.criteria[0].levels[1].points = "11.00";
    expect(() => rubricPayload(draft, "10.00", rubric)).toThrow("由低到高");
    draft.criteria[0].levels[1].points = "5.00";
    expect(() => rubricPayload(draft, "20.00", rubric)).toThrow("满分之和");
    draft.criteria[0].levels[1].description = "  ";
    expect(() => rubricPayload(draft, "10.00", rubric)).toThrow("等级描述");
  });
});

describe("versioned answer import", () => {
  it("preserves raw text and explicit blank submissions while excluding UI-only keys", () => {
    const payload = manualImport([answer, { ...answer, key: "blank", source_revision: "correction", answer_text: "" }], roster, "q1-v2");
    expect(payload.rows[0].answer_text).toBe(raw);
    expect(payload.rows[1].answer_text).toBe("");
    expect(payload.rows[0]).not.toHaveProperty("key");
    expect(payload.rows.every((row) => row.question_version_id === "q1-v2")).toBe(true);
  });

  it("maps exact external IDs from the frozen roster, never array positions or numeric IDs", () => {
    const text = JSON.stringify([{ external_student_id: "001", source_revision: "initial", answer_text: raw }]);
    expect(pastedImport(text, roster, "q1")).toEqual(manualImport([answer], roster, "q1"));
    expect(() => pastedImport(text.replace('"001"', '"1"'), roster, "q1")).toThrow("固定名单");
    expect(() => pastedImport(text.replace('"001"', "1"), roster, "q1")).toThrow("固定名单");
    const injected = JSON.stringify([{ external_student_id: "001", source_revision: "initial", answer_text: raw, student_id: "other" }]);
    expect(() => pastedImport(injected, roster, "q1")).toThrow("未知字段");
  });

  it("rejects duplicate source revisions and students outside the assignment", () => {
    expect(() => manualImport([answer, { ...answer, key: "second" }], roster, "q1")).toThrow("重复");
    expect(() => manualImport([{ ...answer, student_id: "new-class-member" }], roster, "q1")).toThrow("固定名单");
  });

  it("keeps delimiter-bearing source identities distinct in import and preview", () => {
    const rows = manualImport([
      { ...answer, external_submission_id: "source:a", source_revision: "v1" },
      { ...answer, key: "second", external_submission_id: "source", source_revision: "a:v1" },
    ], roster, "q1").rows;
    expect(rows).toHaveLength(2);
    expect(new Set(rows.map(answerIdentity)).size).toBe(2);
    expect(answerIdentity({ ...rows[0] })).toBe(answerIdentity(rows[0]));
    expect(answerIdentity({ ...rows[0], question_version_id: "q2" })).not.toBe(answerIdentity(rows[0]));
    const changedContent = { ...rows[0], answer_text: "changed content" };
    expect(answerIdentity(changedContent)).toBe(answerIdentity(rows[0]));
  });

  it("applies text limits in Unicode code points, matching Python rather than UTF-16 length", () => {
    expect(manualImport([{ ...answer, answer_text: "😀".repeat(100_000) }], roster, "q1").rows).toHaveLength(1);
    expect(() => manualImport([{ ...answer, answer_text: "😀".repeat(100_001) }], roster, "q1")).toThrow("100000");
  });

  it("requires a valid preview for exactly the same answer, version, student and question", () => {
    const payload = manualImport([answer], roster, "q1");
    const gate = { body: JSON.stringify(payload), result: preview };
    expect(importForCommit(payload, gate)).toEqual(payload);
    expect(importForCommit(payload, gate)).not.toBe(payload);
    expect(() => importForCommit(payload)).toThrow("重新预览");
    expect(() => importForCommit(payload, { ...gate, result: { ...preview, valid: false } })).toThrow("重新预览");
    for (const change of [
      { answer_text: raw.trim() }, { source_revision: "correction" },
      { student_id: "student-b" }, { question_version_id: "q2" }, { external_submission_id: "another" },
    ]) {
      expect(() => importForCommit({ rows: [{ ...payload.rows[0], ...change }] }, gate)).toThrow("重新预览");
    }
  });
});
