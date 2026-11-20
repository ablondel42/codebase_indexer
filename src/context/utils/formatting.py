"""Pure stateless context formatting, import deduplication, edge balancing, and window slicing."""

from collections import deque
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from context.models.source import SnippetOptions, SourceWindow
from context.models.symbol_context import ContextEdge, FocusSymbol, SymbolContext
from core.models.edge import Edge
from core.models.enums import EdgeKind, SymbolKind, SymbolScope
from core.models.exceptions import ValidationError
from core.models.node import SymbolNode, SymbolSummary
from graph.utils.predicates import (
    UNRESOLVED_ENDPOINT_PREFIXES,
    derive_target_lines,
    extract_edge_line,
)

PLACEHOLDER_PREFIXES: tuple[str, ...] = UNRESOLVED_ENDPOINT_PREFIXES

_SEMANTIC_EDGE_ORDER: Mapping[EdgeKind, int] = MappingProxyType({
    EdgeKind.REFERENCES: 0,
    EdgeKind.CALLS: 1,
    EdgeKind.INHERITS: 2,
    EdgeKind.IMPORTS: 3,
})


def edges_have_placeholder_targets(edges: Sequence[Edge | ContextEdge]) -> bool:
    """Return ``True`` if any endpoint in ``edges`` begins with a placeholder prefix.

    Args:
        edges: Sequence of ``Edge`` or ``ContextEdge`` models.

    Returns:
        ``True`` when at least one ``from_id`` or ``to_id`` is an unresolved placeholder.
    """
    for edge in edges:
        if edge.from_id.startswith(PLACEHOLDER_PREFIXES) or edge.to_id.startswith(
            PLACEHOLDER_PREFIXES
        ):
            return True
    return False


def is_lsp_enriched(
    context: SymbolContext,
    raw_edges: Sequence[Edge] | None = None,
) -> bool:
    """Return ``True`` when ``context`` incorporates LSP reference enrichment.

    Args:
        context: Assembled ``SymbolContext`` model.
        raw_edges: Optional unbounded raw ``Edge`` sequence touching the focus symbol.

    Returns:
        ``True`` if ``reference_summary`` or any edge reflects LSP enrichment.
    """
    if (
        context.reference_summary.available is True
        or context.reference_summary.reference_source == "lsp"
    ):
        return True
    edges_to_check: Sequence[Edge | ContextEdge] = (
        raw_edges if raw_edges is not None else [*context.incoming_edges, *context.outgoing_edges]
    )
    return any(edge.kind == EdgeKind.REFERENCES or edge.source == "lsp" for edge in edges_to_check)


def is_private_symbol(node: SymbolNode | SymbolSummary) -> bool:
    """Return ``True`` when ``node`` is private-like or function-local.

    Args:
        node: ``SymbolNode`` or ``SymbolSummary`` to inspect.

    Returns:
        ``True`` for ``private_like``/``local`` hints or non-dunder ``_``-prefixed names.
    """
    if node.visibility_hint in ("private_like", "local"):
        return True
    return node.name.startswith("_") and not (
        node.name.startswith("__") and node.name.endswith("__")
    )


def format_import_label(edge: Edge) -> str | None:
    """Format an ``imports`` ``Edge`` into a concise human-readable import descriptor.

    Args:
        edge: ``imports`` ``Edge`` entity carrying ``module``/``symbol``/``alias`` payload.

    Returns:
        Formatted import label (e.g., ``"pkg.mod.Sym as Alias"``), or ``None`` if empty.
    """
    import_type = edge.payload.get("import_type")
    module_name = str(edge.payload.get("module") or "")
    symbol_name = str(edge.payload.get("symbol") or "")
    alias = edge.payload.get("alias")

    if import_type == "import" or not symbol_name:
        base = module_name
    else:
        base = f"{module_name}.{symbol_name}" if module_name else symbol_name

    if not base:
        return None
    if isinstance(alias, str) and alias:
        return f"{base} as {alias}"
    return base


def deduplicate_imports(
    import_edges: Sequence[Edge],
    module_node: SymbolNode | None = None,
) -> list[str]:
    """Collect deduplicated import labels ordered by first source appearance.

    Falls back to ``module_node.payload.get("imported_modules")`` when no ``imports``
    edges are available.

    Args:
        import_edges: Ordered ``imports`` ``Edge`` entities for a file.
        module_node: Optional root ``module`` ``SymbolNode`` fallback.

    Returns:
        Deduplicated list of formatted import strings.
    """
    seen: set[str] = set()
    imports: list[str] = []

    for edge in import_edges:
        label = format_import_label(edge)
        if label and label not in seen:
            seen.add(label)
            imports.append(label)

    if imports or module_node is None:
        return imports

    raw_modules = module_node.payload.get("imported_modules")
    if isinstance(raw_modules, list):
        for mod in raw_modules:
            if isinstance(mod, str) and mod and mod not in seen:
                seen.add(mod)
                imports.append(mod)
    return imports


def edge_sort_key(edge: Edge) -> tuple[int, str, int, str]:
    """Return a deterministic sort key ordering semantic edges by kind priority and site.

    Args:
        edge: Semantic ``Edge`` entity.

    Returns:
        Tuple ``(kind_priority, evidence_uri, line_or_zero, edge_id)``.
    """
    return (
        _SEMANTIC_EDGE_ORDER.get(edge.kind, 99),
        edge.evidence_uri or "",
        extract_edge_line(edge) or 0,
        edge.id,
    )


def select_balanced_edges(edges: Sequence[Edge], limit: int) -> list[Edge]:
    """Select up to ``limit`` semantic edges balanced round-robin across ``EdgeKind`` buckets.

    Args:
        edges: Deterministic sequence of semantic ``Edge`` entities.
        limit: Maximum number of edges to select.

    Returns:
        Selected list of ``Edge`` items sorted deterministically via ``edge_sort_key``.
    """
    if limit <= 0 or not edges:
        return []
    if len(edges) <= limit:
        return list(edges)

    buckets: dict[EdgeKind, deque[Edge]] = {}
    for edge in edges:
        buckets.setdefault(edge.kind, deque()).append(edge)

    active = deque(buckets.values())
    selected: list[Edge] = []
    while active and len(selected) < limit:
        bucket = active.popleft()
        selected.append(bucket.popleft())
        if bucket:
            active.append(bucket)

    selected.sort(key=edge_sort_key)
    return selected


def resolve_symbol_line_bounds(node: SymbolNode | SymbolSummary) -> tuple[int, int]:
    """Resolve 1-based inclusive ``(start_line, end_line)`` for ``node``.

    Args:
        node: ``SymbolNode`` or ``SymbolSummary`` entity.

    Returns:
        Tuple ``(start_line, end_line)`` of 1-based inclusive line numbers.

    Raises:
        ValidationError: If neither ``start_line``/``end_line`` nor ``decl_range`` is usable.
    """
    start_line, end_line = derive_target_lines(
        node.start_line,
        node.end_line,
        node.decl_range,
    )
    if start_line is not None and end_line is not None:
        return start_line, end_line
    raise ValidationError(
        f"Symbol {node.id} has no line range metadata.",
        context={"symbol_id": node.id},
    )


def is_nested_lexical_declaration(symbol: SymbolNode | FocusSymbol) -> bool:
    """Return ``True`` when ``symbol`` has a lexical parent and is not a standard class method."""
    if symbol.lexical_parent_id is None:
        return False
    return not (
        symbol.kind in (SymbolKind.METHOD, SymbolKind.ASYNC_METHOD)
        and symbol.scope == SymbolScope.CLASS
    )


def validate_window_bounds(start_line: int, end_line: int) -> None:
    """Validate 1-based inclusive ``start_line`` and ``end_line`` invariants."""
    if start_line < 1:
        raise ValidationError("start_line must be >= 1", context={"start_line": start_line})
    if end_line < start_line:
        raise ValidationError(
            "end_line must be >= start_line",
            context={"start_line": start_line, "end_line": end_line},
        )


def slice_source_window(
    all_lines: Sequence[str],
    start_line: int,
    end_line: int,
    options: SnippetOptions | None = None,
) -> SourceWindow:
    """Slice a 1-based source window producing both ``raw_source`` and ``numbered_source``.

    Applies ``options.padding_lines`` clamped to ``1..len(all_lines)``, then paginates via
    ``options.offset`` and ``options.max_lines``.

    Args:
        all_lines: Full split lines of the source file.
        start_line: Logical 1-based inclusive start line.
        end_line: Logical 1-based inclusive end line.
        options: Optional pagination and padding settings.

    Returns:
        Populated ``SourceWindow`` containing both clean and line-numbered source strings.

    Raises:
        ValidationError: If ``start_line < 1`` or ``end_line < start_line``.
    """
    validate_window_bounds(start_line, end_line)
    opts = options if options is not None else SnippetOptions()
    clamped_decl_end = min(end_line, len(all_lines))
    padded_start = max(1, start_line - opts.padding_lines)
    padded_end = min(len(all_lines), end_line + opts.padding_lines)

    if not all_lines or padded_start > len(all_lines):
        return SourceWindow(
            start_line=start_line,
            end_line=clamped_decl_end,
            window_start_line=padded_start,
            window_end_line=clamped_decl_end,
            raw_source="",
            numbered_source="",
            truncated=False,
            total_lines=0,
            returned_lines=0,
            next_offset=None,
        )

    slice_lines = list(all_lines[padded_start - 1 : padded_end])
    window = slice_lines[opts.offset : opts.offset + opts.max_lines]
    next_idx = opts.offset + len(window)
    truncated = next_idx < len(slice_lines)
    win_start = (
        padded_start + opts.offset if window else min(padded_start + opts.offset, padded_end)
    )
    win_end = win_start + len(window) - 1 if window else win_start

    return SourceWindow(
        start_line=start_line,
        end_line=clamped_decl_end,
        window_start_line=win_start,
        window_end_line=win_end,
        raw_source="\n".join(window),
        numbered_source="\n".join(f"{win_start + idx}: {line}" for idx, line in enumerate(window)),
        truncated=truncated,
        total_lines=len(slice_lines),
        returned_lines=len(window),
        next_offset=next_idx if truncated else None,
    )


__all__ = [
    "PLACEHOLDER_PREFIXES",
    "deduplicate_imports",
    "edge_sort_key",
    "edges_have_placeholder_targets",
    "format_import_label",
    "is_lsp_enriched",
    "is_nested_lexical_declaration",
    "is_private_symbol",
    "resolve_symbol_line_bounds",
    "select_balanced_edges",
    "slice_source_window",
    "validate_window_bounds",
]
