"""Class node extraction from Python AST."""

import ast
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

import repo_context.parsing.callable_extractor
from repo_context.constants import EDGE_KIND_CONTAINS
from repo_context.models.file import FileRecord
from repo_context.parsing.docstrings import get_doc_summary
from repo_context.parsing.extraction_context import ExtractionContext, ScopeTarget
from repo_context.parsing.hashing import compute_symbol_content_hash
from repo_context.parsing.naming import (
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


@dataclass(frozen=True)
class _ClassPlacement:
    """Resolved structural and lexical placement for a class node."""

    parent_id: str | None
    lexical_parent_id: str | None
    qualified_name: str
    scope: str


def _build_class_signature(class_name: str, base_names: list[str]) -> str:
    """Build compact single-line class signature capped at 240 chars."""
    raw = f"class {class_name}({', '.join(base_names)})" if base_names else f"class {class_name}"
    return cap_signature(raw)


def _resolve_class_placement(
    ctx: ExtractionContext,
    class_name: str,
    module_node_id: str | None,
    target: ScopeTarget | None,
) -> _ClassPlacement:
    """Resolve parent IDs, scope, and qualified name for an ``ast.ClassDef``."""
    current_scope = ctx.scope_tracker.get_current_scope()
    enclosing_id = ctx.scope_tracker.get_lexical_parent_id()
    explicit_parent = target.parent_id if target is not None else None
    fallback_parent = module_node_id if current_scope == "module" else enclosing_id
    actual_parent_id = explicit_parent if explicit_parent is not None else fallback_parent
    lexical_parent_id = enclosing_id if current_scope not in ("module", "class") else None

    if not ctx.scope_tracker.is_empty():
        qualified_name = build_nested_qualified_name(
            ctx.module_path, ctx.scope_tracker.get_lexical_chain(), class_name
        )
    elif target is not None and target.parent_qualified_name:
        qualified_name = f"{target.parent_qualified_name}.{class_name}"
    else:
        qualified_name = build_class_qualified_name(ctx.module_path, class_name)

    return _ClassPlacement(
        parent_id=actual_parent_id,
        lexical_parent_id=lexical_parent_id,
        qualified_name=qualified_name,
        scope=current_scope,
    )


def _collect_direct_contains_edges(
    ctx: ExtractionContext,
    class_node_id: str,
    candidates: Sequence[dict],
) -> list[dict]:
    """Create direct ``contains`` edges for children whose ``parent_id`` is ``class_node_id``."""
    return [
        _make_direct_contains_edge(ctx.repo_id, class_node_id, child, ctx.file_record)
        for child in candidates
        if child.get("parent_id") == class_node_id
    ]


def _extract_class_children(
    ctx: ExtractionContext,
    class_target: ScopeTarget,
    class_node: ast.ClassDef,
    module_node_id: str | None,
) -> tuple[list[dict], list[dict]]:
    """Extract direct methods, nested classes, and their descendants from a class body."""
    extract_callable_nodes = repo_context.parsing.callable_extractor.extract_callable_nodes
    child_nodes: list[dict] = []
    contains_edges: list[dict] = []

    with ctx.scope_tracker.scope_context(class_target.parent_id, class_node.name, "class"):
        for stmt in class_node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                extracted = extract_callable_nodes(ctx, class_target, [stmt])
                child_nodes.extend(extracted)
                contains_edges.extend(
                    _collect_direct_contains_edges(ctx, class_target.parent_id, extracted)
                )
            elif isinstance(stmt, ast.ClassDef):
                nested_nodes, nested_edges = extract_class_nodes(
                    ctx,
                    ast.Module(body=[stmt], type_ignores=[]),
                    module_node_id=module_node_id,
                    target=class_target,
                )
                child_nodes.extend(nested_nodes)
                contains_edges.extend(
                    _collect_direct_contains_edges(ctx, class_target.parent_id, nested_nodes)
                )
                contains_edges.extend(nested_edges)

    return child_nodes, contains_edges


def _build_class_node_dict(
    ctx: ExtractionContext,
    node: ast.ClassDef,
    node_id: str,
    placement: _ClassPlacement,
    child_nodes: Sequence[dict],
) -> dict:
    """Build the symbol dictionary for a class node including direct ``method_ids``."""
    base_names = [safe_unparse(base) for base in node.bases]
    decorators = [safe_unparse(decorator) for decorator in node.decorator_list]
    start_line, end_line = make_declaration_lines(node)
    method_ids = [
        child["id"]
        for child in child_nodes
        if child.get("parent_id") == node_id and child["kind"] in ("method", "async_method")
    ]

    return {
        "id": node_id,
        "repo_id": ctx.repo_id,
        "file_id": ctx.file_record.id,
        "language": "python",
        "kind": "class",
        "name": node.name,
        "qualified_name": placement.qualified_name,
        "uri": ctx.file_record.uri,
        "range_json": make_range(node, lines=ctx.lines),
        "selection_range_json": make_name_selection_range(node, lines=ctx.lines),
        "start_line": start_line,
        "end_line": end_line,
        "signature": _build_class_signature(node.name, base_names),
        "parent_id": placement.parent_id,
        "visibility_hint": compute_visibility_hint(node.name, placement.scope),
        "doc_summary": get_doc_summary(node),
        "content_hash": compute_symbol_content_hash(node, ctx.lines, start_line, end_line),
        "semantic_hash": _compute_class_semantic_hash(
            placement.qualified_name, base_names, decorators
        ),
        "source": "python-ast",
        "confidence": 1.0,
        "scope": placement.scope,
        "lexical_parent_id": placement.lexical_parent_id,
        "payload_json": json.dumps(
            {
                "base_names": base_names,
                "decorators": decorators,
                "method_ids": method_ids,
            },
            sort_keys=True,
        ),
        "last_indexed_at": ctx.file_record.last_indexed_at,
    }


def _build_single_class_and_children(
    ctx: ExtractionContext,
    node: ast.ClassDef,
    module_node_id: str | None,
    target: ScopeTarget | None,
) -> tuple[list[dict], list[dict]]:
    """Extract one class node and all of its nested declarations."""
    placement = _resolve_class_placement(ctx, node.name, module_node_id, target)
    node_id = ctx.duplicate_tracker.get_symbol_id(ctx.repo_id, "class", placement.qualified_name)
    class_target = ScopeTarget(
        parent_id=node_id,
        parent_qualified_name=placement.qualified_name,
        scope="class",
    )
    child_nodes, child_contains_edges = _extract_class_children(
        ctx, class_target, node, module_node_id
    )
    class_node_dict = _build_class_node_dict(ctx, node, node_id, placement, child_nodes)
    return [class_node_dict, *child_nodes], child_contains_edges


def extract_class_nodes(
    ctx: ExtractionContext,
    tree: ast.Module,
    *,
    module_node_id: str | None = None,
    target: ScopeTarget | None = None,
) -> tuple[list[dict], list[dict]]:
    """Extract class nodes (including nested classes and methods) from AST.

    Args:
        ctx: Shared per-file extraction context.
        tree: AST container whose ``body`` contains ``ast.ClassDef`` nodes.
        module_node_id: Optional module symbol node ID for top-level classes.
        target: Optional explicit structural target for nested/local classes.

    Returns:
        Tuple of ``(nodes list, direct contains edges list)``.
    """
    nodes: list[dict] = []
    contains_edges: list[dict] = []

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        cls_nodes, cls_edges = _build_single_class_and_children(
            ctx, node, module_node_id, target
        )
        nodes.extend(cls_nodes)
        contains_edges.extend(cls_edges)

    return nodes, contains_edges


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
    hash_obj.update(f"qualified_name:{qualified_name}".encode())
    hash_obj.update(f"bases:{base_names}".encode())
    hash_obj.update(f"decorators:{decorators}".encode())
    return f"sha256:{hash_obj.hexdigest()}"
