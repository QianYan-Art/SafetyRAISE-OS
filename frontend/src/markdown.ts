const MARKDOWN_TABLE_SEPARATOR = /\|\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|/;
const COLLAPSED_TABLE_ROW = /\|\s*\|(?=\s*(?:\*\*|`|[0-9A-Za-z\u4e00-\u9fa5]))/g;
const SPACED_STRONG_LABEL = /\*\*\s*([^\n*]+?[：:])\s+\*\*(?=\S)/g;
const TIGHT_STRONG_LABEL = /\*\*([^\n*]+?[：:])\*\*(?=\S)/g;

export function normalizeMarkdownForDisplay(source: string): string {
  let content = (source || "")
    .replace(/\r\n?/g, "\n")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/\u00a0/g, " ")
    .trim();
  if (!content) {
    return "";
  }

  content = content
    .replace(SPACED_STRONG_LABEL, "**$1** ")
    .replace(TIGHT_STRONG_LABEL, "**$1** ");

  // 修复模型输出的 ATX 标题缺空格（`#标题` → `# 标题`）。CommonMark 要求 `#` 后必须有空格
  // 才识别为标题，否则会被当成普通文本、`#` 字面漏出。跳过围栏代码块，避免误伤 shell 注释等。
  let inFence = false;
  content = content
    .split("\n")
    .map((line) => {
      if (/^\s{0,3}(```|~~~)/.test(line)) {
        inFence = !inFence;
        return line;
      }
      if (inFence) {
        return line;
      }
      return line.replace(/^(\s{0,3})(#{1,6})(?=[^#\s])/, "$1$2 ");
    })
    .join("\n");

  const repairedLines = content.split("\n").flatMap((rawLine) => {
    if (rawLine.split("|").length >= 7 && MARKDOWN_TABLE_SEPARATOR.test(rawLine)) {
      return rawLine
        .replace(/(\|\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|)\s*(?=\|)/, "$1\n")
        .replace(COLLAPSED_TABLE_ROW, "|\n| ")
        .split("\n");
    }
    return [rawLine];
  });

  content = repairedLines.join("\n");
  const lines = content.split("\n");
  const padded: string[] = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();
    const nextLine = lines[index + 1]?.trim() ?? "";
    const isTableHeader = trimmed.startsWith("|") && nextLine.startsWith("|") && MARKDOWN_TABLE_SEPARATOR.test(nextLine);
    const isTableLine = trimmed.startsWith("|");

    if (isTableHeader && padded.length > 0 && padded[padded.length - 1].trim()) {
      padded.push("");
    }

    padded.push(isTableLine && trimmed && !trimmed.endsWith("|") ? `${line} |` : line);

    if (!isTableLine && index > 0 && lines[index - 1].trim().startsWith("|") && trimmed) {
      padded.splice(padded.length - 1, 0, "");
    }
  }
  return padded.join("\n").trim();
}
