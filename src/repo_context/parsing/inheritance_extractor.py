"""Inheritance edge extraction from Python AST."""

import ast
import json
from collections.abc import Sequence

from repo_context.constants import EDGE_KIND_INHERITS
from repo_context.models.file import FileRecord
from repo_context.parsing.naming import build_nested_qualified_name, safe_unparse
from repo_context.parsing.ranges import make_declaration_lines, make_range


def _collect_class_asts(
    tree: ast.Module,
    module_path: str,
) -> tuple[dict[tuple[str, int], ast.ClassDef], dict[str, ast.ClassDef]]:
    """Walk AST recursively and index all ``ast.ClassDef`` nodes by qualified name and line."""
    by_qname_line: dict[tuple[str, int], ast.ClassDef] = {}
    by_qname: dict[str, ast.ClassDef] = {}

    def _walk(stmts: Sequence[ast.stmt], chain: list[str]) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.ClassDef):
                qname = build_nested_qualified_name(module_path, chain, stmt.name)
                start_line, _ = make_declaration_lines(stmt)
                by_qname_line[(qname, start_line)] = stmt
                by_qname.setdefault(qname, stmt)
                chain.append(stmt.name)
                _walk(stmt.body, chain)
                chain.pop()
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chain.append(stmt.name)
                _walk(stmt.body, chain)
                chain.pop()

    _walk(tree.body, [])
    return by_qname_line, by_qname


def extract_inheritance_edges(
    repo_id: str,
    file_record: FileRecord,
    class_nodes: list[dict],
    tree: ast.Module,
    *,
    lines: Sequence[str] | None = None,
) -> list[dict]:
    """Extract inheritance edges for all classes (top-level, nested, and local).

    Matches AST ``ClassDef`` nodes by ``(qualified_name, start_line)`` so nested
    and same-named local classes never overwrite each other.

    Args:
        repo_id: Repository ID.
        file_record: File record for the source module.
        class_nodes: List of extracted class node dicts.
        tree: Parsed AST module.
        lines: Optional pre-split source lines for exact base ranges.

    Returns:
        List of ``inherits`` edge dictionaries.
    """
    edges: list[dict] = []
    by_qname_line, by_qname = _collect_class_asts(tree, file_record.module_path)

    for class_node in class_nodes:
        if class_node.get("kind") != "class":
            continue

        class_node_id = class_node["id"]
        qname = class_node["qualified_name"]
        start_line = class_node.get("start_line")

        ast_node = (
            by_qname_line.get((qname, int(start_line)))
            if start_line is not None
            else None
        )
        if ast_node is None:
            ast_node = by_qname.get(qname)
        if ast_node is None:
            continue

        seen_bases: dict[str, int] = {}
        for base in ast_node.bases:
            base_name = safe_unparse(base)
            to_id = f"unresolved_base:{base_name}"
            dup_count = seen_bases.get(base_name, 0)
            seen_bases[base_name] = dup_count + 1
            edge_id = _make_inherits_edge_id(repo_id, class_node_id, base_name, dup_count)
            base_range = make_range(base, lines=lines)

            edges.append(
                {
                    "id": edge_id,
                    "repo_id": repo_id,
                    "kind": EDGE_KIND_INHERITS,
                    "from_id": class_node_id,
                    "to_id": to_id,
                    "source": "python-ast",
                    "confidence": 0.75,
                    "evidence_file_id": file_record.id,
                    "evidence_uri": file_record.uri,
                    "evidence_range_json": json.dumps(base_range, sort_keys=True)
                    if base_range
                    else None,
                    "payload_json": json.dumps({"base_name": base_name}, sort_keys=True),
                    "last_indexed_at": file_record.last_indexed_at,
                }
            )

    return edges


def _make_inherits_edge_id(
    repo_id: str,
    class_node_id: str,
    base_name: str,
    dup_count: int = 0,
) -> str:
    """Create a deterministic edge ID for an inheritance edge."""
    base = f"edge:{repo_id}:{EDGE_KIND_INHERITS}:{class_node_id}->unresolved_base:{base_name}"
    if dup_count > 0:
        return f"{base}:dup{dup_count}"
    return base
