import { describe, expect, it } from "vitest";
import type { RosterStudent } from "../../api/types";
import { csvImport, parseCsv } from "./contracts";

const roster: RosterStudent[] = [
  { id: "student-a", external_id: "001", name: "甲" },
  { id: "student-b", external_id: "002", name: "乙" },
];

describe("CSV parsing", () => {
  it("handles BOM, CRLF, quoted commas, embedded newlines and doubled quotes", () => {
    const text = '\uFEFFid,text\r\n001,"第一行, 有逗号\r\n第二行"\r\n002,"他说""好"""\r\n';
    expect(parseCsv(text)).toEqual([
      ["id", "text"],
      ["001", "第一行, 有逗号\r\n第二行"],
      ["002", '他说"好"'],
    ]);
  });

  it("keeps a trailing field and rejects unclosed quotes", () => {
    expect(parseCsv("a,b\n1,")).toEqual([["a", "b"], ["1", ""]]);
    expect(() => parseCsv('a\n"unclosed')).toThrow("未闭合");
    expect(() => parseCsv('a\nx"y')).toThrow("字段开头");
  });
});

describe("CSV import contract", () => {
  it("maps external student IDs and fills safe defaults", () => {
    const payload = csvImport("external_student_id,answer_text\n001, 保留首尾空格 \n002,", roster, "question-1");
    expect(payload.rows).toEqual([
      {
        student_id: "student-a", question_version_id: "question-1",
        external_submission_id: "submission-001", source_revision: "initial",
        answer_text: " 保留首尾空格 ",
      },
      {
        student_id: "student-b", question_version_id: "question-1",
        external_submission_id: "submission-002", source_revision: "initial", answer_text: "",
      },
    ]);
  });

  it("honours explicit submission and revision columns", () => {
    const payload = csvImport(
      "external_student_id,answer_text,external_submission_id,source_revision\n001,答,a:v1,rev-2\n",
      roster, "question-1",
    );
    expect(payload.rows[0]).toMatchObject({
      external_submission_id: "a:v1", source_revision: "rev-2",
    });
  });

  it("rejects unknown or missing headers, unknown students and duplicate identities", () => {
    expect(() => csvImport("external_student_id,answer\n001,x", roster, "q")).toThrow("无法识别");
    expect(() => csvImport("external_student_id\n001", roster, "q")).toThrow("必须包含");
    expect(() => csvImport("external_student_id,answer_text\n999,x", roster, "q")).toThrow("固定名单");
    expect(() => csvImport(
      "external_student_id,answer_text\n001,x\n001,y", roster, "q",
    )).toThrow("重复");
    expect(() => csvImport("external_student_id,answer_text\n001,x\n001,x", roster, "q")).toThrow("重复");
  });

  it("refuses an empty submission or a blank line inside the data", () => {
    expect(() => csvImport(
      "external_student_id,answer_text,source_revision\n001,x,", roster, "q",
    )).toThrow("提交版本");
    expect(() => csvImport("external_student_id,answer_text\n001,x\n,x", roster, "q")).toThrow("学号");
  });
});
