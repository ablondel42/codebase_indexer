"""Sound conservative ASCII trigram literal extractor and line-level regex matcher."""

import re

from core.models.exceptions import ValidationError
from core.models.search import CodeMatchItem, TextDocumentRecord

_MAX_PREVIEW_CHARS = 240
_MAX_TRIGRAM_CLAUSES = 6
_SIMPLE_ESCAPE_LETTERS: frozenset[str] = frozenset(set("bBdDsSwWZA"))
_REGEX_META_CHARS: frozenset[str] = frozenset(set(r"\^$.|?*+()[]{}"))


def compile_search_pattern(
    query: str,
    *,
    regex: bool,
    case_sensitive: bool,
) -> re.Pattern[str]:
    """Compile ``query`` into a Python ``re.Pattern`` or raise ``ValidationError``."""
    if not query:
        raise ValidationError("Search query must be non-empty.")
    flags = 0 if case_sensitive else re.IGNORECASE
    raw_pattern = query if regex else re.escape(query)
    try:
        return re.compile(raw_pattern, flags)
    except re.error as exc:
        raise ValidationError(
            f"Invalid regular expression {query!r}: {exc}",
            context={"query": query},
        ) from exc


def _skip_char_class(pattern: str, start_idx: int) -> int:
    """Return index right after the closing ``]`` of a character class starting at ``[``."""
    idx = start_idx + 1
    length = len(pattern)
    if idx < length and pattern[idx] == "^":
        idx += 1
    if idx < length and pattern[idx] == "]":
        idx += 1
    while idx < length:
        ch = pattern[idx]
        if ch == "\\":
            idx += 2
            continue
        if ch == "]":
            return idx + 1
        idx += 1
    return length


def _skip_brace_quantifier(pattern: str, start_idx: int) -> int:
    """Return index right after ``}`` if ``start_idx`` begins ``{...}``, else ``start_idx + 1``."""
    end_idx = pattern.find("}", start_idx + 1)
    return (end_idx + 1) if end_idx != -1 else (start_idx + 1)


def _flush_literal_buf(buf: list[str], runs: list[str]) -> None:
    """Append buffered literal characters to ``runs`` when at least 3 chars long."""
    if len(buf) >= 3:
        runs.append("".join(buf))
    buf.clear()


def _consume_backslash_escape(pattern: str, idx: int, depth: int) -> tuple[int, int, bool]:
    """Advance past a ``\\x`` escape or signal ``abort=True`` for hex/octal/backref escapes."""
    if idx + 1 >= len(pattern):
        return len(pattern), depth, True
    nxt = pattern[idx + 1]
    if nxt.isalnum() and nxt not in _SIMPLE_ESCAPE_LETTERS:
        return len(pattern), depth, True
    return idx + 2, depth, False


def _advance_regex_token(
    pattern: str,
    idx: int,
    depth: int,
    buf: list[str],
    runs: list[str],
) -> tuple[int, int, bool]:
    """Process one token in ``pattern``, returning ``(next_idx, next_depth, abort)``."""
    ch = pattern[idx]
    if ch == "\\":
        _flush_literal_buf(buf, runs)
        return _consume_backslash_escape(pattern, idx, depth)
    if ch == "[":
        _flush_literal_buf(buf, runs)
        return _skip_char_class(pattern, idx), depth, False
    if ch in "()":
        _flush_literal_buf(buf, runs)
        return idx + 1, depth + (1 if ch == "(" else -1), False
    if ch in "?*{":
        if buf:
            buf.pop()
        _flush_literal_buf(buf, runs)
        return (_skip_brace_quantifier(pattern, idx) if ch == "{" else idx + 1), depth, False

    is_plain_ascii = ch not in _REGEX_META_CHARS and 32 <= ord(ch) < 127
    if not is_plain_ascii:
        _flush_literal_buf(buf, runs)
    elif depth == 0:
        buf.append(ch)
    return idx + 1, depth, False


def _extract_regex_ascii_runs(pattern: str) -> tuple[str, ...]:
    """Extract provably required top-level ASCII literal runs ``>= 3`` chars from ``pattern``."""
    if "|" in pattern or "(?" in pattern or "\n" in pattern or "\r" in pattern:
        return ()

    runs: list[str] = []
    buf: list[str] = []
    idx = 0
    depth = 0
    while idx < len(pattern):
        idx, depth, abort = _advance_regex_token(pattern, idx, depth, buf, runs)
        if abort or depth < 0:
            return ()

    if depth != 0:
        return ()
    _flush_literal_buf(buf, runs)
    unique_sorted = sorted(dict.fromkeys(runs), key=len, reverse=True)
    return tuple(unique_sorted[:_MAX_TRIGRAM_CLAUSES])


def extract_required_ascii_trigrams(
    query: str,
    *,
    regex: bool,
    case_sensitive: bool,
) -> tuple[str, ...]:
    """Return sound ``>= 3``-char trigrams guaranteed present in every match of ``query``."""
    if "\n" in query or "\r" in query:
        return ()
    if not regex:
        if len(query) < 3:
            return ()
        if not query.isascii() and not case_sensitive:
            return ()
        return (query,)
    return _extract_regex_ascii_runs(query)


def _trim_preview(raw_line: str, match_col: int) -> str:
    """Trim ``raw_line`` around ``match_col`` to at most ``_MAX_PREVIEW_CHARS`` characters."""
    stripped = raw_line.strip()
    if len(stripped) <= _MAX_PREVIEW_CHARS:
        return stripped
    start = max(0, match_col - 60)
    window = raw_line[start : start + _MAX_PREVIEW_CHARS].strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if start + _MAX_PREVIEW_CHARS < len(raw_line) else ""
    return f"{prefix}{window}{suffix}"


def match_document_lines(
    doc: TextDocumentRecord,
    pattern: re.Pattern[str],
    *,
    context_lines: int = 0,
) -> list[CodeMatchItem]:
    """Scan ``doc.content`` line-by-line and build ``CodeMatchItem`` entries."""
    lines = doc.content.splitlines()
    items: list[CodeMatchItem] = []
    total = len(lines)

    for zero_idx, raw_line in enumerate(lines):
        found = pattern.search(raw_line)
        if found is None:
            continue
        before = (
            tuple(lines[max(0, zero_idx - context_lines) : zero_idx]) if context_lines > 0 else ()
        )
        after = (
            tuple(lines[zero_idx + 1 : min(total, zero_idx + 1 + context_lines)])
            if context_lines > 0
            else ()
        )
        items.append(
            CodeMatchItem(
                file_path=doc.file_path,
                language=doc.language,
                line=zero_idx + 1,
                column=found.start() + 1,
                preview=_trim_preview(raw_line, found.start()),
                context_before=before,
                context_after=after,
            )
        )
    return items


__all__ = [
    "compile_search_pattern",
    "extract_required_ascii_trigrams",
    "match_document_lines",
]
