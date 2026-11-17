"""Module node extraction from Python AST."""

import ast
import json
from collections.abc import Sequence
from pathlib import Path

from repo_context.models.file import FileRecord
from repo_context.parsing.docstrings import get_doc_summary
from repo_context.parsing.naming import DuplicateTracker, build_module_qualified_name
from repo_context.parsing.scope_tracker import ScopeTracker


def extract_module_node(
    repo_id: str,
    file_record: FileRecord,
    tree: ast.Module,
    file_text: str,
    scope_tracker: ScopeTracker | None = None,
    duplicate_tracker: DuplicateTracker | None = None,
    *,
    lines: Sequence[str] | None = None,
) -> dict:
    """Extract a module node from a parsed Python file.

    Args:
        repo_id: Repository ID.
        file_record: File record for the source module.
        tree: Parsed AST module.
        file_text: Full file content as string.
        scope_tracker: Optional scope tracker (kept for API compatibility).
        duplicate_tracker: Optional duplicate tracker for symbol ID disambiguation.
        lines: Optional pre-split lines from ``file_text``.

    Returns:
        Module node dictionary ready for persistence.
    """
    del scope_tracker  # Module sits at the root of the lexical scope stack.
    module_path = file_record.module_path
    qualified_name = build_module_qualified_name(module_path)

    if module_path:
        name = module_path.split(".")[-1]
    else:
        name = Path(file_record.file_path).stem

    if "." in module_path:
        package_path = ".".join(module_path.split(".")[:-1])
    else:
        package_path = ""

    file_lines = lines if lines is not None else file_text.splitlines()
    if file_lines:
        last_line = len(file_lines) - 1
        last_char = len(file_lines[-1])
        start_line = 1
        end_line = len(file_lines)
    else:
        last_line = 0
        last_char = 0
        start_line = 1
        end_line = 1

    if duplicate_tracker is not None:
        node_id = duplicate_tracker.get_symbol_id(repo_id, "module", qualified_name)
    else:
        node_id = f"sym:{repo_id}:module:{qualified_name}"

    return {
        "id": node_id,
        "repo_id": repo_id,
        "file_id": file_record.id,
        "language": "python",
        "kind": "module",
        "name": name,
        "qualified_name": qualified_name,
        "uri": file_record.uri,
        "range_json": {
            "start": {"line": 0, "character": 0},
            "end": {"line": last_line, "character": last_char},
        },
        "selection_range_json": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 0},
        },
        "start_line": start_line,
        "end_line": end_line,
        "signature": None,
        "parent_id": None,
        "visibility_hint": "module",
        "doc_summary": get_doc_summary(tree),
        "content_hash": file_record.content_hash,
        "semantic_hash": file_record.content_hash,
        "source": "python-ast",
        "confidence": 1.0,
        "scope": "module",
        "lexical_parent_id": None,
        "payload_json": json.dumps(
            {
                "file_path": file_record.file_path,
                "module_path": module_path,
                "package_path": package_path,
                "imported_modules": [],
                "imported_symbols": [],
                "top_level_symbol_ids": [],
            },
            sort_keys=True,
        ),
        "last_indexed_at": file_record.last_indexed_at,
    }
