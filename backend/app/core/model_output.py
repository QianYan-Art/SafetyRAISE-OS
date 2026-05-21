import re


_REASONING_TAGS = ("think", "reasoning", "analysis")
_REASONING_TAG_PATTERN = re.compile(
    r"<(?P<tag>think|reasoning|analysis)\b[^>]*>.*?</(?P=tag)>\s*",
    re.DOTALL | re.IGNORECASE,
)
_REASONING_FENCE_PATTERN = re.compile(
    r"```(?:thinking|reasoning|analysis|thoughts?)\b[^\n]*\n?(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_MARKDOWN_FENCE_PATTERN = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_-]+)?\s*(?P<body>.*?)```",
    re.DOTALL,
)
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_HTML_BREAK_PATTERN = re.compile(r"(?i)<br\s*/?>")
_TABLE_SEPARATOR_ROW_PATTERN = re.compile(r"\|\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|")
_COLLAPSED_TABLE_ROW_PATTERN = re.compile(
    r"(?<=\|)\s*\|\s*(?=(?:\*\*|`|[0-9A-Za-z\u4e00-\u9fa5]))"
)
_THEMATIC_BREAK_PATTERN = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$")
_ORDERED_LIST_PATTERN = re.compile(r"^\d+[.)]\s+")
_UNORDERED_LIST_PATTERN = re.compile(r"^[-*+]\s+")
_STANDALONE_BOLD_HEADING_PATTERN = re.compile(
    r"^\*\*(?P<title>.+?)\*\*\s*[：:]\s*$"
)
_SPACED_STRONG_LABEL_PATTERN = re.compile(r"\*\*\s*([^\n*]+?[：:])\s+\*\*(?=\S)")
_TIGHT_STRONG_LABEL_PATTERN = re.compile(r"\*\*([^\n*]+?[：:])\*\*(?=\S)")
_FENCE_DELIMITER_PATTERN = re.compile(r"^```")


def sanitize_model_text(text: str) -> str:
    content = (text or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        return ""

    content = _REASONING_TAG_PATTERN.sub("", content)
    content = _REASONING_FENCE_PATTERN.sub("", content)
    return content.strip()


def sanitize_markdown_output(text: str) -> str:
    content = sanitize_model_text(text)
    if not content:
        return ""

    fenced_markdown = _extract_preferred_fence(content, preferred_languages=("markdown", "md"))
    if fenced_markdown is not None:
        content = fenced_markdown
    elif content.startswith("```") and content.endswith("```"):
        fenced_any = _extract_preferred_fence(content, preferred_languages=())
        if fenced_any is not None:
            content = fenced_any

    content = _repair_markdown_structure(content)
    content = content.strip()
    first_heading = _HEADING_PATTERN.search(content)
    if first_heading and first_heading.start() > 0 and first_heading.group(1) == "#":
        prefix = content[: first_heading.start()].strip()
        if prefix and len(prefix.splitlines()) <= 3:
            content = content[first_heading.start() :].lstrip()

    if not re.search(r"^#\s+.+$", content, re.MULTILINE):
        content = f"# 交通事故分析报告\n\n{content.strip()}"

    return content.strip()


def split_markdown_sections(markdown: str) -> list[dict[str, str]]:
    content = (markdown or "").strip()
    if not content:
        return []

    matches = list(_HEADING_PATTERN.finditer(content))
    if not matches:
        return [{"title": "正文", "content": content}]

    sections: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        title = match.group(2).strip()
        body = _strip_section_separators(content[start:end].strip())
        section_content = body if body else match.group(0).strip()
        sections.append({"title": title, "content": section_content})
    return sections


def _extract_preferred_fence(content: str, preferred_languages: tuple[str, ...]) -> str | None:
    matches = list(_MARKDOWN_FENCE_PATTERN.finditer(content))
    if not matches:
        return None

    if preferred_languages:
        preferred = {item.lower() for item in preferred_languages}
        for match in matches:
            language = (match.group("lang") or "").lower()
            if language in preferred:
                return match.group("body").strip()

    if len(matches) == 1 and matches[0].span() == (0, len(content)):
        return matches[0].group("body").strip()
    return None


def _repair_markdown_structure(content: str) -> str:
    normalized = _HTML_BREAK_PATTERN.sub("\n", content)
    normalized = normalized.replace("\u00a0", " ")
    normalized = _normalize_strong_labels(normalized)
    normalized = _promote_standalone_headings(normalized)
    normalized = _repair_collapsed_table_rows(normalized)
    normalized = _normalize_markdown_blocks(normalized)
    return normalized.strip()


def _normalize_strong_labels(content: str) -> str:
    normalized = _SPACED_STRONG_LABEL_PATTERN.sub(r"**\1** ", content)
    normalized = _TIGHT_STRONG_LABEL_PATTERN.sub(r"**\1** ", normalized)
    return normalized


def _repair_collapsed_table_rows(content: str) -> str:
    repaired_lines: list[str] = []
    for raw_line in content.split("\n"):
        line = raw_line
        if raw_line.count("|") >= 6 and _TABLE_SEPARATOR_ROW_PATTERN.search(raw_line):
            line = _TABLE_SEPARATOR_ROW_PATTERN.sub(
                lambda match: f"{match.group(0)}\n",
                raw_line,
                1,
            )
            line = _COLLAPSED_TABLE_ROW_PATTERN.sub("|\n| ", line)
        repaired_lines.extend(line.splitlines())
    return "\n".join(repaired_lines)


def _promote_standalone_headings(content: str) -> str:
    promoted_lines: list[str] = []
    for raw_line in content.split("\n"):
        stripped = raw_line.strip()
        match = _STANDALONE_BOLD_HEADING_PATTERN.fullmatch(stripped)
        if match:
            title = (match.group("title") or "").strip()
            if title:
                promoted_lines.append(f"## {title}")
                continue
        promoted_lines.append(raw_line.rstrip())
    return "\n".join(promoted_lines)


def _normalize_markdown_blocks(content: str) -> str:
    lines = content.split("\n")
    blocks: list[str] = []
    paragraph_lines: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        blocks.append("\n".join(paragraph_lines).strip())
        paragraph_lines = []

    while index < len(lines):
        raw_line = lines[index].rstrip()
        stripped = raw_line.strip()

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if _is_heading_like_line(stripped) or _THEMATIC_BREAK_PATTERN.fullmatch(stripped):
            flush_paragraph()
            blocks.append(stripped)
            index += 1
            continue

        if _FENCE_DELIMITER_PATTERN.match(stripped):
            flush_paragraph()
            fenced_block = [raw_line]
            index += 1
            while index < len(lines):
                fenced_block.append(lines[index].rstrip())
                if _FENCE_DELIMITER_PATTERN.match(lines[index].strip()):
                    index += 1
                    break
                index += 1
            blocks.append("\n".join(fenced_block).strip())
            continue

        if stripped.startswith("|"):
            flush_paragraph()
            table_lines = [stripped]
            index += 1
            while index < len(lines):
                candidate = lines[index].rstrip()
                candidate_stripped = candidate.strip()
                if not candidate_stripped.startswith("|"):
                    break
                table_lines.append(candidate_stripped)
                index += 1
            blocks.append("\n".join(table_lines).strip())
            continue

        if _is_list_line(stripped):
            flush_paragraph()
            list_lines = [raw_line]
            index += 1
            while index < len(lines):
                candidate = lines[index].rstrip()
                candidate_stripped = candidate.strip()
                if not candidate_stripped:
                    break
                if _is_list_line(candidate_stripped) or candidate.startswith("  ") or candidate.startswith("\t"):
                    list_lines.append(candidate)
                    index += 1
                    continue
                break
            blocks.append("\n".join(list_lines).strip())
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            quote_lines = [stripped]
            index += 1
            while index < len(lines):
                candidate = lines[index].rstrip()
                candidate_stripped = candidate.strip()
                if not candidate_stripped.startswith(">"):
                    break
                quote_lines.append(candidate_stripped)
                index += 1
            blocks.append("\n".join(quote_lines).strip())
            continue

        paragraph_lines.append(stripped)
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if (
            _line_ends_paragraph(stripped)
            or not next_line
            or _starts_new_block(next_line)
        ):
            flush_paragraph()
        index += 1

    flush_paragraph()
    return "\n\n".join(block for block in blocks if block.strip())


def _is_heading_like_line(line: str) -> bool:
    return bool(_HEADING_PATTERN.match(line))


def _is_list_line(line: str) -> bool:
    return bool(_UNORDERED_LIST_PATTERN.match(line) or _ORDERED_LIST_PATTERN.match(line))


def _starts_new_block(line: str) -> bool:
    return bool(
        _is_heading_like_line(line)
        or _THEMATIC_BREAK_PATTERN.fullmatch(line)
        or line.startswith("|")
        or line.startswith(">")
        or _is_list_line(line)
        or _FENCE_DELIMITER_PATTERN.match(line)
    )


def _line_ends_paragraph(line: str) -> bool:
    stripped = line.rstrip()
    if not stripped:
        return True
    if stripped.endswith(("。", "！", "？", "；", "：", ".", "!", "?", ";", ":", "”", "\"", "」", "]", "）", ")")):
        return True
    if stripped.endswith("**"):
        return True
    return False


def _strip_section_separators(content: str) -> str:
    if not content:
        return ""
    lines = content.split("\n")
    while lines and _THEMATIC_BREAK_PATTERN.fullmatch(lines[0].strip()):
        lines.pop(0)
    while lines and _THEMATIC_BREAK_PATTERN.fullmatch(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()
