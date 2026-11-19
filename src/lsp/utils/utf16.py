"""Pure UTF-16 code-unit and Python Unicode codepoint offset conversion utilities."""

from collections.abc import Sequence

from core.models.range import Position, Range


def codepoint_offset_to_utf16(line_text: str, codepoint_offset: int) -> int:
    """Convert a zero-based Python codepoint offset on a line to a UTF-16 code-unit offset.

    Args:
        line_text: Single line of decoded UTF-8 source text.
        codepoint_offset: Zero-based Python character (codepoint) index on ``line_text``.

    Returns:
        Corresponding zero-based UTF-16 code-unit offset.
    """
    if codepoint_offset <= 0 or not line_text:
        return 0
    if line_text.isascii():
        return min(codepoint_offset, len(line_text))
    prefix = line_text[:codepoint_offset]
    return len(prefix.encode("utf-16-le")) // 2


def utf16_offset_to_codepoint(line_text: str, utf16_offset: int) -> int:
    """Convert a zero-based UTF-16 code-unit offset on a line to a Python codepoint offset.

    Args:
        line_text: Single line of decoded UTF-8 source text.
        utf16_offset: Zero-based UTF-16 code-unit offset reported by an LSP server.

    Returns:
        Corresponding zero-based Python character (codepoint) index on ``line_text``.
    """
    if utf16_offset <= 0 or not line_text:
        return 0
    if line_text.isascii():
        return min(utf16_offset, len(line_text))
    units = 0
    for idx, ch in enumerate(line_text):
        if units >= utf16_offset:
            return idx
        step = 2 if ord(ch) > 0xFFFF else 1
        if units + step > utf16_offset:
            return idx
        units += step
    return len(line_text)


def position_codepoint_to_utf16(lines: Sequence[str], position: Position) -> Position:
    """Convert a zero-based ``Position`` from Python codepoint offsets to UTF-16 offsets.

    Args:
        lines: Split source lines of the document.
        position: Zero-based codepoint ``Position`` to convert.

    Returns:
        Converted zero-based UTF-16 ``Position``.
    """
    character = position.character
    if 0 <= position.line < len(lines):
        character = codepoint_offset_to_utf16(lines[position.line], character)
    return Position(line=position.line, character=character)


def position_utf16_to_codepoint(lines: Sequence[str], position: Position) -> Position:
    """Convert a zero-based ``Position`` from UTF-16 offsets to Python codepoint offsets.

    Args:
        lines: Split source lines of the document.
        position: Zero-based UTF-16 ``Position`` to convert.

    Returns:
        Converted zero-based Python codepoint ``Position``.
    """
    character = position.character
    if 0 <= position.line < len(lines):
        character = utf16_offset_to_codepoint(lines[position.line], character)
    return Position(line=position.line, character=character)


def range_utf16_to_codepoint(lines: Sequence[str], lsp_range: Range) -> Range:
    """Convert a zero-based LSP ``Range`` from UTF-16 offsets to Python codepoint offsets.

    Args:
        lines: Split source lines of the document.
        lsp_range: Zero-based UTF-16 ``Range`` reported by an LSP server.

    Returns:
        Converted zero-based Python codepoint ``Range``.
    """
    return Range(
        start=position_utf16_to_codepoint(lines, lsp_range.start),
        end=position_utf16_to_codepoint(lines, lsp_range.end),
    )
