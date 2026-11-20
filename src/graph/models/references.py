"""Reference query options, site entries, and paginated symbol reference report models."""

from enum import StrEnum

from pydantic import Field

from core.models.base import FrozenModel
from core.models.edge import SymbolReferenceStats
from core.models.enums import EdgeKind, SymbolKind, SymbolScope


class ReferenceDirection(StrEnum):
    """Supported traversal directions when inspecting symbol references."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"
    BOTH = "both"


class ReferenceTargetSummary(FrozenModel):
    """Compact metadata describing the target symbol of a reference report."""

    id: str
    kind: SymbolKind
    name: str
    qualified_name: str
    location: str | None = None
    file_path: str | None = None
    module_path: str | None = None
    lines: str | None = None
    signature: str | None = None
    doc_summary: str | None = None
    scope: SymbolScope | None = None
    visibility_hint: str | None = None


class ReferenceQueryOptions(FrozenModel):
    """Filter, pagination, and preview options for symbol reference inspection."""

    direction: ReferenceDirection = ReferenceDirection.INCOMING
    kinds: tuple[EdgeKind, ...] | None = None
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)
    include_previews: bool = True
    max_preview_chars: int = Field(default=160, ge=4)


class ReferenceSiteEntry(FrozenModel):
    """Single incoming, outgoing, or self-referential relationship site entry."""

    id: str = ""
    direction: str
    kind: EdgeKind
    from_id: str
    to_id: str
    peer_id: str
    peer_name: str
    from_name: str | None = None
    to_name: str | None = None
    file_path: str | None = None
    line: int | None = None
    column: int | None = None
    location: str | None = None
    confidence: float
    source: str
    preview: str | None = None


class SymbolReferencesReport(FrozenModel):
    """Paginated reference and dependency inspection report for a target symbol."""

    symbol: ReferenceTargetSummary
    direction: ReferenceDirection
    total_count: int
    returned_count: int
    offset: int
    limit: int
    has_more: bool
    stats: SymbolReferenceStats
    references: list[ReferenceSiteEntry] = Field(default_factory=list)


__all__ = [
    "ReferenceDirection",
    "ReferenceQueryOptions",
    "ReferenceSiteEntry",
    "ReferenceTargetSummary",
    "SymbolReferencesReport",
]
