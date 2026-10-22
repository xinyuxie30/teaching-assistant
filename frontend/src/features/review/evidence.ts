import type { Citation } from "../../api/types";

export function citationAt(text: string, startUtf16: number, endUtf16: number): Citation {
  if (!Number.isInteger(startUtf16) || !Number.isInteger(endUtf16)
    || startUtf16 < 0 || endUtf16 > text.length || endUtf16 <= startUtf16) {
    throw new Error("请先选择一段完整的原文。");
  }
  const splitsSurrogate = (offset: number) => offset > 0 && offset < text.length
    && /[\uD800-\uDBFF]/.test(text[offset - 1]) && /[\uDC00-\uDFFF]/.test(text[offset]);
  if (splitsSurrogate(startUtf16) || splitsSurrogate(endUtf16)) {
    throw new Error("选区切开了一个字符，请重新选择。");
  }
  return {
    quote: text.slice(startUtf16, endUtf16),
    start: Array.from(text.slice(0, startUtf16)).length,
    end: Array.from(text.slice(0, endUtf16)).length,
  };
}

export function citationMatches(text: string, citation: Citation) {
  const points = Array.from(text);
  return Number.isInteger(citation.start) && Number.isInteger(citation.end)
    && citation.start >= 0 && citation.end > citation.start && citation.end <= points.length
    && points.slice(citation.start, citation.end).join("") === citation.quote;
}

/** Only capture a DOM range inside this exact, unmodified text node. Never search by quote. */
export function selectionCitation(container: HTMLElement, text: string, selection: Selection | null) {
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  const node = container.firstChild;
  if (!node || node.nodeType !== Node.TEXT_NODE || node.nodeValue !== text || container.childNodes.length !== 1) {
    throw new Error("原文显示内容已变化，请重新载入后选择。");
  }
  const boundary = (target: Node, offset: number) => {
    if (target === node) return offset;
    if (target === container && offset === 0) return 0;
    if (target === container && offset === 1) return text.length;
    throw new Error("请将选区完整放在学生原答案内。");
  };
  return citationAt(text, boundary(range.startContainer, range.startOffset), boundary(range.endContainer, range.endOffset));
}
