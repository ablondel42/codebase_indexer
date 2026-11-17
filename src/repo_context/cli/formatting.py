"""ASCII table, symbol tree, and JSON output formatters for the rc CLI."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from typing import Any


def _json_default(value: Any) -> Any:
    """Serialize common domain types cleanly for ``json.dumps``."""
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def json_dumps(data: Any, *, indent: int | None = 2) -> str:
    """Serialize payload to JSON string."""
    return json.dumps(data, indent=indent, default=_json_default)


def print_json(data: Any) -> None:
    """Print JSON payload to stdout."""
    print(json_dumps(data, indent=2))


def format_bytes(num_bytes: int) -> str:
    """Format a byte size in human-readable form."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    value = float(num_bytes)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024.0 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{num_bytes} B"


def estimate_tokens(payload: str | bytes | int) -> tuple[int, int]:
    """Return ``(byte_count, estimated_tokens)`` using the ~bytes/4 heuristic."""
    if isinstance(payload, int):
        byte_count = max(0, payload)
    elif isinstance(payload, bytes):
        byte_count = len(payload)
    else:
        byte_count = len(payload.encode("utf-8"))
    if byte_count == 0:
        return 0, 0
    return byte_count, max(1, (byte_count + 3) // 4)


def truncate_text(value: Any, max_width: int = 80, *, empty: str = "-") -> str:
    """Flatten whitespace and truncate text to ``max_width`` characters."""
    if value is None:
        return empty
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    collapsed = " ".join(text.split())
    if not collapsed:
        return empty
    if max_width > 3 and len(collapsed) > max_width:
        return collapsed[: max_width - 3] + "..."
    if max_width > 0 and len(collapsed) > max_width:
        return collapsed[:max_width]
    return collapsed


def format_line_range(
    start_line: int | None = None,
    end_line: int | None = None,
    lines: str | None = None,
) -> str:
    """Format a line range as ``Lstart-end`` or ``Lline``."""
    if lines:
        return lines if lines.startswith("L") else f"L{lines}"
    if isinstance(start_line, int) and isinstance(end_line, int):
        return f"L{start_line}-{end_line}"
    if isinstance(start_line, int):
        return f"L{start_line}"
    return "-"


def format_location(
    file_path: str | None,
    start_line: int | None = None,
    end_line: int | None = None,
    lines: str | None = None,
) -> str:
    """Format a symbol or edge location as ``file_path:Lstart-end``."""
    span = format_line_range(start_line=start_line, end_line=end_line, lines=lines)
    if file_path and span != "-":
        return f"{file_path}:{span}"
    if file_path:
        return file_path
    return span


def format_ascii_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    max_col_widths: Sequence[int | None] | None = None,
    alignments: Sequence[str] | None = None,
    empty_message: str = "(none)",
) -> str:
    """Format tabular data as a bordered ASCII table."""
    if not headers:
        return empty_message
    if not rows:
        return empty_message

    col_count = len(headers)
    normalized_rows: list[list[str]] = []
    for row in rows:
        formatted_row: list[str] = []
        for idx in range(col_count):
            cell = row[idx] if idx < len(row) else None
            cap = max_col_widths[idx] if max_col_widths and idx < len(max_col_widths) else None
            if cell is None:
                text = "-"
            elif isinstance(cell, bool):
                text = "true" if cell else "false"
            elif cap is not None:
                text = truncate_text(cell, max_width=cap)
            else:
                text = " ".join(str(cell).split()) or "-"
            formatted_row.append(text)
        normalized_rows.append(formatted_row)

    widths = [len(str(h)) for h in headers]
    for row in normalized_rows:
        for idx, cell in enumerate(row):
            if len(cell) > widths[idx]:
                widths[idx] = len(cell)

    if max_col_widths:
        for idx, cap in enumerate(max_col_widths):
            if cap is not None:
                widths[idx] = min(widths[idx], max(len(str(headers[idx])), cap))

    border = "+-" + "-+-".join("-" * w for w in widths) + "-+"

    def _pad(text: str, width: int, align: str) -> str:
        if align == "right":
            return text.rjust(width)
        return text.ljust(width)

    header_cells = [
        _pad(str(headers[idx]), widths[idx], "left") for idx in range(col_count)
    ]
    lines = [border, "| " + " | ".join(header_cells) + " |", border]

    for row in normalized_rows:
        cells = []
        for idx in range(col_count):
            align = alignments[idx] if alignments and idx < len(alignments) else "left"
            cells.append(_pad(row[idx], widths[idx], align))
        lines.append("| " + " | ".join(cells) + " |")

    lines.append(border)
    return "\n".join(lines)


def format_key_value_section(
    title: str,
    items: Sequence[tuple[str, Any]],
) -> str:
    """Format a titled key-value metadata block."""
    lines = [title, "-" * len(title)]
    if not items:
        lines.append("(none)")
        return "\n".join(lines)

    key_width = max(len(k) for k, _ in items)
    for key, value in items:
        display = "-" if value is None else str(value)
        lines.append(f"  {key.ljust(key_width)} : {display}")
    return "\n".join(lines)


def format_outline_tree(outline: dict[str, Any]) -> str:
    """Format a ``build_file_outline`` dictionary as an indented ASCII symbol tree."""
    is_fresh = bool(outline.get("is_fresh", True))
    stale_reason = outline.get("stale_reason")
    freshness = "fresh" if is_fresh else (f"stale ({stale_reason})" if stale_reason else "stale")

    lines: list[str] = [
        f"File: {outline.get('file_path', '-')} "
        f"(module: {outline.get('module_path', '-')}, "
        f"{outline.get('line_count', 0)} lines, {freshness})"
    ]

    doc = outline.get("doc")
    if doc:
        lines.append(f"Doc: {truncate_text(doc, max_width=100)}")

    imports = outline.get("imports") or []
    if imports:
        lines.append(f"Imports ({len(imports)}): {', '.join(imports)}")

    symbols = outline.get("symbols") or []
    lines.append(f"Symbols ({len(symbols)} top-level):")
    if not symbols:
        lines.append("  (none)")
        return "\n".join(lines)

    def _render_nodes(nodes: list[dict[str, Any]], prefix: str) -> None:
        for idx, node in enumerate(nodes):
            is_last = idx == len(nodes) - 1
            branch = "`-- " if is_last else "|-- "
            child_prefix = prefix + ("    " if is_last else "|   ")

            span = format_line_range(lines=node.get("lines"))
            kind = node.get("kind", "unknown")
            label = node.get("signature") or node.get("name") or "?"
            node_id = node.get("id", "?")
            lines.append(f"{prefix}{branch}[{span}] {kind} {truncate_text(label, max_width=100)}  [{node_id}]")

            children = node.get("children") or []
            if children:
                _render_nodes(children, child_prefix)

    _render_nodes(symbols, "")
    return "\n".join(lines)
