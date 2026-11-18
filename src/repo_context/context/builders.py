"""Context builders for assembling slim, bounded symbol-centered graph views."""

from collections import Counter

from sqlalchemy.engine import Connection

from repo_context.constants import (
    EDGE_KIND_CALLS,
    EDGE_KIND_CONTAINS,
    EDGE_KIND_IMPORTS,
    EDGE_KIND_INHERITS,
    EDGE_KIND_REFERENCES,
    EDGE_KIND_SCOPE_PARENT,
    SYMBOL_KIND_ASYNC_METHOD,
    SYMBOL_KIND_METHOD,
)
from repo_context.context.freshness import build_freshness
from repo_context.context.helpers import format_line_span
from repo_context.context.source_reader import read_symbol_source
from repo_context.context.summaries import build_confidence, build_structural_summary
from repo_context.exceptions import ParseError, PathSecurityError, ValidationError
from repo_context.graph import (
    build_reference_stats,
    get_child_symbols,
    get_files_by_ids,
    get_incoming_edges,
    get_lexical_child_symbols,
    get_lexical_parent_symbol,
    get_outgoing_edges,
    get_parent_symbol,
    get_symbol,
    get_symbols_by_ids,
    get_symbols_for_file,
)
from repo_context.models import SymbolContext

_STRUCTURAL_EDGE_KINDS = frozenset(
    {EDGE_KIND_CONTAINS, EDGE_KIND_SCOPE_PARENT, "SCOPE_PARENT"}
)
_SEMANTIC_EDGE_ORDER = {
    EDGE_KIND_REFERENCES: 0,
    EDGE_KIND_CALLS: 1,
    EDGE_KIND_INHERITS: 2,
    EDGE_KIND_IMPORTS: 3,
}


def _extract_node_line_bounds(node: dict) -> tuple[int | None, int | None]:
    """Return 1-based ``(start_line, end_line)`` from node fields or range_json."""
    start_line = node.get("start_line")
    end_line = node.get("end_line")
    if isinstance(start_line, int) and isinstance(end_line, int):
        return start_line, end_line

    rng = node.get("range_json")
    if isinstance(rng, dict):
        start = rng.get("start")
        end = rng.get("end")
        if isinstance(start, dict) and isinstance(end, dict):
            s_line = start.get("line")
            e_line = end.get("line")
            if isinstance(s_line, int) and isinstance(e_line, int):
                return s_line + 1, e_line + 1
    return start_line, end_line


def _extract_edge_line(edge: dict) -> int | None:
    """Return 1-based evidence line number from an edge's ``evidence_range_json``."""
    rng = edge.get("evidence_range_json")
    if isinstance(rng, dict):
        start = rng.get("start")
        if isinstance(start, dict) and isinstance(start.get("line"), int):
            return start["line"] + 1
    return None


def adapt_node_to_symbol(node: dict, *, file_info: dict | None = None) -> dict:
    """Adapt a stored node into a slim context symbol dict without unused blobs."""
    payload = node.get("payload_json")
    if not isinstance(payload, dict):
        payload = {}

    file_path = (file_info or {}).get("file_path") or payload.get("file_path")
    module_path = (file_info or {}).get("module_path") or payload.get("module_path")
    start_line, end_line = _extract_node_line_bounds(node)

    return {
        "id": node["id"],
        "kind": node["kind"],
        "name": node["name"],
        "qualified_name": node["qualified_name"],
        "file_path": file_path,
        "module_path": module_path,
        "start_line": start_line,
        "end_line": end_line,
        "signature": node.get("signature"),
        "doc_summary": node.get("doc_summary"),
        "parent_id": node.get("parent_id"),
        "scope": node.get("scope"),
        "lexical_parent_id": node.get("lexical_parent_id"),
        "visibility_hint": node.get("visibility_hint"),
        "confidence": node.get("confidence", 1.0),
    }


def _adapt_compact_symbol(node: dict) -> dict:
    """Adapt a child or sibling node into a minimal token-lean outline entry."""
    return {
        "id": node["id"],
        "kind": node["kind"],
        "name": node["name"],
        "lines": format_line_span(node),
        "signature": node.get("signature"),
        "doc_summary": node.get("doc_summary"),
    }


def adapt_edge_to_context_edge(
    edge: dict,
    *,
    peer_symbol: dict | None = None,
    file_info: dict | None = None,
    direction: str | None = None,
) -> dict:
    """Adapt a stored edge into a compact context relationship entry."""
    peer_name = peer_symbol.get("qualified_name") if peer_symbol else None
    if not peer_name:
        target_id = edge["from_id"] if direction == "incoming" else edge["to_id"]
        if ":" in target_id and target_id.startswith(
            ("external_or_unresolved:", "unresolved_base:", "unresolved_call:")
        ):
            peer_name = target_id.split(":", 1)[1]

    payload = edge.get("payload_json")
    if not peer_name and isinstance(payload, dict):
        peer_name = payload.get("target") or payload.get("base_name") or payload.get("module")

    return {
        "id": edge["id"],
        "kind": edge["kind"],
        "from_id": edge["from_id"],
        "to_id": edge["to_id"],
        "peer_name": peer_name,
        "file_path": (file_info or {}).get("file_path"),
        "line": _extract_edge_line(edge),
        "confidence": edge["confidence"],
        "source": edge.get("source"),
    }


def _edge_sort_key(edge: dict) -> tuple[int, str, int, str]:
    """Sort semantic edges deterministically by kind priority, URI, line, and ID."""
    return (
        _SEMANTIC_EDGE_ORDER.get(edge.get("kind", ""), 99),
        edge.get("evidence_uri") or "",
        _extract_edge_line(edge) or 0,
        edge.get("id", ""),
    )


def _select_balanced_edges(edges: list[dict], limit: int) -> list[dict]:
    """Select up to ``limit`` semantic edges balanced round-robin across kinds."""
    if limit <= 0 or not edges:
        return []
    if len(edges) <= limit:
        return edges

    buckets: dict[str, list[dict]] = {}
    for edge in edges:
        kind = edge.get("kind") or ""
        buckets.setdefault(kind, []).append(edge)

    selected: list[dict] = []
    offsets = {kind: 0 for kind in buckets}
    while len(selected) < limit:
        progressed = False
        for kind, items in buckets.items():
            idx = offsets[kind]
            if idx < len(items):
                selected.append(items[idx])
                offsets[kind] = idx + 1
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break

    selected.sort(key=_edge_sort_key)
    return selected


def _compute_relationship_counts(
    incoming_semantic: list[dict],
    outgoing_semantic: list[dict],
) -> dict[str, int]:
    """Compute exact per-relationship counts prior to bounding to ``edge_limit``."""
    in_counts = Counter(e.get("kind") for e in incoming_semantic)
    out_counts = Counter(e.get("kind") for e in outgoing_semantic)
    return {
        "callers": in_counts.get(EDGE_KIND_CALLS, 0),
        "callees": out_counts.get(EDGE_KIND_CALLS, 0),
        "imported_by": in_counts.get(EDGE_KIND_IMPORTS, 0),
        "imports": out_counts.get(EDGE_KIND_IMPORTS, 0),
        "subclasses": in_counts.get(EDGE_KIND_INHERITS, 0),
        "bases": out_counts.get(EDGE_KIND_INHERITS, 0),
        "references_in": in_counts.get(EDGE_KIND_REFERENCES, 0),
        "references_out": out_counts.get(EDGE_KIND_REFERENCES, 0),
    }


def build_symbol_context(
    conn: Connection,
    node_id: str,
    *,
    edge_limit: int = 20,
    include_source: bool = False,
    max_source_lines: int = 80,
) -> SymbolContext | None:
    """Build a slim, bounded context view around a symbol by its ID."""
    if edge_limit < 0:
        raise ValidationError("edge_limit must be >= 0", context={"edge_limit": edge_limit})

    focus_node = get_symbol(conn, node_id)
    if focus_node is None:
        return None

    # Fetch structural and lexical hierarchy
    structural_parent_node = get_parent_symbol(conn, focus_node)
    structural_children_nodes = get_child_symbols(conn, node_id)

    has_real_lexical_parent = (
        focus_node.get("lexical_parent_id") is not None
        and not (
            focus_node.get("kind") in (SYMBOL_KIND_METHOD, SYMBOL_KIND_ASYNC_METHOD)
            and focus_node.get("scope") == "class"
        )
    )
    lexical_parent_node = (
        get_lexical_parent_symbol(conn, focus_node) if has_real_lexical_parent else None
    )

    raw_lexical_children = get_lexical_child_symbols(conn, node_id)
    # Never duplicate identical structural class members under lexical_children
    lexical_children_nodes = [
        child
        for child in raw_lexical_children
        if child.get("scope") == "function"
    ]

    # Fetch raw incoming/outgoing edges once for stats, freshness, and bounded summaries
    outgoing_edges_raw = get_outgoing_edges(conn, node_id)
    incoming_edges_raw = get_incoming_edges(conn, node_id)
    all_raw_edges = [*incoming_edges_raw, *outgoing_edges_raw]

    incoming_semantic = sorted(
        [e for e in incoming_edges_raw if e.get("kind") not in _STRUCTURAL_EDGE_KINDS],
        key=_edge_sort_key,
    )
    outgoing_semantic = sorted(
        [e for e in outgoing_edges_raw if e.get("kind") not in _STRUCTURAL_EDGE_KINDS],
        key=_edge_sort_key,
    )

    bounded_incoming_raw = _select_balanced_edges(incoming_semantic, edge_limit)
    bounded_outgoing_raw = _select_balanced_edges(outgoing_semantic, edge_limit)

    # Direct structural siblings in the same file (never descendants of focus_node)
    all_file_symbols = get_symbols_for_file(conn, focus_node["file_id"])
    focus_parent_id = focus_node.get("parent_id")
    direct_siblings = [
        n
        for n in all_file_symbols
        if n["id"] != node_id and n.get("parent_id") == focus_parent_id
    ]

    # Batch-resolve peer nodes and file metadata for slim context adapters
    peer_ids: list[str] = []
    file_ids: list[str] = [focus_node["file_id"]]
    if structural_parent_node:
        file_ids.append(structural_parent_node["file_id"])
    if lexical_parent_node:
        file_ids.append(lexical_parent_node["file_id"])
    for edge in bounded_incoming_raw:
        peer_ids.append(edge["from_id"])
        if edge.get("evidence_file_id"):
            file_ids.append(edge["evidence_file_id"])
    for edge in bounded_outgoing_raw:
        peer_ids.append(edge["to_id"])
        if edge.get("evidence_file_id"):
            file_ids.append(edge["evidence_file_id"])

    peers_by_id = get_symbols_by_ids(conn, peer_ids)
    for peer in peers_by_id.values():
        if peer.get("file_id"):
            file_ids.append(peer["file_id"])
    files_by_id = get_files_by_ids(conn, file_ids)

    focus_file = files_by_id.get(focus_node["file_id"])
    focus_symbol = adapt_node_to_symbol(focus_node, file_info=focus_file)

    if include_source:
        try:
            source_payload = read_symbol_source(
                conn, node_id, max_lines=max_source_lines, offset=0
            )
            focus_symbol["source"] = source_payload["source"]
            if source_payload["truncated"]:
                focus_symbol["source_truncated"] = True
        except PathSecurityError:
            focus_symbol["source_error"] = (
                "Access denied: source file is missing, symlinked, or outside repository root."
            )
        except (OSError, ParseError):
            focus_symbol["source_error"] = "Unable to read symbol source from disk."
        except ValidationError:
            focus_symbol["source_error"] = "Symbol has no usable line range metadata."

    structural_parent = (
        adapt_node_to_symbol(
            structural_parent_node,
            file_info=files_by_id.get(structural_parent_node["file_id"]),
        )
        if structural_parent_node
        else None
    )
    lexical_parent = (
        adapt_node_to_symbol(
            lexical_parent_node,
            file_info=files_by_id.get(lexical_parent_node["file_id"]),
        )
        if lexical_parent_node
        else None
    )

    structural_children = [
        _adapt_compact_symbol(n) for n in structural_children_nodes[:edge_limit]
    ]
    lexical_children = [
        _adapt_compact_symbol(n) for n in lexical_children_nodes[:edge_limit]
    ]
    file_siblings = [_adapt_compact_symbol(n) for n in direct_siblings[:edge_limit]]

    incoming_edges = [
        adapt_edge_to_context_edge(
            e,
            peer_symbol=peers_by_id.get(e["from_id"]),
            file_info=files_by_id.get(e.get("evidence_file_id")),
            direction="incoming",
        )
        for e in bounded_incoming_raw
    ]
    outgoing_edges = [
        adapt_edge_to_context_edge(
            e,
            peer_symbol=peers_by_id.get(e["to_id"]),
            file_info=files_by_id.get(e.get("evidence_file_id")),
            direction="outgoing",
        )
        for e in bounded_outgoing_raw
    ]

    context = SymbolContext(
        focus_symbol=focus_symbol,
        structural_parent=structural_parent,
        structural_children=structural_children,
        lexical_parent=lexical_parent,
        lexical_children=lexical_children,
        incoming_edges=incoming_edges,
        outgoing_edges=outgoing_edges,
        file_siblings=file_siblings,
    )

    relationship_counts = _compute_relationship_counts(incoming_semantic, outgoing_semantic)
    context.reference_summary = build_reference_stats(conn, node_id)
    context.structural_summary = build_structural_summary(
        context,
        total_structural_child_count=len(structural_children_nodes),
        total_lexical_child_count=len(lexical_children_nodes),
        total_incoming_count=len(incoming_semantic),
        total_outgoing_count=len(outgoing_semantic),
        total_sibling_count=len(direct_siblings),
        relationship_counts=relationship_counts,
    )
    context.freshness = build_freshness(
        context,
        focus_node,
        conn,
        raw_edges=all_raw_edges,
        file_record=focus_file,
    )
    context.confidence = build_confidence(context, raw_edges=all_raw_edges)

    return context
