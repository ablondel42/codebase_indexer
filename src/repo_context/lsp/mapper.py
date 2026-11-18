"""LSP location mapper - range containment, UTF-16 offsets, and symbol resolution."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any


def normalize_location(location: Any) -> dict[str, Any]:
    """Normalize an LSP Location or LocationLink dict/object to a consistent dict shape."""
    if isinstance(location, dict):
        if "uri" in location and "range" in location:
            raw_range = location["range"]
            uri = location["uri"]
        else:
            uri = location.get("targetUri") or location.get("uri")
            raw_range = (
                location.get("targetSelectionRange")
                or location.get("targetRange")
                or location.get("range")
            )
        if not isinstance(raw_range, dict):
            raise ValueError(f"Invalid LSP location range: {location!r}")
        return {
            "uri": uri,
            "range": {
                "start": {
                    "line": int(raw_range["start"]["line"]),
                    "character": int(raw_range["start"]["character"]),
                },
                "end": {
                    "line": int(raw_range["end"]["line"]),
                    "character": int(raw_range["end"]["character"]),
                },
            },
        }

    uri = getattr(location, "uri", None) or getattr(location, "target_uri", None)
    rng = (
        getattr(location, "range", None)
        or getattr(location, "target_selection_range", None)
        or getattr(location, "target_range", None)
    )
    if rng is None:
        raise ValueError(f"Invalid LSP location object (missing range): {location!r}")
    return {
        "uri": uri,
        "range": {
            "start": {
                "line": int(rng.start.line),
                "character": int(rng.start.character),
            },
            "end": {
                "line": int(rng.end.line),
                "character": int(rng.end.character),
            },
        },
    }


def codepoint_offset_to_utf16(line_text: str, codepoint_offset: int) -> int:
    """Convert a 0-based Python codepoint offset on a line to a UTF-16 code-unit offset."""
    if codepoint_offset <= 0 or not line_text:
        return 0
    if line_text.isascii():
        return min(codepoint_offset, len(line_text))
    prefix = line_text[:codepoint_offset]
    return len(prefix.encode("utf-16-le")) // 2


def utf16_offset_to_codepoint(line_text: str, utf16_offset: int) -> int:
    """Convert a 0-based UTF-16 code-unit offset on a line to a Python codepoint offset."""
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


def position_codepoint_to_utf16(lines: Sequence[str], position: dict[str, int]) -> dict[str, int]:
    """Convert a 0-based position from Python codepoint offsets to UTF-16 offsets."""
    line = int(position["line"])
    character = int(position["character"])
    if 0 <= line < len(lines):
        character = codepoint_offset_to_utf16(lines[line], character)
    return {"line": line, "character": character}


def position_utf16_to_codepoint(lines: Sequence[str], position: dict[str, int]) -> dict[str, int]:
    """Convert a 0-based position from UTF-16 offsets to Python codepoint offsets."""
    line = int(position["line"])
    character = int(position["character"])
    if 0 <= line < len(lines):
        character = utf16_offset_to_codepoint(lines[line], character)
    return {"line": line, "character": character}


def range_utf16_to_codepoint(lines: Sequence[str], lsp_range: dict[str, Any]) -> dict[str, Any]:
    """Convert a 0-based LSP range from UTF-16 offsets to Python codepoint offsets."""
    return {
        "start": position_utf16_to_codepoint(lines, lsp_range["start"]),
        "end": position_utf16_to_codepoint(lines, lsp_range["end"]),
    }


def _parse_range(range_data: Any) -> dict[str, Any] | None:
    """Parse range from JSON string or dict."""
    if range_data is None:
        return None
    if isinstance(range_data, str):
        try:
            range_data = json.loads(range_data)
        except json.JSONDecodeError:
            return None
    if not _is_valid_range(range_data):
        return None
    return range_data


def _is_valid_position(pos: Any) -> bool:
    """Check whether a position dict has integer line and character fields."""
    if not isinstance(pos, dict):
        return False
    line = pos.get("line")
    character = pos.get("character")
    return (
        isinstance(line, int)
        and not isinstance(line, bool)
        and isinstance(character, int)
        and not isinstance(character, bool)
    )


def _is_valid_range(rng: Any) -> bool:
    """Check whether a range dict has valid start and end positions."""
    return (
        isinstance(rng, dict)
        and _is_valid_position(rng.get("start"))
        and _is_valid_position(rng.get("end"))
    )


def _pos_le(a: dict[str, int], b: dict[str, int]) -> bool:
    """Check if position a <= position b."""
    return (a["line"], a["character"]) <= (b["line"], b["character"])


def _pos_ge(a: dict[str, int], b: dict[str, int]) -> bool:
    """Check if position a >= position b."""
    return (a["line"], a["character"]) >= (b["line"], b["character"])


def range_contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    """Check if outer range contains inner range."""
    if not (_is_valid_range(outer) and _is_valid_range(inner)):
        return False
    return _pos_le(outer["start"], inner["start"]) and _pos_ge(outer["end"], inner["end"])


def _range_span_key(r: dict[str, Any]) -> tuple[int, int]:
    """Get a sort key for range span (smaller = more specific)."""
    return (
        r["end"]["line"] - r["start"]["line"],
        r["end"]["character"] - r["start"]["character"],
    )


def pick_smallest_containing_symbol(
    symbols_in_file: Sequence[dict[str, Any]],
    usage_range: dict[str, Any],
) -> dict[str, Any] | None:
    """Find the smallest symbol containing the usage range.

    Prefers the narrowest containing span, breaking exact range ties in favor
    of non-module symbols before falling back to symbol ID.

    Args:
        symbols_in_file: Sequence of symbol dicts for a file.
        usage_range: The usage location range.

    Returns:
        The smallest containing symbol dict, or None if none found.
    """
    if not _is_valid_range(usage_range):
        return None

    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for sym in symbols_in_file:
        range_json = _parse_range(sym.get("range_json"))
        if range_json and range_contains(range_json, usage_range):
            candidates.append((sym, range_json))

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            _range_span_key(item[1]),
            1 if item[0].get("kind") == "module" else 0,
            item[0]["id"],
        )
    )
    return candidates[0][0]


def find_module_node_for_file(symbols_in_file: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the module node in a sequence of symbols."""
    for symbol in symbols_in_file:
        if symbol.get("kind") == "module":
            return symbol
    return None
