"""Graph edge entity, target update DTO, and reference/dependency statistics models."""

from typing import Any

from pydantic import Field, field_validator

from core.models.base import FrozenModel
from core.models.enums import EdgeKind, SymbolKind, SymbolScope
from core.models.range import Range
from core.utils.pathing import format_edge_location
from core.utils.timestamps import validate_iso_timestamp


class Edge(FrozenModel):
    """Directed relationship between two symbol identifiers in the repository graph."""

    id: str = Field(..., min_length=1)
    repo_id: str = Field(..., min_length=1)
    kind: EdgeKind
    from_id: str = Field(..., min_length=1)
    to_id: str = Field(..., min_length=1)
    source: str = "python-ast"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_file_id: str | None = None
    evidence_uri: str | None = None
    evidence_range: Range | None = None
    location: str | None = None
    last_indexed_at: str | None = None

    def model_post_init(self, __context: Any) -> None:
        """Populate Cmd+Click ``location`` from ``evidence_uri`` and ``evidence_range``."""
        if self.location is None and self.evidence_uri:
            computed = format_edge_location(self.evidence_uri, self.evidence_range)
            object.__setattr__(self, "location", computed)

    @field_validator("last_indexed_at")
    @classmethod
    def _check_timestamp(cls, value: str | None) -> str | None:
        """Validate ``last_indexed_at`` is an ISO 8601 timestamp when provided.

        Args:
            value: Candidate timestamp string or ``None``.

        Returns:
            Validated timestamp string or ``None``.

        Raises:
            ValueError: If ``value`` is not a valid ISO 8601 string.
        """
        return validate_iso_timestamp(value)


class EdgeTargetUpdate(FrozenModel):
    """Batch target-resolution update produced by ``SymbolLinker``."""

    edge_id: str
    to_id: str
    confidence: float = Field(..., ge=0.0, le=1.0)


class DirectedEdgeQuery(FrozenModel):
    """Filter and pagination parameters for directed edge lookups around a symbol."""

    symbol_id: str
    direction: str = "incoming"
    kinds: tuple[EdgeKind, ...] | None = None
    limit: int | None = None
    offset: int = 0


class EdgeBucketStats(FrozenModel):
    """Aggregated incoming edge, file, and module counts for a kind bucket."""

    edge_count: int = 0
    file_count: int = 0
    module_count: int = 0


class SymbolReferenceStats(FrozenModel):
    """Reference fanout summary and LSP enrichment freshness metadata for a symbol."""

    reference_count: int = 0
    referencing_file_count: int = 0
    referencing_module_count: int = 0
    available: bool = False
    last_refreshed_at: str | None = None
    reference_source: str = "ast"


class ReferencedByDetail(FrozenModel):
    """Caller or referencing symbol metadata joined with its containing file."""

    id: str
    repo_id: str
    file_id: str
    language: str = "python"
    kind: SymbolKind
    name: str
    qualified_name: str
    uri: str
    start_line: int | None = None
    end_line: int | None = None
    signature: str | None = None
    scope: SymbolScope | None = None
    lexical_parent_id: str | None = None
    file_path: str | None = None
    module_path: str | None = None


class ReferencingSiteRef(FrozenModel):
    """Distinct evidence file and module reference site descriptor."""

    file_id: str | None = None
    module_path: str | None = None


class LowConfidenceEdgeSample(FrozenModel):
    """Sampled low-confidence incoming edge IDs and total matching count."""

    edge_ids: list[str] = Field(default_factory=list)
    total_count: int = 0
