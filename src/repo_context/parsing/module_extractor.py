"""Module node extraction from Python AST."""

import ast
import json
from collections.abc import Sequence
from pathlib import Path

from repo_context.parsing.docstrings import get_doc_summary
from repo_context.parsing.extraction_context import ExtractionContext
from repo_context.parsing.naming import build_module_qualified_name


def _compute_module_span(file_lines: Sequence[str]) -> tuple[int, int, int]:
    """Return ``(last_line_zero_based, last_char, end_line_one_based)`` for module lines."""
    if file_lines:
        return len(file_lines) - 1, len(file_lines[-1]), len(file_lines)
    return 0, 0, 1


def extract_module_node(
    ctx: ExtractionContext,
    tree: ast.Module,
    file_text: str = "",
) -> dict:
    """Extract a module node from a parsed Python file.

    Args:
        ctx: Shared per-file extraction context.
        tree: Parsed AST module.
        file_text: Full file content fallback when ``ctx.lines`` is None.

    Returns:
        Module node dictionary ready for persistence.
    """
    qualified_name = build_module_qualified_name(ctx.module_path)
    name = (
        ctx.module_path.split(".")[-1]
        if ctx.module_path
        else Path(ctx.file_record.file_path).stem
    )
    package_path = (
        ".".join(ctx.module_path.split(".")[:-1]) if "." in ctx.module_path else ""
    )

    file_lines = ctx.lines if ctx.lines is not None else file_text.splitlines()
    last_line, last_char, end_line = _compute_module_span(file_lines)
    node_id = ctx.duplicate_tracker.get_symbol_id(ctx.repo_id, "module", qualified_name)

    return {
        "id": node_id,
        "repo_id": ctx.repo_id,
        "file_id": ctx.file_record.id,
        "language": "python",
        "kind": "module",
        "name": name,
        "qualified_name": qualified_name,
        "uri": ctx.file_record.uri,
        "range_json": {
            "start": {"line": 0, "character": 0},
            "end": {"line": last_line, "character": last_char},
        },
        "selection_range_json": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 0},
        },
        "start_line": 1,
        "end_line": end_line,
        "signature": None,
        "parent_id": None,
        "visibility_hint": "module",
        "doc_summary": get_doc_summary(tree),
        "content_hash": ctx.file_record.content_hash,
        "semantic_hash": ctx.file_record.content_hash,
        "source": "python-ast",
        "confidence": 1.0,
        "scope": "module",
        "lexical_parent_id": None,
        "payload_json": json.dumps(
            {
                "file_path": ctx.file_record.file_path,
                "module_path": ctx.module_path,
                "package_path": package_path,
                "imported_modules": [],
                "imported_symbols": [],
                "top_level_symbol_ids": [],
            },
            sort_keys=True,
        ),
        "last_indexed_at": ctx.file_record.last_indexed_at,
    }
