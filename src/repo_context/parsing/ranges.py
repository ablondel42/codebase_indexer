"""Range helpers for converting AST locations to zero-based LSP/storage ranges."""

import ast
import re
from collections.abc import Sequence
from typing import Any

_DECL_NAME_RE = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")


def to_zero_based_line(line: int | None) -> int | None:
    """Convert AST one-based line number to zero-based.

    Args:
        line: One-based line number from AST.

    Returns:
        Zero-based line number, or None if input is None.
    """
    if line is None:
        return None
    return line - 1


def make_position(line: int | None, character: int | None) -> dict[str, int] | None:
    """Create a position dictionary.

    Args:
        line: Zero-based line number.
        character: Zero-based character offset.

    Returns:
        Position dict with ``line`` and ``character`` keys, or None if either is None.
    """
    if line is None or character is None:
        return None
    return {"line": line, "character": character}


def utf8_byte_offset_to_char_offset(source_line: str, byte_offset: int) -> int:
    """Convert an AST UTF-8 byte column offset to a Unicode character index.

    Args:
        source_line: Line text without trailing newline.
        byte_offset: UTF-8 byte offset within ``source_line``.

    Returns:
        Zero-based Unicode codepoint character offset.
    """
    if byte_offset <= 0:
        return 0
    encoded = source_line.encode("utf-8")
    clamped = min(byte_offset, len(encoded))
    return len(encoded[:clamped].decode("utf-8", errors="replace"))


def _get_line(lines: Sequence[str] | None, lineno: int | None) -> str | None:
    """Return the 1-based line from ``lines`` when valid."""
    if lines is None or lineno is None or lineno < 1 or lineno > len(lines):
        return None
    return lines[lineno - 1]


def _col_to_char(byte_col: int | None, line_text: str | None) -> int | None:
    """Convert a byte column to a character index when ``line_text`` is available."""
    if byte_col is None:
        return None
    if line_text is None:
        return byte_col
    return utf8_byte_offset_to_char_offset(line_text, byte_col)


def make_declaration_lines(node: ast.AST) -> tuple[int, int]:
    """Compute 1-based inclusive ``(start_line, end_line)`` for a declaration.

    For decorated classes and callables, ``start_line`` begins at the first
    decorator line so hashes and spans cover the entire declaration.

    Args:
        node: AST declaration node.

    Returns:
        Tuple of 1-based inclusive ``(start_line, end_line)``.
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
    return int(start_line), int(end_line)


def make_range(node: ast.AST, lines: Sequence[str] | None = None) -> dict[str, Any] | None:
    """Create a zero-based range dictionary from an AST node.

    Args:
        node: AST node with location metadata.
        lines: Optional pre-split source lines for UTF-8 byte to char conversion.

    Returns:
        Range dict with ``start`` and ``end`` positions, or None if metadata missing.
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

    return {"start": start, "end": end}


def make_name_selection_range(
    node: ast.AST,
    source_line: str | None = None,
    *,
    lines: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Create a name-focused zero-based selection range from an AST node.

    For ``ClassDef``, ``FunctionDef``, and ``AsyncFunctionDef``, converts the
    AST UTF-8 byte ``col_offset`` to a Unicode character offset and matches the
    declared identifier after ``def`` / ``async def`` / ``class`` so names like
    ``async def sync()`` are never mis-positioned inside ``async``.

    Args:
        node: AST declaration or identifier node.
        source_line: Optional source line text for ``node.lineno``.
        lines: Optional pre-split source lines (used when ``source_line`` is None).

    Returns:
        Selection range dict, or None if location metadata is missing.
    """
    raw_name = getattr(node, "name", None)
    # Python 3.12+ ast.TypeAlias has .name as an ast.Name node rather than str.
    if isinstance(raw_name, ast.AST):
        return make_name_selection_range(raw_name, source_line, lines=lines)

    lineno = getattr(node, "lineno", None)
    col_offset = getattr(node, "col_offset", None)
    if lineno is None or col_offset is None:
        return None

    if source_line is None:
        source_line = _get_line(lines, lineno)

    if isinstance(node, ast.Name):
        name = node.id
        start_char = _col_to_char(col_offset, source_line)
        end_col = getattr(node, "end_col_offset", None)
        end_char = _col_to_char(end_col, source_line) if end_col is not None else None
        if start_char is None:
            return None
        if end_char is None or getattr(node, "end_lineno", lineno) != lineno:
            end_char = start_char + len(name)
        return {
            "start": make_position(to_zero_based_line(lineno), start_char),
            "end": make_position(to_zero_based_line(lineno), end_char),
        }

    if isinstance(node, ast.Attribute):
        name = node.attr
    else:
        name = raw_name if isinstance(raw_name, str) else getattr(node, "id", None)

    if not name:
        return None

    if source_line is not None:
        char_col = utf8_byte_offset_to_char_offset(source_line, col_offset)
        tail = source_line[char_col:]
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            match = _DECL_NAME_RE.match(tail)
            if match is not None and match.group(1) == name:
                start_char = char_col + match.start(1)
                end_char = char_col + match.end(1)
                return {
                    "start": make_position(to_zero_based_line(lineno), start_char),
                    "end": make_position(to_zero_based_line(lineno), end_char),
                }
        word_match = re.search(rf"\b{re.escape(name)}\b", tail)
        if word_match is not None:
            start_char = char_col + word_match.start()
            end_char = char_col + word_match.end()
        else:
            start_char = char_col
            end_char = char_col + len(name)
    else:
        start_char = col_offset
        end_char = col_offset + len(name)

    start = make_position(to_zero_based_line(lineno), start_char)
    end = make_position(to_zero_based_line(lineno), end_char)
    if start is None or end is None:
        return None

    return {"start": start, "end": end}
