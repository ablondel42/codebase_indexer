"""Module-level constants, type aliases, and class-level attribute extraction."""

import ast
import hashlib
import json
import sys
from collections.abc import Sequence

from repo_context.constants import (
    SYMBOL_KIND_ATTRIBUTE,
    SYMBOL_KIND_CONSTANT,
    SYMBOL_KIND_TYPE_ALIAS,
)
from repo_context.models.file import FileRecord
from repo_context.parsing.hashing import compute_symbol_content_hash
from repo_context.parsing.naming import (
    DuplicateTracker,
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
    hash_obj.update(f"kind:{kind}".encode("utf-8"))
    hash_obj.update(f"qualified_name:{qualified_name}".encode("utf-8"))
    hash_obj.update(f"annotation:{annotation}".encode("utf-8"))
    hash_obj.update(f"value_hint:{value_hint}".encode("utf-8"))
    return f"sha256:{hash_obj.hexdigest()}"


def _build_assignment_node(
    repo_id: str,
    file_record: FileRecord,
    parent_id: str,
    parent_qualified_name: str,
    scope: str,
    kind: str,
    name_node: ast.Name,
    stmt: ast.AST,
    signature: str,
    annotation: str | None,
    value_hint: str | None,
    duplicate_tracker: DuplicateTracker,
    lines: Sequence[str] | None,
) -> dict:
    """Build one normalized constant, attribute, or type_alias node dict."""
    name = name_node.id
    qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
    node_id = duplicate_tracker.get_symbol_id(repo_id, kind, qualified_name)

    start_line, end_line = make_declaration_lines(stmt)
    range_json = make_range(stmt, lines=lines)
    selection_range_json = make_name_selection_range(name_node, lines=lines)
    content_hash = compute_symbol_content_hash(stmt, lines, start_line, end_line)
    semantic_hash = _compute_assignment_semantic_hash(kind, qualified_name, annotation, value_hint)

    payload: dict = {}
    if annotation is not None:
        payload["annotation"] = annotation
    if value_hint is not None:
        payload["value_hint"] = value_hint

    return {
        "id": node_id,
        "repo_id": repo_id,
        "file_id": file_record.id,
        "language": "python",
        "kind": kind,
        "name": name,
        "qualified_name": qualified_name,
        "uri": file_record.uri,
        "range_json": range_json,
        "selection_range_json": selection_range_json,
        "start_line": start_line,
        "end_line": end_line,
        "signature": signature,
        "parent_id": parent_id,
        "visibility_hint": compute_visibility_hint(name, scope),
        "doc_summary": None,
        "content_hash": content_hash,
        "semantic_hash": semantic_hash,
        "source": "python-ast",
        "confidence": 1.0,
        "scope": scope,
        "lexical_parent_id": None,
        "payload_json": json.dumps(payload, sort_keys=True),
        "last_indexed_at": file_record.last_indexed_at,
    }


def _extract_from_body(
    body: Sequence[ast.stmt],
    repo_id: str,
    file_record: FileRecord,
    parent_id: str,
    parent_qualified_name: str,
    scope: str,
    default_kind: str,
    duplicate_tracker: DuplicateTracker,
    lines: Sequence[str] | None,
) -> list[dict]:
    """Extract Assign, AnnAssign, and TypeAlias nodes from a module or class body."""
    nodes: list[dict] = []

    for stmt in body:
        if sys.version_info >= (3, 12) and isinstance(stmt, ast.TypeAlias):
            name_node = stmt.name
            if not isinstance(name_node, ast.Name) or name_node.id == "_":
                continue
            value_hint = cap_signature(safe_unparse(stmt.value))
            signature = cap_signature(safe_unparse(stmt))
            nodes.append(
                _build_assignment_node(
                    repo_id=repo_id,
                    file_record=file_record,
                    parent_id=parent_id,
                    parent_qualified_name=parent_qualified_name,
                    scope=scope,
                    kind=SYMBOL_KIND_TYPE_ALIAS,
                    name_node=name_node,
                    stmt=stmt,
                    signature=signature,
                    annotation=None,
                    value_hint=value_hint,
                    duplicate_tracker=duplicate_tracker,
                    lines=lines,
                )
            )

        elif isinstance(stmt, ast.AnnAssign):
            if not isinstance(stmt.target, ast.Name) or stmt.target.id == "_":
                continue
            annotation = safe_unparse(stmt.annotation)
            kind = SYMBOL_KIND_TYPE_ALIAS if _is_type_alias_annotation(annotation) else default_kind
            value_hint = (
                cap_signature(safe_unparse(stmt.value)) if stmt.value is not None else None
            )
            signature = cap_signature(safe_unparse(stmt))
            nodes.append(
                _build_assignment_node(
                    repo_id=repo_id,
                    file_record=file_record,
                    parent_id=parent_id,
                    parent_qualified_name=parent_qualified_name,
                    scope=scope,
                    kind=kind,
                    name_node=stmt.target,
                    stmt=stmt,
                    signature=signature,
                    annotation=annotation,
                    value_hint=value_hint,
                    duplicate_tracker=duplicate_tracker,
                    lines=lines,
                )
            )

        elif isinstance(stmt, ast.Assign):
            value_hint = cap_signature(safe_unparse(stmt.value))
            stmt_signature = cap_signature(safe_unparse(stmt))
            for target in stmt.targets:
                for name_node in _extract_target_names(target):
                    sig = (
                        stmt_signature
                        if len(stmt.targets) == 1 and isinstance(target, ast.Name)
                        else cap_signature(f"{name_node.id} = {value_hint}")
                    )
                    nodes.append(
                        _build_assignment_node(
                            repo_id=repo_id,
                            file_record=file_record,
                            parent_id=parent_id,
                            parent_qualified_name=parent_qualified_name,
                            scope=scope,
                            kind=default_kind,
                            name_node=name_node,
                            stmt=stmt,
                            signature=sig,
                            annotation=None,
                            value_hint=value_hint,
                            duplicate_tracker=duplicate_tracker,
                            lines=lines,
                        )
                    )

    return nodes


def _build_class_lookup(class_nodes: Sequence[dict]) -> tuple[dict[tuple[str, int], dict], dict[str, dict]]:
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


def extract_assignment_nodes(
    repo_id: str,
    file_record: FileRecord,
    module_node_id: str,
    module_path: str,
    tree: ast.Module,
    *,
    class_nodes: Sequence[dict] | None = None,
    duplicate_tracker: DuplicateTracker | None = None,
    lines: Sequence[str] | None = None,
) -> list[dict]:
    """Extract module constants/type aliases and class attributes/fields.

    Args:
        repo_id: Repository ID.
        file_record: File record for the module.
        module_node_id: Parent module node ID.
        module_path: Module dotted path.
        tree: Parsed AST module.
        class_nodes: Previously extracted class nodes (including nested/local classes).
        duplicate_tracker: Optional shared duplicate tracker.
        lines: Optional pre-split source lines.

    Returns:
        List of extracted ``constant``, ``type_alias``, and ``attribute`` node dicts.
    """
    tracker = duplicate_tracker if duplicate_tracker is not None else DuplicateTracker()
    extracted: list[dict] = []

    # 1. Module-level constants and type aliases
    extracted.extend(
        _extract_from_body(
            body=tree.body,
            repo_id=repo_id,
            file_record=file_record,
            parent_id=module_node_id,
            parent_qualified_name=module_path,
            scope="module",
            default_kind=SYMBOL_KIND_CONSTANT,
            duplicate_tracker=tracker,
            lines=lines,
        )
    )

    if not class_nodes:
        return extracted

    # 2. Class-level attributes/fields across top-level, nested, and local classes
    by_qname_line, by_qname = _build_class_lookup(class_nodes)

    def _walk_body_for_classes(stmts: Sequence[ast.stmt], lexical_chain: list[str]) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.ClassDef):
                qname = build_nested_qualified_name(module_path, lexical_chain, stmt.name)
                decl_start, _ = make_declaration_lines(stmt)
                cls_node = by_qname_line.get((qname, decl_start)) or by_qname.get(qname)
                if cls_node is not None:
                    extracted.extend(
                        _extract_from_body(
                            body=stmt.body,
                            repo_id=repo_id,
                            file_record=file_record,
                            parent_id=cls_node["id"],
                            parent_qualified_name=cls_node["qualified_name"],
                            scope="class",
                            default_kind=SYMBOL_KIND_ATTRIBUTE,
                            duplicate_tracker=tracker,
                            lines=lines,
                        )
                    )
                lexical_chain.append(stmt.name)
                _walk_body_for_classes(stmt.body, lexical_chain)
                lexical_chain.pop()
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lexical_chain.append(stmt.name)
                _walk_body_for_classes(stmt.body, lexical_chain)
                lexical_chain.pop()

    _walk_body_for_classes(tree.body, [])
    return extracted
