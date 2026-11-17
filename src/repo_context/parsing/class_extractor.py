"""Class node extraction from Python AST."""

import ast
import hashlib
import json
from collections.abc import Sequence
from typing import Optional

import repo_context.parsing.callable_extractor
from repo_context.constants import EDGE_KIND_CONTAINS
from repo_context.models.file import FileRecord
from repo_context.parsing.docstrings import get_doc_summary
from repo_context.parsing.hashing import compute_symbol_content_hash
from repo_context.parsing.naming import (
    DuplicateTracker,
    build_class_qualified_name,
    build_nested_qualified_name,
    cap_signature,
    compute_visibility_hint,
    safe_unparse,
)
from repo_context.parsing.ranges import (
    make_declaration_lines,
    make_name_selection_range,
    make_range,
)
from repo_context.parsing.scope_tracker import ScopeTracker


def _build_class_signature(class_name: str, base_names: list[str]) -> str:
    """Build compact single-line class signature capped at 240 chars."""
    if base_names:
        raw = f"class {class_name}({', '.join(base_names)})"
    else:
        raw = f"class {class_name}"
    return cap_signature(raw)


def extract_class_nodes(
    repo_id: str,
    file_record: FileRecord,
    module_node_id: str,
    module_path: str,
    tree: ast.Module,
    scope_tracker: ScopeTracker,
    duplicate_tracker: DuplicateTracker,
    parent_id: Optional[str] = None,
    parent_qualified_name: Optional[str] = None,
    file_text: str = "",
    *,
    lines: Sequence[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Extract class nodes (including nested classes and methods) from AST.

    Args:
        repo_id: Repository ID.
        file_record: File record for the source module.
        module_node_id: Module symbol node ID.
        module_path: Module dotted path.
        tree: AST container whose ``body`` contains ``ast.ClassDef`` nodes.
        scope_tracker: Scope tracker for lexical nesting.
        duplicate_tracker: Duplicate tracker for symbol ID disambiguation.
        parent_id: Optional explicit structural parent ID.
        parent_qualified_name: Optional parent qualified name fallback.
        file_text: Full source text (used when ``lines`` is not provided).
        lines: Optional pre-split source lines.

    Returns:
        Tuple of ``(nodes list, direct contains edges list)``.
    """
    file_lines = lines if lines is not None else (file_text.splitlines() if file_text else None)
    nodes: list[dict] = []
    contains_edges: list[dict] = []

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue

        class_name = node.name
        current_scope = scope_tracker.get_current_scope()
        enclosing_id = scope_tracker.get_lexical_parent_id()

        if current_scope == "module":
            actual_parent_id = parent_id if parent_id is not None else module_node_id
            lexical_parent_id: str | None = None
        elif current_scope == "class":
            actual_parent_id = parent_id if parent_id is not None else enclosing_id
            lexical_parent_id = None
        else:
            actual_parent_id = parent_id if parent_id is not None else enclosing_id
            lexical_parent_id = enclosing_id

        if not scope_tracker.is_empty():
            qualified_name = build_nested_qualified_name(
                module_path, scope_tracker.get_lexical_chain(), class_name
            )
        elif parent_qualified_name is not None:
            qualified_name = f"{parent_qualified_name}.{class_name}"
        else:
            qualified_name = build_class_qualified_name(module_path, class_name)

        node_id = duplicate_tracker.get_symbol_id(repo_id, "class", qualified_name)

        base_names = [safe_unparse(base) for base in node.bases]
        decorators = [safe_unparse(decorator) for decorator in node.decorator_list]
        visibility_hint = compute_visibility_hint(class_name, current_scope)
        start_line, end_line = make_declaration_lines(node)
        range_json = make_range(node, lines=file_lines)
        selection_range_json = make_name_selection_range(node, lines=file_lines)
        signature = _build_class_signature(class_name, base_names)
        content_hash = compute_symbol_content_hash(node, file_lines, start_line, end_line)

        class_node_dict = {
            "id": node_id,
            "repo_id": repo_id,
            "file_id": file_record.id,
            "language": "python",
            "kind": "class",
            "name": class_name,
            "qualified_name": qualified_name,
            "uri": file_record.uri,
            "range_json": range_json,
            "selection_range_json": selection_range_json,
            "start_line": start_line,
            "end_line": end_line,
            "signature": signature,
            "parent_id": actual_parent_id,
            "visibility_hint": visibility_hint,
            "doc_summary": get_doc_summary(node),
            "content_hash": content_hash,
            "semantic_hash": _compute_class_semantic_hash(qualified_name, base_names, decorators),
            "source": "python-ast",
            "confidence": 1.0,
            "scope": current_scope,
            "lexical_parent_id": lexical_parent_id,
            "payload_json": json.dumps(
                {
                    "base_names": base_names,
                    "decorators": decorators,
                    "method_ids": [],
                },
                sort_keys=True,
            ),
            "last_indexed_at": file_record.last_indexed_at,
        }
        nodes.append(class_node_dict)

        child_nodes, child_contains_edges = _extract_class_children(
            repo_id=repo_id,
            file_record=file_record,
            module_node_id=module_node_id,
            class_node_id=node_id,
            class_qualified_name=qualified_name,
            class_node=node,
            scope_tracker=scope_tracker,
            duplicate_tracker=duplicate_tracker,
            module_path=module_path,
            file_text=file_text,
            lines=file_lines,
        )
        nodes.extend(child_nodes)
        contains_edges.extend(child_contains_edges)

        # Keep method_ids strictly scoped to direct method children of this class.
        method_ids = [
            child["id"]
            for child in child_nodes
            if child.get("parent_id") == node_id and child["kind"] in ("method", "async_method")
        ]
        class_node_dict["payload_json"] = json.dumps(
            {
                "base_names": base_names,
                "decorators": decorators,
                "method_ids": method_ids,
            },
            sort_keys=True,
        )

    return nodes, contains_edges


def _extract_class_children(
    repo_id: str,
    file_record: FileRecord,
    module_node_id: str,
    class_node_id: str,
    class_qualified_name: str,
    class_node: ast.ClassDef,
    scope_tracker: ScopeTracker,
    duplicate_tracker: DuplicateTracker,
    module_path: str,
    file_text: str,
    lines: Sequence[str] | None,
) -> tuple[list[dict], list[dict]]:
    """Extract direct methods, nested classes, and their descendants from a class body."""
    extract_callable_nodes = repo_context.parsing.callable_extractor.extract_callable_nodes

    child_nodes: list[dict] = []
    contains_edges: list[dict] = []

    with scope_tracker.scope_context(class_node_id, class_node.name, "class"):
        for stmt in class_node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                extracted = extract_callable_nodes(
                    repo_id=repo_id,
                    file_record=file_record,
                    parent_id=class_node_id,
                    parent_qualified_name=class_qualified_name,
                    nodes=[stmt],
                    scope_tracker=scope_tracker,
                    duplicate_tracker=duplicate_tracker,
                    module_path=module_path,
                    file_text=file_text,
                    lines=lines,
                )
                child_nodes.extend(extracted)
                for extracted_node in extracted:
                    if extracted_node.get("parent_id") == class_node_id:
                        contains_edges.append(
                            _make_direct_contains_edge(
                                repo_id, class_node_id, extracted_node, file_record
                            )
                        )
            elif isinstance(stmt, ast.ClassDef):
                nested_nodes, nested_edges = extract_class_nodes(
                    repo_id=repo_id,
                    file_record=file_record,
                    module_node_id=module_node_id,
                    module_path=module_path,
                    tree=ast.Module(body=[stmt], type_ignores=[]),
                    scope_tracker=scope_tracker,
                    duplicate_tracker=duplicate_tracker,
                    parent_id=class_node_id,
                    parent_qualified_name=class_qualified_name,
                    file_text=file_text,
                    lines=lines,
                )
                child_nodes.extend(nested_nodes)
                for nested_node in nested_nodes:
                    if nested_node.get("parent_id") == class_node_id:
                        contains_edges.append(
                            _make_direct_contains_edge(
                                repo_id, class_node_id, nested_node, file_record
                            )
                        )
                contains_edges.extend(nested_edges)

    return child_nodes, contains_edges


def _make_direct_contains_edge(
    repo_id: str,
    from_id: str,
    child_node: dict,
    file_record: FileRecord,
) -> dict:
    """Create a direct structural containment edge from class to direct child."""
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


def _compute_class_semantic_hash(
    qualified_name: str, base_names: list[str], decorators: list[str]
) -> str:
    """Compute a semantic hash for a class node."""
    hash_obj = hashlib.sha256()
    hash_obj.update(b"kind:class")
    hash_obj.update(f"qualified_name:{qualified_name}".encode("utf-8"))
    hash_obj.update(f"bases:{base_names}".encode("utf-8"))
    hash_obj.update(f"decorators:{decorators}".encode("utf-8"))
    return f"sha256:{hash_obj.hexdigest()}"
