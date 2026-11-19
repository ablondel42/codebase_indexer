"""Pure AST location helpers returning typed ``Position``, ``LineSpan``, and ``Range``."""

import ast
import re
from collections.abc import Sequence
from typing import Any

from core.models.range import LineSpan, Position, Range

_DECL_NAME_RE = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")


def to_zero_based_line(line: int | None) -> int | None:
    """Convert an AST 1-based line number to a 0-based line index.

    Args:
        line: One-based line number from Python ``ast``, or ``None``.

    Returns:
        Zero-based line number, or ``None`` if ``line`` is ``None``.
    """
    if line is None:
        return None
    return line - 1


def utf8_byte_offset_to_char_offset(source_line: str, byte_offset: int) -> int:
    """Convert an AST UTF-8 byte column offset to a Unicode codepoint character index.

    Args:
        source_line: Source line text without trailing newline.
        byte_offset: Zero-based UTF-8 byte offset within ``source_line``.

    Returns:
        Zero-based Unicode codepoint character index.
    """
    if byte_offset <= 0:
        return 0
    encoded = source_line.encode("utf-8")
    clamped = min(byte_offset, len(encoded))
    return len(encoded[:clamped].decode("utf-8", errors="replace"))


def make_position(line: int | None, character: int | None) -> Position | None:
    """Build a typed ``Position`` when both 0-based coordinates are valid.

    Args:
        line: Zero-based line index, or ``None``.
        character: Zero-based character offset, or ``None``.

    Returns:
        ``Position`` model, or ``None`` if either coordinate is ``None`` or negative.
    """
    if line is None or character is None or line < 0 or character < 0:
        return None
    return Position(line=line, character=character)


def _get_line(lines: Sequence[str] | None, lineno: int | None) -> str | None:
    """Return the 1-based line from ``lines`` when within bounds.

    Args:
        lines: Optional pre-split source lines.
        lineno: One-based line number.

    Returns:
        Matching source line string or ``None``.
    """
    if lines is None or lineno is None or lineno < 1 or lineno > len(lines):
        return None
    return lines[lineno - 1]


def _col_to_char(byte_col: int | None, line_text: str | None) -> int | None:
    """Convert a UTF-8 byte column to a character index when ``line_text`` is provided.

    Args:
        byte_col: Raw AST ``col_offset`` or ``end_col_offset``.
        line_text: Optional source line text for UTF-8 conversion.

    Returns:
        Zero-based character offset, or ``None`` if ``byte_col`` is ``None``.
    """
    if byte_col is None:
        return None
    if line_text is None:
        return byte_col
    return utf8_byte_offset_to_char_offset(line_text, byte_col)


def make_declaration_lines(node: ast.AST) -> LineSpan:
    """Compute 1-based inclusive ``LineSpan`` for an AST declaration node.

    For decorated classes and callables, ``start_line`` begins at the first
    decorator line so content hashes and source slices cover decorators.

    Args:
        node: AST declaration node carrying ``lineno``.

    Returns:
        Typed ``LineSpan(start_line=..., end_line=...)``.

    Raises:
        ValueError: If ``node`` lacks a ``lineno`` attribute.
    """
    lineno = getattr(node, "lineno", None)
    if lineno is None:
        raise ValueError(f"AST node {type(node).__name__} has no lineno")

    decorators = getattr(node, "decorator_list", None) or []
    if decorators:
        first_dec_line = getattr(decorators[0], "lineno", None)
        start_line = min(lineno, first_dec_line) if first_dec_line is not None else lineno
    else:
        start_line = lineno

    end_lineno = getattr(node, "end_lineno", None)
    end_line = end_lineno if end_lineno is not None else lineno
    resolved_start = max(int(start_line), 1)
    resolved_end = max(int(end_line), resolved_start)
    return LineSpan(start_line=resolved_start, end_line=resolved_end)


def make_range(node: ast.AST, lines: Sequence[str] | None = None) -> Range | None:
    """Create a zero-based ``Range`` from an AST node's start/end coordinates.

    Args:
        node: AST node with location attributes.
        lines: Optional pre-split source lines for UTF-8 byte-to-character conversion.

    Returns:
        Typed ``Range`` model, or ``None`` if location metadata is incomplete.
    """
    lineno = getattr(node, "lineno", None)
    col_offset = getattr(node, "col_offset", None)
    end_lineno = getattr(node, "end_lineno", None)
    end_col_offset = getattr(node, "end_col_offset", None)

    if lineno is None or col_offset is None:
        return None

    start_char = _col_to_char(col_offset, _get_line(lines, lineno))
    end_char = _col_to_char(end_col_offset, _get_line(lines, end_lineno))

    start = make_position(to_zero_based_line(lineno), start_char)
    end = make_position(to_zero_based_line(end_lineno), end_char)
    if start is None or end is None:
        return None
    return Range(start=start, end=end)


def _make_single_line_range(
    lineno: int,
    start_char: int | None,
    end_char: int | None,
) -> Range | None:
    """Construct a single-line ``Range`` when both character bounds are valid.

    Args:
        lineno: One-based line number.
        start_char: Zero-based start character index.
        end_char: Zero-based end character index.

    Returns:
        Typed ``Range`` or ``None``.
    """
    zero_line = to_zero_based_line(lineno)
    start = make_position(zero_line, start_char)
    end = make_position(zero_line, end_char)
    if start is None or end is None:
        return None
    return Range(start=start, end=end)


def _selection_range_for_name_node(
    node: ast.Name,
    lineno: int,
    col_offset: int,
    source_line: str | None,
) -> Range | None:
    """Compute a name selection ``Range`` directly for an ``ast.Name`` node.

    Args:
        node: ``ast.Name`` target node.
        lineno: One-based line number.
        col_offset: Zero-based UTF-8 byte column offset.
        source_line: Optional source line text.

    Returns:
        Typed ``Range`` covering the identifier, or ``None``.
    """
    start_char = _col_to_char(col_offset, source_line)
    if start_char is None:
        return None
    end_col = getattr(node, "end_col_offset", None)
    end_char = _col_to_char(end_col, source_line) if end_col is not None else None
    if end_char is None or getattr(node, "end_lineno", lineno) != lineno:
        end_char = start_char + len(node.id)
    return _make_single_line_range(lineno, start_char, end_char)


def _extract_node_identifier(node: ast.AST, raw_name: Any) -> str | None:
    """Extract simple identifier text from a declaration or attribute AST node.

    Args:
        node: AST node being inspected.
        raw_name: Value of ``getattr(node, 'name', None)``.

    Returns:
        Non-empty identifier string, or ``None``.
    """
    if isinstance(node, ast.Attribute):
        return node.attr or None
    name = raw_name if isinstance(raw_name, str) else getattr(node, "id", None)
    return name if isinstance(name, str) and name else None


def _compute_identifier_char_span(
    node: ast.AST,
    name: str,
    col_offset: int,
    source_line: str | None,
) -> tuple[int, int]:
    """Compute zero-based ``(start_char, end_char)`` for ``name`` on ``source_line``.

    Args:
        node: AST declaration node.
        name: Declared identifier string.
        col_offset: Zero-based UTF-8 byte column offset of ``node``.
        source_line: Optional source line text.

    Returns:
        Zero-based character span ``(start_char, end_char)``.
    """
    if source_line is None:
        return col_offset, col_offset + len(name)

    char_col = utf8_byte_offset_to_char_offset(source_line, col_offset)
    tail = source_line[char_col:]
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        match = _DECL_NAME_RE.match(tail)
        if match is not None and match.group(1) == name:
            return char_col + match.start(1), char_col + match.end(1)

    word_match = re.search(rf"\b{re.escape(name)}\b", tail)
    if word_match is not None:
        return char_col + word_match.start(), char_col + word_match.end()
    return char_col, char_col + len(name)


def make_name_selection_range(
    node: ast.AST,
    source_line: str | None = None,
    *,
    lines: Sequence[str] | None = None,
) -> Range | None:
    """Create a name-focused zero-based selection ``Range`` from an AST node.

    For ``ClassDef``, ``FunctionDef``, and ``AsyncFunctionDef``, converts the
    UTF-8 byte ``col_offset`` to a character index and matches the identifier
    following ``class`` / ``def`` / ``async def`` so ``async def sync()`` selects
    ``sync`` rather than ``async``.

    Args:
        node: AST declaration or identifier node.
        source_line: Optional source line text for ``node.lineno``.
        lines: Optional pre-split source lines used when ``source_line`` is ``None``.

    Returns:
        Typed ``Range`` covering the symbol identifier, or ``None`` if unavailable.
    """
    raw_name = getattr(node, "name", None)
    if isinstance(raw_name, ast.AST):
        return make_name_selection_range(raw_name, source_line, lines=lines)

    lineno = getattr(node, "lineno", None)
    col_offset = getattr(node, "col_offset", None)
    if lineno is None or col_offset is None:
        return None

    resolved_line = source_line if source_line is not None else _get_line(lines, lineno)
    if isinstance(node, ast.Name):
        return _selection_range_for_name_node(node, lineno, col_offset, resolved_line)

    name = _extract_node_identifier(node, raw_name)
    if not name:
        return None

    start_char, end_char = _compute_identifier_char_span(node, name, col_offset, resolved_line)
    return _make_single_line_range(lineno, start_char, end_char)


__all__ = [
    "make_declaration_lines",
    "make_name_selection_range",
    "make_position",
    "make_range",
    "to_zero_based_line",
    "utf8_byte_offset_to_char_offset",
]
