"""Pure qualified-name, semantic-hash, placement, and symbol/edge builders."""

import hashlib
from collections.abc import Sequence
from typing import Any

from core.models.enums import (
    PREFIX_EDGE,
    PREFIX_SHA256,
    EdgeKind,
    Language,
    SymbolKind,
    SymbolScope,
)
from core.models.file import FileRecord
from core.models.node import SymbolNode
from core.models.range import Range
from parsing.models.extraction import DeclarationPlacement, ScopeTarget
from parsing.utils.ast_helpers import compute_visibility_hint


def build_nested_qualified_name(
    module_path: str,
    lexical_chain: Sequence[str],
    name: str,
) -> str:
    """Build a qualified name incorporating enclosing class/function scope names.

    Args:
        module_path: Dotted Python module path.
        lexical_chain: Ordered enclosing declaration names from outermost to innermost.
        name: Simple declared symbol name.

    Returns:
        Full dotted qualified name string.
    """
    parts = [module_path] if module_path else []
    parts.extend(lexical_chain)
    parts.append(name)
    return ".".join(parts)


def make_structural_edge_id(
    repo_id: str,
    kind: EdgeKind | str,
    from_id: str,
    to_id: str,
) -> str:
    """Build a deterministic edge ID for structural ``contains`` or ``scope_parent`` edges.

    Args:
        repo_id: Owning repository identifier.
        kind: Structural ``EdgeKind`` (``contains`` or ``scope_parent``).
        from_id: Source symbol node identifier.
        to_id: Target symbol node identifier.

    Returns:
        Canonical ``edge:<repo_id>:<kind>:<from_id>-><to_id>`` identifier.
    """
    kind_val = kind.value if isinstance(kind, EdgeKind) else str(kind)
    return f"{PREFIX_EDGE}{repo_id}:{kind_val}:{from_id}->{to_id}"


def compute_semantic_digest(parts: Sequence[str]) -> str:
    """Compute a deterministic ``sha256:`` digest over ordered semantic ``key:value`` parts.

    Args:
        parts: Ordered string fragments hashed sequentially as UTF-8 bytes.

    Returns:
        ``sha256:``-prefixed digest string.
    """
    hash_obj = hashlib.sha256()
    for part in parts:
        hash_obj.update(part.encode("utf-8"))
    return f"{PREFIX_SHA256}{hash_obj.hexdigest()}"


def resolve_symbol_placement(
    module_path: str,
    lexical_chain: Sequence[str],
    scope_state: tuple[SymbolScope, str | None],
    *,
    kind: SymbolKind,
    name: str,
    target: tuple[ScopeTarget | None, str | None] = (None, None),
) -> DeclarationPlacement:
    """Resolve structural/lexical parent IDs and qualified name for a declaration.

    Args:
        module_path: Dotted Python module path.
        lexical_chain: Ordered enclosing declaration names from outermost to innermost.
        scope_state: Tuple ``(current_scope, enclosing_id)`` from ``ScopeTracker``.
        kind: Resolved ``SymbolKind`` for the declaration.
        name: Simple declared identifier name.
        target: Tuple ``(scope_target, fallback_parent_id)``.

    Returns:
        Resolved ``DeclarationPlacement`` value object.
    """
    current_scope, enclosing_id = scope_state
    scope_target, fallback_parent_id = target
    if kind == SymbolKind.CLASS:
        explicit_parent = scope_target.parent_id if scope_target is not None else None
        fallback = fallback_parent_id if current_scope == SymbolScope.MODULE else enclosing_id
        actual_parent_id = explicit_parent if explicit_parent is not None else fallback
    else:
        target_parent = scope_target.parent_id if scope_target is not None else fallback_parent_id
        actual_parent_id = (
            target_parent
            if current_scope == SymbolScope.MODULE or enclosing_id is None
            else enclosing_id
        )

    lexical_parent_id = (
        enclosing_id if current_scope not in (SymbolScope.MODULE, SymbolScope.CLASS) else None
    )

    if lexical_chain:
        qualified_name = build_nested_qualified_name(module_path, lexical_chain, name)
    elif scope_target is not None and scope_target.parent_qualified_name:
        qualified_name = f"{scope_target.parent_qualified_name}.{name}"
    elif module_path:
        qualified_name = f"{module_path}.{name}"
    else:
        qualified_name = name

    return DeclarationPlacement(
        kind=kind,
        parent_id=actual_parent_id,
        lexical_parent_id=lexical_parent_id,
        qualified_name=qualified_name,
        scope=current_scope,
    )


def build_symbol_node(
    file_record: FileRecord,
    node_id: str,
    name: str,
    placement: DeclarationPlacement,
    *,
    spans: tuple[Range | None, Range | None, int, int],
    metadata: tuple[str | None, str | None, str, str, dict[str, Any]],
) -> SymbolNode:
    """Construct an immutable Python AST ``SymbolNode`` from normalized components.

    Args:
        file_record: Tracked ``FileRecord`` owning the declaration.
        node_id: Deterministic allocated ``SymbolNode`` identifier.
        name: Simple declared identifier name.
        placement: Resolved ``DeclarationPlacement`` for this symbol.
        spans: Tuple ``(decl_range, selection_range, start_line, end_line)``.
        metadata: Tuple ``(signature, doc_summary, content_hash, semantic_hash, payload)``.

    Returns:
        Constructed immutable ``SymbolNode``.
    """
    decl_range, selection_range, start_line, end_line = spans
    signature, doc_summary, content_hash, semantic_hash, payload = metadata
    return SymbolNode(
        id=node_id,
        repo_id=file_record.repo_id,
        file_id=file_record.id,
        language=Language.PYTHON,
        kind=placement.kind,
        name=name,
        qualified_name=placement.qualified_name,
        uri=file_record.uri,
        decl_range=decl_range,
        selection_range=selection_range,
        start_line=start_line,
        end_line=end_line,
        signature=signature,
        parent_id=placement.parent_id,
        visibility_hint=compute_visibility_hint(name, placement.scope),
        doc_summary=doc_summary,
        content_hash=content_hash,
        semantic_hash=semantic_hash,
        source="python-ast",
        confidence=1.0,
        scope=placement.scope,
        lexical_parent_id=placement.lexical_parent_id,
        payload=payload,
        last_indexed_at=file_record.last_indexed_at,
    )


__all__ = [
    "build_nested_qualified_name",
    "build_symbol_node",
    "compute_semantic_digest",
    "make_structural_edge_id",
    "resolve_symbol_placement",
]
