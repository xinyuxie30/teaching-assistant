import type { Criterion, ImportPayload, ImportPreview, ImportRow, RosterStudent, Rubric } from "../../api/types";

export const POLICY = { selection_policy: "unique-argmax-v1", rounding_policy: "total-half-up-2dp-v1" };
export const keyFor = (prefix: string) => `${prefix}_${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`;
export const charCount = (text: string) => Array.from(text).length;

export function pointCents(value: string): number | null {
  if (!/^\d{1,5}(?:\.\d{1,2})?$/.test(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  return cents <= 1_000_000 ? cents : null;
}
export function pointText(cents: number) { return (cents / 100).toFixed(2); }
export function newCriterion(maximum = "0.00"): Criterion {
  return {
    id: keyFor("point"), semantic_version: 1, description: "", maximum_points: maximum,
    levels: [
      { id: "missing", ordinal: 0, description: "", points: "0.00" },
      { id: "complete", ordinal: 1, description: "", points: maximum },
    ],
  };
}
export function newRubric(maximum: string): Rubric {
  return { ...POLICY, global_instructions: "", criteria: [newCriterion(maximum)] };
}
export function editableRubric(source: Rubric): Rubric {
  const copy = structuredClone(source);
  // The API permits any array order; ordinal is the authoritative level order.
  copy.criteria.forEach((criterion) => criterion.levels.sort((a, b) => a.ordinal - b.ordinal));
  return copy;
}

/** Normalize numeric display, preserve stable point/level IDs, version changed semantics. */
export function rubricPayload(draft: Rubric, questionMaximum: string, base?: Rubric): Rubric {
  const errors: string[] = [];
  if (!draft.global_instructions.trim()) errors.push("请填写整体评分说明。");
  if (draft.criteria.length < 1 || draft.criteria.length > 30) errors.push("需包含 1–30 个评分点。");
  let total = 0;
  const criteria = draft.criteria.map((criterion, index) => {
    const label = `评分点 ${index + 1}`;
    if (!criterion.description.trim()) errors.push(`${label}：请填写评估内容。`);
    if (criterion.levels.length < 2 || criterion.levels.length > 10) errors.push(`${label}：需包含 2–10 个等级。`);
    let prior = -1;
    const levels = criterion.levels.map((level, ordinal) => {
      if (!level.description.trim()) errors.push(`${label}第 ${ordinal + 1} 级：请填写可区分的等级描述。`);
      const cents = pointCents(level.points);
      if (cents === null) errors.push(`${label}第 ${ordinal + 1} 级：分数需为 0–10000，最多两位小数。`);
      if (cents !== null && cents < prior) errors.push(`${label}：等级分值应按由低到高排列。`);
      prior = cents ?? prior;
      return { ...level, ordinal, description: level.description.trim(), points: pointText(cents ?? 0) };
    });
    const maximum = levels.length ? pointCents(levels[levels.length - 1].points)! : 0;
    total += maximum;
    const normalized = { ...criterion, description: criterion.description.trim(), maximum_points: pointText(maximum), levels };
    const previous = base?.criteria.find((row) => row.id === criterion.id);
    if (previous) {
      const comparable = (value: Criterion) => JSON.stringify({
        description: value.description.trim(), maximum_points: pointText(pointCents(value.maximum_points) ?? 0),
        levels: [...value.levels].sort((a, b) => a.ordinal - b.ordinal).map((level) => ({
          ...level, description: level.description.trim(), points: pointText(pointCents(level.points) ?? 0),
        })),
      });
      normalized.semantic_version = previous.semantic_version + Number(comparable(previous) !== comparable(normalized));
    }
    return normalized;
  });
  if (total !== pointCents(questionMaximum)) errors.push(`各评分点满分之和应为 ${questionMaximum}，当前为 ${pointText(total)}。`);
  if (new Set(criteria.map((criterion) => criterion.id)).size !== criteria.length) errors.push("评分点标识重复，请重新起草。");
  if (errors.length) throw new Error(errors.join("\n"));
  return { ...POLICY, global_instructions: draft.global_instructions.trim(), criteria };
}

export interface AnswerEntry {
  key: string; student_id: string; external_submission_id: string; source_revision: string; answer_text: string;
}
export const emptyAnswer = (): AnswerEntry => ({
  key: keyFor("answer"), student_id: "", external_submission_id: "", source_revision: "initial", answer_text: "",
});
export const submissionId = (student: RosterStudent) => `submission-${student.external_id}`;
export function answerIdentity(row: Pick<ImportRow, "student_id" | "question_version_id" | "external_submission_id" | "source_revision">): string {
  return JSON.stringify([row.student_id, row.question_version_id, row.external_submission_id, row.source_revision]);
}

export function manualImport(rows: AnswerEntry[], roster: RosterStudent[], questionId: string): ImportPayload {
  if (!rows.length || rows.length > 200) throw new Error("每次导入需包含 1–200 份答案。");
  const allowed = new Set(roster.map((student) => student.id));
  const identities = new Set<string>();
  let characters = 0;
  const output = rows.map((row, index) => {
    const prefix = `第 ${index + 1} 份答案`;
    if (!allowed.has(row.student_id)) throw new Error(`${prefix}：请选择作业固定名单中的学生。`);
    if (!row.external_submission_id.trim() || charCount(row.external_submission_id) > 120) throw new Error(`${prefix}：来源编号需为 1–120 字符。`);
    if (!row.source_revision.trim() || charCount(row.source_revision) > 80) throw new Error(`${prefix}：提交版本需为 1–80 字符。`);
    const identity = answerIdentity({ ...row, question_version_id: questionId });
    if (identities.has(identity)) throw new Error(`${prefix}：同一学生、来源编号和提交版本重复。`);
    identities.add(identity);
    const length = charCount(row.answer_text);
    if (length > 100_000) throw new Error(`${prefix}：原文超过 100000 字符。`);
    characters += length;
    return { student_id: row.student_id, question_version_id: questionId,
      external_submission_id: row.external_submission_id, source_revision: row.source_revision,
      answer_text: row.answer_text };
  });
  if (characters > 2_000_000) throw new Error("本次原文合计超过 2000000 字符，请拆分导入。");
  return { rows: output };
}

export function pastedImport(text: string, roster: RosterStudent[], questionId: string): ImportPayload {
  let parsed: unknown;
  try { parsed = JSON.parse(text); } catch { throw new Error("JSON 格式无法读取，请检查引号、逗号和换行转义。"); }
  if (!Array.isArray(parsed) || !parsed.length || parsed.length > 200) throw new Error("请提供包含 1–200 份答案的 JSON 数组。");
  const students = new Map(roster.map((student) => [student.external_id, student]));
  const allowed = new Set(["external_student_id", "external_submission_id", "source_revision", "answer_text"]);
  const rows = parsed.map((row: unknown, index) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) throw new Error(`第 ${index + 1} 项需为答案对象。`);
    const entry = row as Record<string, unknown>;
    if (Object.keys(entry).some((key) => !allowed.has(key))) throw new Error(`第 ${index + 1} 项包含未知字段，请使用给定的四个字段。`);
    const student = typeof entry.external_student_id === "string" ? students.get(entry.external_student_id) : undefined;
    if (!student) throw new Error(`第 ${index + 1} 项：学号不在这份作业的固定名单中。`);
    if (typeof entry.answer_text !== "string" || typeof entry.source_revision !== "string"
      || (entry.external_submission_id !== undefined && typeof entry.external_submission_id !== "string")) {
      throw new Error(`第 ${index + 1} 项：原文、提交版本和可选来源编号必须是字符串。`);
    }
    return { key: String(index), student_id: student.id, answer_text: entry.answer_text,
      source_revision: entry.source_revision,
      external_submission_id: (entry.external_submission_id as string | undefined) ?? submissionId(student) };
  });
  return manualImport(rows, roster, questionId);
}

export interface PreviewGate { body: string; result: ImportPreview }
export function importForCommit(payload: ImportPayload, preview?: PreviewGate): ImportPayload {
  if (!preview?.result.valid || preview.body !== JSON.stringify(payload)) {
    throw new Error("内容已修改或尚未通过服务器预览，请重新预览后导入。");
  }
  return JSON.parse(preview.body) as ImportPayload;
}
export const formErrors = (error: unknown) => error instanceof Error ? error.message.split("\n") : ["请检查填写内容。"];
