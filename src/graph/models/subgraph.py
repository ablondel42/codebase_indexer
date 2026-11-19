"""Bounded neighborhood subgraph options and UI-ready subgraph slice models."""

from pydantic import Field

from core.models.base import FrozenModel
from core.models.edge import Edge
from core.models.enums import EdgeKind
from core.models.node import SymbolSummary


class SubgraphOptions(FrozenModel):
    """Traversal depth, node budget, and edge-kind filters for subgraph extraction."""

    depth: int = Field(default=1, ge=0, le=5)
    max_nodes: int = Field(default=100, ge=1)
    kinds: tuple[EdgeKind, ...] | None = None


class SubgraphSlice(FrozenModel):
    """Deduplicated symbol neighborhood subgraph for UI graph visualization."""

    root_symbol_ids: list[str]
    nodes: list[SymbolSummary]
    edges: list[Edge]
    depth: int
    truncated: bool = False


__all__ = [
    "SubgraphOptions",
    "SubgraphSlice",
]
