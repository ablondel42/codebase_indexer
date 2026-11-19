"""Repository graph statistics, database status, and read-only SQL report models."""

from typing import Any

from pydantic import Field

from core.models.base import FrozenModel
from core.models.runs import IndexRunRecord


class RepoGraphStats(FrozenModel):
    """Aggregated file, line, symbol-kind, and edge-kind statistics for a repository."""

    repo_id: str
    file_count: int = 0
    test_file_count: int = 0
    total_lines: int = 0
    node_count: int = 0
    edge_count: int = 0
    module_count: int = 0
    class_count: int = 0
    callable_count: int = 0
    local_callable_count: int = 0
    kind_counts: dict[str, int] = Field(default_factory=dict)
    edge_kind_counts: dict[str, int] = Field(default_factory=dict)


class LspCoverageMetrics(FrozenModel):
    """Non-module symbol LSP reference refresh coverage metrics."""

    non_module_symbols: int = 0
    refreshed_symbols: int = 0
    coverage_pct: float = 0.0
    lsp_reference_edges: int = 0


class TableCountEntry(FrozenModel):
    """Row count entry for a single SQLite table."""

    table: str
    row_count: int


class SelectedRepoSummary(FrozenModel):
    """Compact summary of the selected repository in ``DatabaseStatusReport``."""

    id: str
    name: str
    root_path: str
    last_indexed_at: str | None = None


class RepoStorageSummary(FrozenModel):
    """Per-repository storage summary including file, node, and edge counts."""

    id: str
    name: str
    root_path: str
    language: str
    files: int = 0
    nodes: int = 0
    edges: int = 0
    created_at: str
    last_indexed_at: str | None = None


class DatabaseStatusReport(FrozenModel):
    """Comprehensive database health, schema version, table count, and LSP report."""

    db_path: str
    size_bytes: int
    user_version: int
    expected_schema_version: int
    schema_ok: bool
    repo_count: int = 0
    selected_repo: SelectedRepoSummary | None = None
    tables: list[TableCountEntry] = Field(default_factory=list)
    edge_counts_by_kind: dict[str, int] = Field(default_factory=dict)
    lsp_coverage: LspCoverageMetrics = Field(default_factory=LspCoverageMetrics)
    latest_run: IndexRunRecord | None = None


class TableListReport(FrozenModel):
    """Database table listing with schema ``user_version`` and exact row counts."""

    db_path: str
    user_version: int
    tables: list[TableCountEntry] = Field(default_factory=list)


class SqlQueryResult(FrozenModel):
    """Result envelope for a guarded read-only SQL query."""

    db_path: str
    columns: list[str] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    rows: list[dict[str, Any]] = Field(default_factory=list)
