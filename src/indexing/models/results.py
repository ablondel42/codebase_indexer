"""Pydantic v2 result, option, and status models for batch and incremental indexing."""

from enum import StrEnum

from pydantic import Field

from core.models.base import FrozenModel
from core.models.diagnostics import RepoGraphStats
from core.models.repo import RepoRecord
from parsing.models.extraction import FileGraphExtraction
from parsing.models.linking import LinkResolutionReport


class ExtractionFailureEntry(FrozenModel):
    """Per-file AST extraction or path-confinement failure recorded during batch indexing."""

    file_path: str
    error: str


class LspBatchSummary(FrozenModel):
    """Summary counters and status from repository-wide LSP reference enrichment."""

    available: bool = False
    symbols_attempted: int = 0
    symbols_refreshed: int = 0
    errors: int = 0
    total_references: int = 0
    error: str | None = None


class BatchIndexOptions(FrozenModel):
    """Configuration overrides for a single ``BatchIndexer.index_repository`` run."""

    force: bool = False
    include_tests: bool | None = None
    enrich_lsp: bool = False


class BatchExtractionSummary(FrozenModel):
    """Intermediate extracted file graphs, failure entries, and extraction counters."""

    attempted_count: int = 0
    extractions: list[FileGraphExtraction] = Field(default_factory=list)
    failures: list[ExtractionFailureEntry] = Field(default_factory=list)
    nodes_extracted: int = 0
    edges_extracted: int = 0


class PersistedBatchOutcome(FrozenModel):
    """Completed audit run metadata and link report before optional LSP enrichment."""

    run_id: str
    repo: RepoRecord
    files_scanned: int
    files_indexed: int
    files_unchanged: int
    files_pruned: int
    failures: list[ExtractionFailureEntry] = Field(default_factory=list)
    nodes_extracted: int = 0
    edges_extracted: int = 0
    link_report: LinkResolutionReport


class BatchIndexResult(FrozenModel):
    """Final typed result of a full or incremental repository batch indexing run."""

    run_id: str
    repo: RepoRecord
    db_path: str
    files_scanned: int
    files_indexed: int
    files_unchanged: int
    files_pruned: int
    files_failed: int
    failures: list[ExtractionFailureEntry] = Field(default_factory=list)
    nodes_extracted: int = 0
    edges_extracted: int = 0
    link_report: LinkResolutionReport
    graph_stats: RepoGraphStats
    lsp_summary: LspBatchSummary | None = None


class IncrementalStatus(StrEnum):
    """Outcome status codes for incremental per-file reindex and delete operations."""

    REINDEXED = "reindexed"
    DELETED = "deleted"
    SKIPPED = "skipped"
    NOT_TRACKED = "not_tracked"
    PARSE_FAILED = "parse_failed"
    ERROR = "error"


class IncrementalFileResult(FrozenModel):
    """Structured result for a single file processed by ``IncrementalIndexer``."""

    file_path: str
    status: IncrementalStatus
    node_count: int = 0
    edge_count: int = 0
    deleted_node_count: int = 0
    deleted_edge_count: int = 0
    invalidated_reference_edge_count: int = 0
    invalidated_target_symbol_count: int = 0
    resolved_edge_count: int = 0
    reset_edge_count: int = 0
    error_message: str | None = None


class IncrementalBatchResult(FrozenModel):
    """Result of processing a collapsed batch of file change events."""

    repo_id: str
    results: list[IncrementalFileResult] = Field(default_factory=list)
    link_report: LinkResolutionReport | None = None
    dangling_lsp_edges_deleted: int = 0


__all__ = [
    "BatchExtractionSummary",
    "BatchIndexOptions",
    "BatchIndexResult",
    "ExtractionFailureEntry",
    "IncrementalBatchResult",
    "IncrementalFileResult",
    "IncrementalStatus",
    "LspBatchSummary",
    "PersistedBatchOutcome",
]
