import { afterEach, describe, expect, it } from "vitest";
import { citationAt, citationMatches, selectionCitation } from "./evidence";

afterEach(() => {
  window.getSelection()?.removeAllRanges();
  document.body.replaceChildren();
});

describe("answer citations", () => {
  it("preserves CRLF, emoji and the second occurrence of identical text", () => {
    const text = "  😀同一句\r\n同一句  ";
    const start = text.lastIndexOf("同一句");
    const citation = citationAt(text, start, start + 3);
    expect(citation).toEqual({ quote: "同一句", start: 8, end: 11 });
    expect(citationMatches(text, citation)).toBe(true);
    const crossLine = citationAt(text, 2, start + 3);
    expect(crossLine).toEqual({ quote: "😀同一句\r\n同一句", start: 2, end: 11 });
    expect(citationMatches(text.replace("\r\n", "\n"), citation)).toBe(false);
  });

  it("rejects UTF-16 offsets that split a character and mismatched quotations", () => {
    expect(() => citationAt("A😀B", 1, 2)).toThrow();
    expect(() => citationAt("A😀B", 2, 3)).toThrow();
    expect(citationAt("A😀B", 1, 3)).toEqual({ quote: "😀", start: 1, end: 2 });
    expect(citationMatches("A😀B", { quote: "B", start: 1, end: 2 })).toBe(false);
    expect(citationMatches("A😀B", { quote: "", start: 2, end: 2 })).toBe(false);
  });

  it("reads DOM offsets from the unmodified text node, including repeated quotes", () => {
    const original = "😀重复\r\n重复";
    const source = document.createElement("div");
    source.textContent = original;
    document.body.append(source);
    const range = document.createRange();
    range.setStart(source.firstChild!, original.lastIndexOf("重复"));
    range.setEnd(source.firstChild!, original.length);
    const selection = window.getSelection()!;
    selection.addRange(range);
    expect(selectionCitation(source, original, selection))
      .toEqual({ quote: "重复", start: 5, end: 7 });
    expect(() => selectionCitation(source, original.replace("\r\n", "\n"), selection)).toThrow();
    const outside = document.createTextNode("outside");
    document.body.append(outside);
    range.setEnd(outside, 3);
    selection.removeAllRanges();
    selection.addRange(range);
    expect(() => selectionCitation(source, original, selection)).toThrow();
  });

  it("accepts whole-container boundaries but never markup inserted into the source", () => {
    const source = document.createElement("div");
    source.textContent = "完整答案";
    document.body.append(source);
    const selection = window.getSelection()!;
    const range = document.createRange();
    range.selectNodeContents(source);
    selection.addRange(range);
    expect(selectionCitation(source, "完整答案", selection))
      .toEqual({ quote: "完整答案", start: 0, end: 4 });
    source.append(document.createElement("mark"));
    expect(() => selectionCitation(source, "完整答案", selection)).toThrow();
  });
});
