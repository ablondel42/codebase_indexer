"""Immutable Pydantic v2 query DTOs for ``QueryEngine`` operations."""

from pydantic import Field

from core.models.base import FrozenModel
from core.models.enums import EdgeKind, SymbolKind
from graph.models.references import ReferenceDirection


class ListFilesQuery(FrozenModel):
    """Filter, freshness, and pagination parameters for listing tracked files."""

    repo: str | None = None
    path_prefix: str | None = None
    pattern: str | None = None
    include_tests: bool = False
    check_freshness: bool = False
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)


class SearchSymbolsQuery(FrozenModel):
    """Full-text and pattern search parameters for repository symbols."""

    query: str
    repo: str | None = None
    kind: SymbolKind | None = None
    file_path: str | None = None
    limit: int = Field(default=20, ge=1)


class ListSymbolsQuery(FrozenModel):
    """Filter and pagination parameters for listing repository symbols."""

    repo: str | None = None
    kind: SymbolKind | None = None
    file_path: str | None = None
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)


class SymbolMembersQuery(FrozenModel):
    """Parameters for listing direct structural members of a module or class symbol."""

    symbol: str
    repo: str | None = None
    include_private: bool = True


class SymbolSnippetsQuery(FrozenModel):
    """Parameters for batched single-pass disk reading of multiple symbol snippets."""

    symbols: list[str] = Field(..., min_length=1)
    repo: str | None = None
    max_lines: int = Field(default=120, ge=1)
    offset: int = Field(default=0, ge=0)
    padding_lines: int = Field(default=0, ge=0)


class SubgraphQuery(FrozenModel):
    """Parameters for extracting a bounded BFS neighborhood subgraph slice."""

    symbols: list[str] = Field(..., min_length=1)
    repo: str | None = None
    depth: int = Field(default=1, ge=0, le=5)
    max_nodes: int = Field(default=100, ge=1)
    kinds: tuple[EdgeKind, ...] | None = None


class SymbolReferencesQuery(FrozenModel):
    """Direction, LSP refresh, preview, and pagination parameters for symbol references."""

    symbol: str
    repo: str | None = None
    direction: ReferenceDirection = ReferenceDirection.INCOMING
    refresh_lsp: bool = False
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)
    include_previews: bool = True


class OutlineQuery(FrozenModel):
    """Parameters for building a depth-bounded hierarchical file symbol outline."""

    file_path: str
    repo: str | None = None
    include_private: bool = True
    max_depth: int = Field(default=3, ge=0)


class SourceQuery(FrozenModel):
    """Line-window pagination and context padding parameters for a single symbol snippet."""

    symbol: str
    repo: str | None = None
    max_lines: int = Field(default=120, ge=1)
    offset: int = Field(default=0, ge=0)
    padding_lines: int = Field(default=0, ge=0)


class ContextQuery(FrozenModel):
    """Bounded item budgets and inline source options for symbol context assembly."""

    symbol: str
    repo: str | None = None
    include_source: bool = False
    max_source_lines: int = Field(default=80, ge=1)
    edge_limit: int = Field(default=25, ge=0)
    child_limit: int = Field(default=25, ge=0)
    sibling_limit: int = Field(default=15, ge=0)


__all__ = [
    "ContextQuery",
    "ListFilesQuery",
    "ListSymbolsQuery",
    "OutlineQuery",
    "SearchSymbolsQuery",
    "SourceQuery",
    "SubgraphQuery",
    "SymbolMembersQuery",
    "SymbolReferencesQuery",
    "SymbolSnippetsQuery",
]
