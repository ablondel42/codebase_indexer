"""Pure stateless graph predicates, line-range derivation, and edge peer formatting."""

from collections.abc import Mapping

from core.models.edge import Edge
from core.models.enums import SymbolScope
from core.models.node import SymbolNode, SymbolSummary
from core.models.range import Range
from graph.models.risk import RiskTarget

UNRESOLVED_ENDPOINT_PREFIXES: tuple[str, ...] = (
    "external_or_unresolved:",
    "unresolved_base:",
    "unresolved_call:",
)


def is_public_like(target: RiskTarget) -> bool:
    """Return ``True`` when ``target`` exposes a non-local public or dunder API surface.

    Function-scoped local declarations (``scope == "function"``) always return ``False``
    even if ``visibility_hint == "public"``.

    Args:
        target: Normalized ``RiskTarget`` symbol descriptor.

    Returns:
        ``True`` if modifying ``target`` touches public-like module or class surface.
    """
    if target.scope == SymbolScope.FUNCTION.value:
        return False
    if target.visibility_hint == "public":
        return True
    short_name = target.qualified_name.split(".")[-1] if target.qualified_name else ""
    if short_name.startswith("__") and short_name.endswith("__"):
        return True
    return not short_name.startswith("_")


def derive_target_lines(
    start_line: int | None,
    end_line: int | None,
    decl_range: Range | None,
) -> tuple[int | None, int | None]:
    """Resolve 1-based inclusive ``(start_line, end_line)`` with zero-based ``Range`` fallback.

    Args:
        start_line: Stored 1-based start line or ``None``.
        end_line: Stored 1-based end line or ``None``.
        decl_range: Optional zero-based declaration ``Range`` fallback.

    Returns:
        Tuple of 1-based ``(start_line, end_line)`` (elements may be ``None`` if unavailable).
    """
    if start_line is not None and end_line is not None:
        return start_line, end_line
    if decl_range is None:
        return start_line, end_line
    resolved_start = start_line if start_line is not None else decl_range.start.line + 1
    resolved_end = end_line if end_line is not None else decl_range.end.line + 1
    return resolved_start, resolved_end


def classify_edge_peer(edge: Edge, target_id: str) -> tuple[str, str]:
    """Classify ``edge`` direction relative to ``target_id`` and return ``(direction, peer_id)``.

    Args:
        edge: Directed graph ``Edge`` incident to ``target_id``.
        target_id: Anchor symbol identifier.

    Returns:
        Tuple ``(direction, peer_id)`` where ``direction`` is ``"self"``, ``"incoming"``,
        or ``"outgoing"``.
    """
    if edge.from_id == target_id and edge.to_id == target_id:
        return "self", target_id
    if edge.to_id == target_id:
        return "incoming", edge.from_id
    return "outgoing", edge.to_id


def _strip_placeholder_prefix(endpoint_id: str) -> str | None:
    """Strip a known unresolved placeholder prefix from ``endpoint_id`` when present.

    Args:
        endpoint_id: Candidate edge endpoint identifier.

    Returns:
        Unprefixed target label if ``endpoint_id`` starts with a placeholder prefix, else ``None``.
    """
    for prefix in UNRESOLVED_ENDPOINT_PREFIXES:
        if endpoint_id.startswith(prefix):
            return endpoint_id[len(prefix) :]
    return None


def _extract_payload_target(edge: Edge) -> str | None:
    """Extract a human-readable unresolved target label from ``edge.payload``.

    Args:
        edge: Graph ``Edge`` carrying AST extraction metadata.

    Returns:
        Target, base-class, or module string when present, else ``None``.
    """
    for key in ("target", "base_name", "module"):
        val = edge.payload.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def resolve_endpoint_name(
    endpoint_id: str,
    nodes_by_id: Mapping[str, SymbolNode | SymbolSummary],
    edge: Edge,
    *,
    allow_payload_fallback: bool = True,
) -> str | None:
    """Resolve an optional human-readable display name for one edge endpoint.

    Args:
        endpoint_id: Source or target symbol/placeholder identifier.
        nodes_by_id: Lookup mapping of resolved symbol nodes or summaries by ID.
        edge: Parent ``Edge`` providing payload fallback hints for target endpoints.
        allow_payload_fallback: Whether ``edge.payload`` keys may be used when unresolved.

    Returns:
        Resolved ``qualified_name`` / ``name`` / placeholder target, or ``None`` if unknown.
    """
    node = nodes_by_id.get(endpoint_id)
    if node is not None:
        return node.qualified_name or node.name
    stripped = _strip_placeholder_prefix(endpoint_id)
    if stripped is not None:
        return stripped
    if allow_payload_fallback:
        return _extract_payload_target(edge)
    return None


def resolve_peer_label(
    peer_id: str,
    nodes_by_id: Mapping[str, SymbolNode | SymbolSummary],
    edge: Edge,
) -> str:
    """Resolve a non-empty human-readable label for ``peer_id`` (falling back to ``peer_id``).

    Args:
        peer_id: Peer symbol or placeholder identifier on ``edge``.
        nodes_by_id: Lookup mapping of resolved symbol nodes or summaries by ID.
        edge: Graph ``Edge`` connecting the target symbol and ``peer_id``.

    Returns:
        Human-readable peer name, stripped placeholder target, payload hint, or ``peer_id``.
    """
    resolved = resolve_endpoint_name(
        peer_id,
        nodes_by_id,
        edge,
        allow_payload_fallback=(peer_id == edge.to_id),
    )
    return resolved if resolved else peer_id


def extract_edge_line(edge: Edge) -> int | None:
    """Extract the 1-based start line number from ``edge.evidence_range`` when present.

    Args:
        edge: Graph ``Edge`` entity.

    Returns:
        One-based evidence start line number, or ``None`` if ``evidence_range`` is absent.
    """
    if edge.evidence_range is None:
        return None
    return edge.evidence_range.start.line + 1


def format_site_location(file_path: str | None, line: int | None) -> str | None:
    """Format a compact ``"file:line"`` location string for a reference site.

    Args:
        file_path: Repository-relative POSIX file path or ``None``.
        line: One-based line number or ``None``.

    Returns:
        Formatted ``"{file_path}:{line}"``, ``file_path``, ``"line {line}"``, or ``None``.
    """
    if file_path and line is not None:
        return f"{file_path}:{line}"
    if file_path:
        return file_path
    if line is not None:
        return f"line {line}"
    return None


__all__ = [
    "UNRESOLVED_ENDPOINT_PREFIXES",
    "classify_edge_peer",
    "derive_target_lines",
    "extract_edge_line",
    "format_site_location",
    "is_public_like",
    "resolve_endpoint_name",
    "resolve_peer_label",
]
