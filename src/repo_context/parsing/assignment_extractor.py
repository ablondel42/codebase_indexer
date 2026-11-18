"""Module-level constants, type aliases, and class-level attribute extraction."""

import ast
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from repo_context.constants import (
    SYMBOL_KIND_ATTRIBUTE,
    SYMBOL_KIND_CONSTANT,
    SYMBOL_KIND_TYPE_ALIAS,
)
from repo_context.parsing.extraction_context import ExtractionContext, ScopeTarget
from repo_context.parsing.hashing import compute_symbol_content_hash
from repo_context.parsing.naming import (
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
class _AssignmentSpec:
    """Extracted metadata for a single assignment target."""

    kind: str
    name_node: ast.Name
    stmt: ast.AST
    signature: str
    annotation: str | None = None
    value_hint: str | None = None


def _is_type_alias_annotation(annotation_str: str) -> bool:
    """Return True if an annotation string denotes ``typing.TypeAlias``."""
    stripped = annotation_str.strip()
    return (
        stripped in ("TypeAlias", "typing.TypeAlias")
        or stripped.startswith("TypeAlias[")
        or stripped.startswith("typing.TypeAlias[")
    )


def _extract_target_names(target: ast.AST) -> list[ast.Name]:
    """Extract simple ``ast.Name`` targets from an assignment target."""
    if isinstance(target, ast.Name):
        return [target] if target.id != "_" else []
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[ast.Name] = []
        for elt in target.elts:
            names.extend(_extract_target_names(elt))
        return names
    return []


def _compute_assignment_semantic_hash(
    kind: str,
    qualified_name: str,
    annotation: str | None,
    value_hint: str | None,
) -> str:
    """Compute a deterministic semantic hash for a constant, attribute, or type alias."""
    hash_obj = hashlib.sha256()
    hash_obj.update(f"kind:{kind}".encode())
    hash_obj.update(f"qualified_name:{qualified_name}".encode())
    hash_obj.update(f"annotation:{annotation}".encode())
    hash_obj.update(f"value_hint:{value_hint}".encode())
    return f"sha256:{hash_obj.hexdigest()}"


def _build_assignment_node(
    ctx: ExtractionContext,
    target: ScopeTarget,
    spec: _AssignmentSpec,
) -> dict:
    """Build one normalized constant, attribute, or type_alias node dict."""
    name = spec.name_node.id
    qualified_name = (
        f"{target.parent_qualified_name}.{name}" if target.parent_qualified_name else name
    )
    node_id = ctx.duplicate_tracker.get_symbol_id(ctx.repo_id, spec.kind, qualified_name)
    start_line, end_line = make_declaration_lines(spec.stmt)

    payload: dict = {}
    if spec.annotation is not None:
        payload["annotation"] = spec.annotation
    if spec.value_hint is not None:
        payload["value_hint"] = spec.value_hint

    return {
        "id": node_id,
        "repo_id": ctx.repo_id,
        "file_id": ctx.file_record.id,
        "language": "python",
        "kind": spec.kind,
        "name": name,
        "qualified_name": qualified_name,
        "uri": ctx.file_record.uri,
        "range_json": make_range(spec.stmt, lines=ctx.lines),
        "selection_range_json": make_name_selection_range(spec.name_node, lines=ctx.lines),
        "start_line": start_line,
        "end_line": end_line,
        "signature": spec.signature,
        "parent_id": target.parent_id,
        "visibility_hint": compute_visibility_hint(name, target.scope),
        "doc_summary": None,
        "content_hash": compute_symbol_content_hash(spec.stmt, ctx.lines, start_line, end_line),
        "semantic_hash": _compute_assignment_semantic_hash(
            spec.kind, qualified_name, spec.annotation, spec.value_hint
        ),
        "source": "python-ast",
        "confidence": 1.0,
        "scope": target.scope,
        "lexical_parent_id": None,
        "payload_json": json.dumps(payload, sort_keys=True),
        "last_indexed_at": ctx.file_record.last_indexed_at,
    }


def _extract_type_alias_nodes(
    ctx: ExtractionContext,
    target: ScopeTarget,
    stmt: ast.stmt,
) -> list[dict]:
    """Extract a ``type_alias`` node from Python 3.12+ ``ast.TypeAlias`` statements."""
    if sys.version_info < (3, 12) or not isinstance(stmt, ast.TypeAlias):
        return []
    name_node = stmt.name
    if not isinstance(name_node, ast.Name) or name_node.id == "_":
        return []
    spec = _AssignmentSpec(
        kind=SYMBOL_KIND_TYPE_ALIAS,
        name_node=name_node,
        stmt=stmt,
        signature=cap_signature(safe_unparse(stmt)),
        value_hint=cap_signature(safe_unparse(stmt.value)),
    )
    return [_build_assignment_node(ctx, target, spec)]


def _extract_ann_assign_nodes(
    ctx: ExtractionContext,
    target: ScopeTarget,
    stmt: ast.AnnAssign,
    default_kind: str,
) -> list[dict]:
    """Extract a constant, attribute, or annotated type alias from ``ast.AnnAssign``."""
    if not isinstance(stmt.target, ast.Name) or stmt.target.id == "_":
        return []
    annotation = safe_unparse(stmt.annotation)
    kind = SYMBOL_KIND_TYPE_ALIAS if _is_type_alias_annotation(annotation) else default_kind
    value_hint = cap_signature(safe_unparse(stmt.value)) if stmt.value is not None else None
    spec = _AssignmentSpec(
        kind=kind,
        name_node=stmt.target,
        stmt=stmt,
        signature=cap_signature(safe_unparse(stmt)),
        annotation=annotation,
        value_hint=value_hint,
    )
    return [_build_assignment_node(ctx, target, spec)]


def _extract_assign_nodes(
    ctx: ExtractionContext,
    target: ScopeTarget,
    stmt: ast.Assign,
    default_kind: str,
) -> list[dict]:
    """Extract constant or attribute nodes from ``ast.Assign``."""
    value_hint = cap_signature(safe_unparse(stmt.value))
    stmt_signature = cap_signature(safe_unparse(stmt))
    is_single_name = len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
    nodes: list[dict] = []

    for assign_target in stmt.targets:
        for name_node in _extract_target_names(assign_target):
            sig = (
                stmt_signature
                if is_single_name
                else cap_signature(f"{name_node.id} = {value_hint}")
            )
            spec = _AssignmentSpec(
                kind=default_kind,
                name_node=name_node,
                stmt=stmt,
                signature=sig,
                value_hint=value_hint,
            )
            nodes.append(_build_assignment_node(ctx, target, spec))
    return nodes


def _extract_from_body(
    ctx: ExtractionContext,
    target: ScopeTarget,
    body: Sequence[ast.stmt],
    default_kind: str,
) -> list[dict]:
    """Extract Assign, AnnAssign, and TypeAlias nodes from a module or class body."""
    nodes: list[dict] = []
    for stmt in body:
        if isinstance(stmt, ast.AnnAssign):
            nodes.extend(_extract_ann_assign_nodes(ctx, target, stmt, default_kind))
        elif isinstance(stmt, ast.Assign):
            nodes.extend(_extract_assign_nodes(ctx, target, stmt, default_kind))
        else:
            nodes.extend(_extract_type_alias_nodes(ctx, target, stmt))
    return nodes


def _build_class_lookup(
    class_nodes: Sequence[dict],
) -> tuple[dict[tuple[str, int], dict], dict[str, dict]]:
    """Build exact and fallback lookups for extracted class nodes."""
    by_qname_line: dict[tuple[str, int], dict] = {}
    by_qname: dict[str, dict] = {}
    for cls in class_nodes:
        if cls.get("kind") != "class":
            continue
        qname = cls["qualified_name"]
        by_qname.setdefault(qname, cls)
        start_line = cls.get("start_line")
        if start_line is not None:
            by_qname_line[(qname, int(start_line))] = cls
    return by_qname_line, by_qname


def _find_class_node(
    stmt: ast.ClassDef,
    module_path: str,
    lexical_chain: list[str],
    class_lookup: tuple[dict[tuple[str, int], dict], dict[str, dict]],
) -> dict | None:
    """Resolve the extracted class node dict matching an ``ast.ClassDef``."""
    by_qname_line, by_qname = class_lookup
    qname = build_nested_qualified_name(module_path, lexical_chain, stmt.name)
    decl_start, _ = make_declaration_lines(stmt)
    return by_qname_line.get((qname, decl_start)) or by_qname.get(qname)


def _walk_class_attributes(
    ctx: ExtractionContext,
    stmts: Sequence[ast.stmt],
    lexical_chain: list[str],
    class_lookup: tuple[dict[tuple[str, int], dict], dict[str, dict]],
    extracted: list[dict],
) -> None:
    """Recursively extract class-level attributes across top-level, nested, and local classes."""
    for stmt in stmts:
        if isinstance(stmt, ast.ClassDef):
            cls_node = _find_class_node(stmt, ctx.module_path, lexical_chain, class_lookup)
            if cls_node is not None:
                cls_target = ScopeTarget(
                    parent_id=cls_node["id"],
                    parent_qualified_name=cls_node["qualified_name"],
                    scope="class",
                )
                extracted.extend(
                    _extract_from_body(ctx, cls_target, stmt.body, SYMBOL_KIND_ATTRIBUTE)
                )
            lexical_chain.append(stmt.name)
            _walk_class_attributes(ctx, stmt.body, lexical_chain, class_lookup, extracted)
            lexical_chain.pop()
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lexical_chain.append(stmt.name)
            _walk_class_attributes(ctx, stmt.body, lexical_chain, class_lookup, extracted)
            lexical_chain.pop()


def extract_assignment_nodes(
    ctx: ExtractionContext,
    module_node_id: str,
    tree: ast.Module,
    *,
    class_nodes: Sequence[dict] | None = None,
) -> list[dict]:
    """Extract module constants/type aliases and class attributes/fields.

    Args:
        ctx: Shared per-file extraction context.
        module_node_id: Parent module node ID.
        tree: Parsed AST module.
        class_nodes: Previously extracted class nodes (including nested/local classes).

    Returns:
        List of extracted ``constant``, ``type_alias``, and ``attribute`` node dicts.
    """
    module_target = ScopeTarget(
        parent_id=module_node_id,
        parent_qualified_name=ctx.module_path,
        scope="module",
    )
    extracted = _extract_from_body(ctx, module_target, tree.body, SYMBOL_KIND_CONSTANT)
    if not class_nodes:
        return extracted

    class_lookup = _build_class_lookup(class_nodes)
    _walk_class_attributes(ctx, tree.body, [], class_lookup, extracted)
    return extracted
