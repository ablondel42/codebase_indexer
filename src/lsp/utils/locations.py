"""Pure helpers for LSP location normalization, symbol containment, and edge construction."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from core.models.edge import Edge
from core.models.enums import PREFIX_EDGE, EdgeKind, SymbolKind
from core.models.exceptions import LspError
from core.models.node import SymbolNode, SymbolSummary
from core.models.range import Position, Range
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_iso
from lsp.models.protocol import NormalizedLocation

logger = get_logger("lsp.utils.locations")

CONFIDENCE_EXACT = 0.9
CONFIDENCE_MODULE = 0.7


def _extract_non_negative_int(mapping: Mapping[str, Any], key: str) -> int | None:
    """Extract a non-boolean, non-negative integer field from ``mapping``.

    Args:
        mapping: Source dictionary-like payload.
        key: Key name to inspect.

    Returns:
        Non-negative ``int`` value when valid, else ``None``.
    """
    raw = mapping.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def _parse_lsp_position(raw_pos: Any) -> Position | None:
    """Parse a raw LSP position mapping into a validated ``Position`` model.

    Args:
        raw_pos: Candidate ``{"line": int, "character": int}`` mapping.

    Returns:
        Validated ``Position`` instance or ``None`` if malformed.
    """
    if not isinstance(raw_pos, Mapping):
        return None
    line = _extract_non_negative_int(raw_pos, "line")
    character = _extract_non_negative_int(raw_pos, "character")
    if line is None or character is None:
        return None
    return Position(line=line, character=character)


def _parse_lsp_range(raw_range: Any) -> Range | None:
    """Parse a raw LSP range mapping into a validated ``Range`` model.

    Args:
        raw_range: Candidate ``{"start": ..., "end": ...}`` mapping.

    Returns:
        Validated ``Range`` instance or ``None`` if malformed.
    """
    if not isinstance(raw_range, Mapping):
        return None
    start = _parse_lsp_position(raw_range.get("start"))
    end = _parse_lsp_position(raw_range.get("end"))
    if start is None or end is None:
        return None
    return Range(start=start, end=end)


def normalize_location(raw_location: Mapping[str, Any]) -> NormalizedLocation | None:
    """Normalize a raw LSP ``Location`` or ``LocationLink`` mapping.

    Supports both ``Location`` (``uri`` + ``range``) and ``LocationLink``
    (``targetUri`` + ``targetSelectionRange`` / ``targetRange``). Malformed
    entries are logged at ``DEBUG`` level and skipped by returning ``None``.

    Args:
        raw_location: Raw location mapping returned by ``textDocument/references``.

    Returns:
        ``NormalizedLocation`` when valid, or ``None`` when missing required fields.
    """
    if not isinstance(raw_location, Mapping):
        logger.debug("Skipping non-mapping LSP location entry: %r", raw_location)
        return None

    if "uri" in raw_location and "range" in raw_location:
        raw_uri = raw_location.get("uri")
        raw_range = raw_location.get("range")
    else:
        raw_uri = raw_location.get("targetUri") or raw_location.get("uri")
        raw_range = (
            raw_location.get("targetSelectionRange")
            or raw_location.get("targetRange")
            or raw_location.get("range")
        )

    if not isinstance(raw_uri, str) or not raw_uri:
        logger.debug("Skipping LSP location with missing or invalid URI: %r", raw_location)
        return None

    parsed_range = _parse_lsp_range(raw_range)
    if parsed_range is None:
        logger.debug("Skipping LSP location with invalid range: %r", raw_location)
        return None

    return NormalizedLocation(uri=raw_uri, range=parsed_range)


def range_contains(outer: Range, inner: Range) -> bool:
    """Return ``True`` when ``outer`` inclusively contains ``inner``.

    Args:
        outer: Candidate enclosing range.
        inner: Candidate nested usage range.

    Returns:
        ``True`` if ``outer.start <= inner.start`` and ``outer.end >= inner.end``.
    """
    outer_start = (outer.start.line, outer.start.character)
    outer_end = (outer.end.line, outer.end.character)
    inner_start = (inner.start.line, inner.start.character)
    inner_end = (inner.end.line, inner.end.character)
    return outer_start <= inner_start and outer_end >= inner_end


def _symbol_sort_key(symbol: SymbolNode, decl_range: Range) -> tuple[tuple[int, int], int, str]:
    """Build deterministic specificity sort key for containing symbol selection.

    Args:
        symbol: Candidate containing ``SymbolNode``.
        decl_range: Validated ``decl_range`` of ``symbol``.

    Returns:
        Sort tuple ordering narrower spans first, non-module symbols before module
        symbols on span ties, and lexicographical ``symbol.id`` last.
    """
    span = (
        decl_range.end.line - decl_range.start.line,
        decl_range.end.character - decl_range.start.character,
    )
    module_penalty = 1 if symbol.kind == SymbolKind.MODULE else 0
    return (span, module_penalty, symbol.id)


def pick_smallest_containing_symbol(
    symbols: Sequence[SymbolNode],
    usage_range: Range,
) -> SymbolNode | None:
    """Select the narrowest symbol in ``symbols`` whose ``decl_range`` contains ``usage_range``.

    Prefers the narrowest line/character span, breaking exact span ties in favor
    of non-module declarations before falling back to ``symbol.id``.

    Args:
        symbols: Sequence of ``SymbolNode`` declarations belonging to a file.
        usage_range: Zero-based codepoint ``Range`` of the reference site.

    Returns:
        Most specific containing ``SymbolNode``, or ``None`` if none contains ``usage_range``.
    """
    candidates: list[tuple[SymbolNode, Range]] = [
        (sym, sym.decl_range)
        for sym in symbols
        if sym.decl_range is not None and range_contains(sym.decl_range, usage_range)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: _symbol_sort_key(item[0], item[1]))
    return candidates[0][0]


def find_module_node_for_file(symbols: Sequence[SymbolNode]) -> SymbolNode | None:
    """Return the first ``SymbolKind.MODULE`` node in ``symbols``, if present.

    Args:
        symbols: Sequence of ``SymbolNode`` declarations belonging to a file.

    Returns:
        Module ``SymbolNode`` or ``None`` if absent.
    """
    for symbol in symbols:
        if symbol.kind == SymbolKind.MODULE:
            return symbol
    return None


def get_reference_query_position(symbol: SymbolNode | SymbolSummary) -> Position:
    """Return the zero-based codepoint ``Position`` used to query LSP references.

    Prefers ``symbol.selection_range.start`` and falls back to ``symbol.decl_range.start``.

    Args:
        symbol: Target ``SymbolNode`` or ``SymbolSummary`` to inspect.

    Returns:
        Zero-based codepoint ``Position`` for ``textDocument/references``.

    Raises:
        LspError: If neither ``selection_range`` nor ``decl_range`` is populated.
    """
    if symbol.selection_range is not None:
        return symbol.selection_range.start
    if symbol.decl_range is not None:
        return symbol.decl_range.start
    raise LspError(
        f"Symbol {symbol.id} has no stored range for references query.",
        context={"symbol_id": symbol.id},
    )


def _add_unique_uri(candidates: list[str], value: str) -> None:
    """Append ``value`` to ``candidates`` when non-empty and not yet present.

    Args:
        candidates: Mutable list accumulating candidate URI strings.
        value: Candidate URI string to append.
    """
    if value and value not in candidates:
        candidates.append(value)


def _add_path_uris(candidates: list[str], path: Path) -> None:
    """Append ``as_uri()`` and percent-decoded URI variants for an absolute ``Path``.

    Args:
        candidates: Mutable list accumulating candidate URI strings.
        path: Candidate filesystem ``Path``.
    """
    if not path.is_absolute():
        return
    try:
        path_uri = path.as_uri()
    except ValueError as exc:
        logger.debug("Skipping invalid URI conversion for path %s: %s", path, exc)
        return
    _add_unique_uri(candidates, path_uri)
    _add_unique_uri(candidates, unquote(path_uri))


def _normalize_parsed_uri_path(raw_path: str) -> Path:
    """Decode a percent-escaped URI path and normalize Windows drive prefixes.

    Args:
        raw_path: Raw ``urlparse(uri).path`` component.

    Returns:
        Normalized ``Path`` instance.
    """
    decoded = unquote(raw_path)
    if len(decoded) >= 3 and decoded[0] == "/" and decoded[2] == ":" and decoded[1].isalpha():
        decoded = decoded[1:]
    return Path(decoded)


def extract_uri_candidates(uri: str) -> tuple[list[str], Path | None]:
    """Derive candidate ``file://`` URI strings and canonical ``Path`` from a URI.

    Args:
        uri: Raw ``file://`` URI string reported by an LSP server.

    Returns:
        Tuple of ``(candidate_uris, resolved_path_or_none)``.
    """
    if not uri:
        return [], None

    candidates: list[str] = []
    _add_unique_uri(candidates, uri)
    _add_unique_uri(candidates, unquote(uri))

    parsed = urlparse(uri)
    if parsed.scheme not in ("file", "") or not parsed.path:
        return candidates, None

    raw_path = _normalize_parsed_uri_path(parsed.path)
    _add_path_uris(candidates, raw_path)

    try:
        resolved_path = raw_path.resolve()
    except OSError as exc:
        logger.debug("Cannot resolve filesystem path %s for URI %s: %s", raw_path, uri, exc)
        resolved_path = raw_path
    _add_path_uris(candidates, resolved_path)

    return candidates, resolved_path


def build_reference_edge(
    repo_id: str,
    target: SymbolNode,
    source: SymbolNode,
    file_id: str,
    evidence_uri: str,
    usage_range: Range,
) -> Edge:
    """Construct a deterministic LSP ``references`` ``Edge`` model.

    Derives ``mapping_mode`` and ``confidence`` from ``source.kind``
    (``SymbolKind.MODULE`` -> ``"module_fallback"`` / ``0.7``, otherwise
    ``"exact_symbol"`` / ``0.9``).

    Args:
        repo_id: Owning repository identifier.
        target: Referenced target ``SymbolNode``.
        source: Referencing source ``SymbolNode`` (symbol or module fallback).
        file_id: Evidence ``FileRecord`` identifier containing the reference site.
        evidence_uri: Evidence ``file://`` URI reported for the reference site.
        usage_range: Zero-based codepoint ``Range`` of the reference site.

    Returns:
        Validated immutable ``Edge`` instance with ``source="lsp"``.
    """
    is_module_fallback = source.kind == SymbolKind.MODULE
    mapping_mode = "module_fallback" if is_module_fallback else "exact_symbol"
    confidence = CONFIDENCE_MODULE if is_module_fallback else CONFIDENCE_EXACT
    edge_id = (
        f"{PREFIX_EDGE}{repo_id}:{EdgeKind.REFERENCES.value}:"
        f"{source.id}->{target.id}:{usage_range.start.line}:{usage_range.start.character}"
    )
    return Edge(
        id=edge_id,
        repo_id=repo_id,
        kind=EdgeKind.REFERENCES,
        from_id=source.id,
        to_id=target.id,
        source="lsp",
        confidence=confidence,
        evidence_file_id=file_id,
        evidence_uri=evidence_uri,
        evidence_range=usage_range,
        payload={
            "mapping_mode": mapping_mode,
            "target_symbol_kind": target.kind.value,
            "source_symbol_kind": source.kind.value,
            "source_symbol_scope": source.scope.value if source.scope is not None else None,
        },
        last_indexed_at=utc_now_iso(),
    )
