"""Per-file AST extraction pipeline with nested scope, assignments, and calls."""

import ast
import json
from pathlib import Path
from typing import Any

from repo_context.constants import EDGE_KIND_CONTAINS, EDGE_KIND_SCOPE_PARENT
from repo_context.models.file import FileRecord
from repo_context.parsing.assignment_extractor import extract_assignment_nodes
from repo_context.parsing.ast_loader import load_file_text, parse_file
from repo_context.parsing.call_extractor import extract_call_edges
from repo_context.parsing.callable_extractor import extract_callable_nodes
from repo_context.parsing.class_extractor import extract_class_nodes
from repo_context.parsing.import_extractor import extract_import_edges_and_payload
from repo_context.parsing.inheritance_extractor import extract_inheritance_edges
from repo_context.parsing.module_extractor import extract_module_node
from repo_context.parsing.naming import DuplicateTracker
from repo_context.parsing.pathing import ensure_safe_repo_file
from repo_context.parsing.scope_tracker import ScopeTracker


def extract_file_graph(
    repo_id: str,
    file_record: FileRecord,
    repo_root: Path,
    config: Any = None,
) -> tuple[list[dict], list[dict], dict]:
    """Extract all symbol nodes and AST edges for a single Python file.

    Args:
        repo_id: Repository ID.
        file_record: File record for the target Python file.
        repo_root: Repository root path.
        config: Optional application config (used for ``max_file_size_bytes``).

    Returns:
        Tuple of ``(nodes list, edges list, summary dict)``.

    Raises:
        PathSecurityError: If ``file_record.file_path`` escapes ``repo_root`` or is a symlink.
        FileNotFoundError: If the file cannot be found.
        ParseError: If the file is oversized, invalid UTF-8, or fails AST parsing.
    """
    safe_path = ensure_safe_repo_file(repo_root, file_record.file_path)
    max_file_size_bytes = getattr(config, "max_file_size_bytes", None)
    file_text = load_file_text(safe_path, max_file_size_bytes=max_file_size_bytes)
    lines = file_text.splitlines()
    tree = parse_file(file_text, filename=safe_path)

    scope_tracker = ScopeTracker()
    duplicate_tracker = DuplicateTracker()

    module_node = extract_module_node(
        repo_id,
        file_record,
        tree,
        file_text,
        scope_tracker,
        duplicate_tracker,
        lines=lines,
    )
    module_node_id = module_node["id"]
    module_path = file_record.module_path

    import_edges, imported_modules, imported_symbols = extract_import_edges_and_payload(
        repo_id, module_node_id, file_record, tree, lines=lines
    )

    class_tree_nodes, _ = extract_class_nodes(
        repo_id,
        file_record,
        module_node_id,
        module_path,
        tree,
        scope_tracker,
        duplicate_tracker,
        file_text=file_text,
        lines=lines,
    )

    top_level_callables = _extract_top_level_callables(
        repo_id,
        file_record,
        module_node_id,
        module_path,
        tree,
        scope_tracker,
        duplicate_tracker,
        file_text,
        lines,
    )

    all_class_nodes = [
        n for n in [*class_tree_nodes, *top_level_callables] if n["kind"] == "class"
    ]
    assignment_nodes = extract_assignment_nodes(
        repo_id,
        file_record,
        module_node_id,
        module_path,
        tree,
        class_nodes=all_class_nodes,
        duplicate_tracker=duplicate_tracker,
        lines=lines,
    )

    nodes = [module_node, *class_tree_nodes, *top_level_callables, *assignment_nodes]

    # Emit contains edges strictly for direct parent_id -> child_id relationships,
    # and scope_parent edges strictly for function-local lexical nesting.
    structural_edges: list[dict] = []
    for node in nodes[1:]:
        parent_id = node.get("parent_id")
        if parent_id is not None:
            structural_edges.append(
                _make_contains_edge(repo_id, parent_id, node, file_record)
            )
        lexical_parent_id = node.get("lexical_parent_id")
        if lexical_parent_id is not None:
            structural_edges.append(
                _make_scope_parent_edge(repo_id, node["id"], lexical_parent_id, file_record)
            )

    inheritance_edges = extract_inheritance_edges(
        repo_id, file_record, all_class_nodes, tree, lines=lines
    )
    call_edges = extract_call_edges(
        repo_id, file_record, module_node_id, nodes, tree, lines=lines
    )

    module_payload = json.loads(module_node["payload_json"])
    module_payload["imported_modules"] = imported_modules
    module_payload["imported_symbols"] = imported_symbols
    module_payload["top_level_symbol_ids"] = [
        n["id"] for n in nodes if n.get("parent_id") == module_node_id
    ]
    module_node["payload_json"] = json.dumps(module_payload, sort_keys=True)

    edges = [*import_edges, *structural_edges, *inheritance_edges, *call_edges]

    method_count = sum(1 for n in nodes if n["kind"] in ("method", "async_method"))
    local_callable_count = sum(
        1 for n in nodes if n["kind"] in ("local_function", "local_async_function")
    )
    top_callable_count = sum(1 for n in nodes if n["kind"] in ("function", "async_function"))
    callable_count = top_callable_count + method_count + local_callable_count

    summary = {
        "file_id": file_record.id,
        "file_path": file_record.file_path,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "module_count": 1,
        "class_count": len(all_class_nodes),
        "callable_count": callable_count,
        "method_count": method_count,
        "constant_count": sum(1 for n in nodes if n["kind"] == "constant"),
        "attribute_count": sum(1 for n in nodes if n["kind"] == "attribute"),
        "type_alias_count": sum(1 for n in nodes if n["kind"] == "type_alias"),
        "call_edge_count": len(call_edges),
    }

    return nodes, edges, summary


def _extract_top_level_callables(
    repo_id: str,
    file_record: FileRecord,
    module_node_id: str,
    module_path: str,
    tree: ast.Module,
    scope_tracker: ScopeTracker,
    duplicate_tracker: DuplicateTracker,
    file_text: str,
    lines: list[str],
) -> list[dict]:
    """Extract top-level functions and their nested local declarations."""
    callables = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return extract_callable_nodes(
        repo_id=repo_id,
        file_record=file_record,
        parent_id=module_node_id,
        parent_qualified_name=module_path,
        nodes=callables,
        scope_tracker=scope_tracker,
        duplicate_tracker=duplicate_tracker,
        module_path=module_path,
        file_text=file_text,
        lines=lines,
    )


def _make_contains_edge(
    repo_id: str,
    from_id: str,
    child_node: dict,
    file_record: FileRecord,
) -> dict:
    """Create a direct structural ``contains`` edge from ``from_id`` to ``child_node``."""
    child_range = child_node.get("range_json")
    if isinstance(child_range, dict):
        evidence_range_json = json.dumps(child_range, sort_keys=True)
    elif isinstance(child_range, str):
        evidence_range_json = child_range
    else:
        evidence_range_json = None

    return {
        "id": f"edge:{repo_id}:{EDGE_KIND_CONTAINS}:{from_id}->{child_node['id']}",
        "repo_id": repo_id,
        "kind": EDGE_KIND_CONTAINS,
        "from_id": from_id,
        "to_id": child_node["id"],
        "source": "python-ast",
        "confidence": 1.0,
        "evidence_file_id": file_record.id,
        "evidence_uri": file_record.uri,
        "evidence_range_json": evidence_range_json,
        "payload_json": "{}",
        "last_indexed_at": file_record.last_indexed_at,
    }


def _make_scope_parent_edge(
    repo_id: str,
    from_id: str,
    to_id: str,
    file_record: FileRecord,
) -> dict:
    """Create a ``scope_parent`` edge for function-local lexical nesting."""
    return {
        "id": f"edge:{repo_id}:{EDGE_KIND_SCOPE_PARENT}:{from_id}->{to_id}",
        "repo_id": repo_id,
        "kind": EDGE_KIND_SCOPE_PARENT,
        "from_id": from_id,
        "to_id": to_id,
        "source": "python-ast",
        "confidence": 1.0,
        "evidence_file_id": file_record.id,
        "evidence_uri": file_record.uri,
        "evidence_range_json": None,
        "payload_json": "{}",
        "last_indexed_at": file_record.last_indexed_at,
    }
