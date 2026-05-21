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
