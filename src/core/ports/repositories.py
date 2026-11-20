"""Hexagonal repository ``Protocol`` ports isolating persistence from domain layers."""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from core.models.diagnostics import (
    DatabaseStatusReport,
    RepoGraphStats,
    RepoStorageSummary,
    TableListReport,
)
from core.models.edge import (
    DirectedEdgeQuery,
    Edge,
    EdgeBucketStats,
    EdgeTargetUpdate,
    LowConfidenceEdgeSample,
    ReferencingSiteRef,
)
from core.models.enums import EdgeKind, SymbolKind
from core.models.file import (
    FilePageQuery,
    FilePageResult,
    FileRecord,
    FileSymbolCountRow,
)
from core.models.node import (
    SymbolNode,
    SymbolPageQuery,
    SymbolPageResult,
    SymbolSearchQuery,
    SymbolSummary,
)
from core.models.repo import RepoRecord
from core.models.runs import (
    IndexRunMetrics,
    IndexRunRecord,
    ReferenceRefreshState,
)
from core.models.search import (
    TextCandidateQuery,
    TextDocumentMeta,
    TextDocumentRecord,
)


@runtime_checkable
class RepoRepositoryProtocol(Protocol):
    """Persistence contract for indexed ``RepoRecord`` entities."""

    def upsert(self, repo: RepoRecord) -> None:
        """Insert or update a repository record.

        Args:
            repo: Repository entity to persist.
        """
        ...

    def get_by_id(self, repo_id: str) -> RepoRecord | None:
        """Fetch a repository record by its exact ID.

        Args:
            repo_id: Canonical repository identifier.

        Returns:
            Matching ``RepoRecord`` or ``None`` if absent.
        """
        ...

    def list_all(self) -> list[RepoRecord]:
        """List all indexed repositories ordered deterministically.

        Returns:
            List of ``RepoRecord`` entities.
        """
        ...

    def resolve_id(self, repo_id_or_name: str | None = None) -> str:
        """Resolve an explicit repository ID/name or single-repo default to a canonical ID.

        Args:
            repo_id_or_name: Optional repository ID, short name, or ``None``.

        Returns:
            Resolved ``repo_id`` string.
        """
        ...


@runtime_checkable
class FileRepositoryProtocol(Protocol):
    """Persistence contract for tracked ``FileRecord`` entities."""

    def upsert(self, file_record: FileRecord) -> None:
        """Insert or update a single tracked file record.

        Args:
            file_record: File entity to persist.
        """
        ...

    def upsert_many(self, file_records: Sequence[FileRecord]) -> None:
        """Insert or update a batch of tracked file records.

        Args:
            file_records: Sequence of ``FileRecord`` entities to persist.
        """
        ...

    def get_by_id(self, file_id: str) -> FileRecord | None:
        """Fetch a file record by its exact ID.

        Args:
            file_id: Canonical file identifier.

        Returns:
            Matching ``FileRecord`` or ``None`` if absent.
        """
        ...

    def get_by_ids(self, file_ids: Sequence[str]) -> dict[str, FileRecord]:
        """Batch-fetch file records keyed by ``file_id``.

        Args:
            file_ids: Sequence of canonical file identifiers.

        Returns:
            Mapping from ``file_id`` to ``FileRecord``.
        """
        ...

    def get_by_path(self, repo_id: str, file_path: str) -> FileRecord | None:
        """Fetch a file record by repository ID and relative POSIX path.

        Args:
            repo_id: Owning repository identifier.
            file_path: Repository-relative POSIX file path.

        Returns:
            Matching ``FileRecord`` or ``None`` if absent.
        """
        ...

    def resolve_by_uri(self, uri: str, repo_id: str | None = None) -> FileRecord | None:
        """Resolve a ``file://`` URI (including normalized path variants) to a ``FileRecord``.

        Args:
            uri: Candidate ``file://`` URI string.
            repo_id: Optional repository scope filter.

        Returns:
            Matching ``FileRecord`` or ``None`` if absent.
        """
        ...

    def list_for_repo(self, repo_id: str, *, include_tests: bool = True) -> list[FileRecord]:
        """List tracked files belonging to ``repo_id``.

        Args:
            repo_id: Owning repository identifier.
            include_tests: Whether test files should be included.

        Returns:
            Ordered list of ``FileRecord`` entities.
        """
        ...

    def list_page(self, query: FilePageQuery) -> FilePageResult:
        """Return a filtered, paginated page of tracked repository files.

        Args:
            query: Pagination and path-prefix filter parameters.

        Returns:
            ``FilePageResult`` containing ``total_count`` and ``items``.
        """
        ...

    def list_with_symbol_counts(
        self,
        repo_id: str,
        *,
        include_tests: bool = True,
    ) -> list[FileSymbolCountRow]:
        """List tracked files paired with their extracted symbol counts.

        Args:
            repo_id: Owning repository identifier.
            include_tests: Whether test files should be included.

        Returns:
            Ordered list of ``FileSymbolCountRow`` records.
        """
        ...

    def prune_not_in_ids(self, repo_id: str, keep_file_ids: set[str]) -> int:
        """Delete files in ``repo_id`` whose IDs are not in ``keep_file_ids``.

        Args:
            repo_id: Owning repository identifier.
            keep_file_ids: Set of active ``file_id``s to retain.

        Returns:
            Number of pruned file records.
        """
        ...

    def delete_file_graph(
        self,
        file_id: str,
        *,
        preserve_incoming_ast_edges: bool = True,
    ) -> int:
        """Delete a file record, its symbol nodes, owned edges, and stale LSP references.

        Args:
            file_id: Target file identifier to remove.
            preserve_incoming_ast_edges: When ``True``, retains external callers' AST
                edges so batch re-linking can reset them cleanly to placeholders.

        Returns:
            Number of deleted file rows (``0`` or ``1``).
        """
        ...


@runtime_checkable
class NodeRepositoryProtocol(Protocol):
    """Persistence contract for ``SymbolNode`` entities and ``SymbolSummary`` projections."""

    def upsert(self, node: SymbolNode) -> None:
        """Insert or update a single symbol node.

        Args:
            node: Symbol node entity to persist.
        """
        ...

    def upsert_many(self, nodes: Sequence[SymbolNode]) -> None:
        """Insert or update a batch of symbol nodes.

        Args:
            nodes: Sequence of ``SymbolNode`` entities to persist.
        """
        ...

    def get_by_id(self, node_id: str) -> SymbolNode | None:
        """Fetch a symbol node by its exact ID.

        Args:
            node_id: Canonical symbol identifier.

        Returns:
            Matching ``SymbolNode`` or ``None`` if absent.
        """
        ...

    def get_by_ids(self, node_ids: Sequence[str]) -> dict[str, SymbolNode]:
        """Batch-fetch symbol nodes keyed by ``node_id``.

        Args:
            node_ids: Sequence of canonical symbol identifiers.

        Returns:
            Mapping from ``node_id`` to ``SymbolNode``.
        """
        ...

    def get_summary_by_id(self, node_id: str) -> SymbolSummary | None:
        """Fetch a single ``SymbolSummary`` joined with file and module paths.

        Args:
            node_id: Canonical symbol identifier.

        Returns:
            Matching ``SymbolSummary`` or ``None`` if absent.
        """
        ...

    def get_summaries_by_ids(self, node_ids: Sequence[str]) -> dict[str, SymbolSummary]:
        """Batch-fetch ``SymbolSummary`` projections keyed by ``node_id``.

        Args:
            node_ids: Sequence of canonical symbol identifiers.

        Returns:
            Mapping from ``node_id`` to ``SymbolSummary``.
        """
        ...

    def get_by_qualified_name(
        self,
        repo_id: str,
        qualified_name: str,
        kind: SymbolKind | None = None,
    ) -> SymbolNode | None:
        """Fetch a symbol node by repository ID, ``qualified_name``, and optional ``kind``.

        Args:
            repo_id: Owning repository identifier.
            qualified_name: Dotted symbol qualified name.
            kind: Optional symbol kind filter.

        Returns:
            Matching ``SymbolNode`` or ``None`` if absent.
        """
        ...

    def list_for_file(self, file_id: str) -> list[SymbolNode]:
        """List all symbol nodes declared in ``file_id`` ordered by source position.

        Args:
            file_id: Containing file identifier.

        Returns:
            Ordered list of ``SymbolNode`` entities.
        """
        ...

    def list_for_repo(self, repo_id: str) -> list[SymbolNode]:
        """List all symbol nodes belonging to ``repo_id``.

        Args:
            repo_id: Owning repository identifier.

        Returns:
            Ordered list of ``SymbolNode`` entities.
        """
        ...

    def list_children(self, parent_id: str) -> list[SymbolNode]:
        """List direct structural child symbol nodes whose ``parent_id`` matches.

        Args:
            parent_id: Structural parent symbol identifier.

        Returns:
            Ordered list of child ``SymbolNode`` entities.
        """
        ...

    def list_lexical_children(self, lexical_parent_id: str) -> list[SymbolNode]:
        """List nested lexical child symbol nodes whose ``lexical_parent_id`` matches.

        Args:
            lexical_parent_id: Enclosing lexical scope symbol identifier.

        Returns:
            Ordered list of nested ``SymbolNode`` entities.
        """
        ...

    def list_member_summaries(
        self,
        parent_id: str,
        *,
        include_private: bool = True,
    ) -> list[SymbolSummary]:
        """List direct member ``SymbolSummary`` items of a class or module symbol.

        Args:
            parent_id: Containing class or module symbol identifier.
            include_private: Whether private-like members should be included.

        Returns:
            Ordered list of member ``SymbolSummary`` projections.
        """
        ...

    def find_by_name(
        self,
        repo_id: str,
        name_pattern: str,
        kind: SymbolKind | None = None,
        limit: int = 50,
    ) -> list[SymbolNode]:
        """Find symbol nodes matching a SQL ``LIKE`` name pattern.

        Args:
            repo_id: Owning repository identifier.
            name_pattern: Exact or wildcard ``LIKE`` pattern.
            kind: Optional symbol kind filter.
            limit: Maximum number of results to return.

        Returns:
            Matching ``SymbolNode`` entities.
        """
        ...

    def search_fts(self, query: SymbolSearchQuery) -> list[SymbolSummary]:
        """Search symbols via FTS5 (with deterministic ``LIKE`` fallback).

        Args:
            query: Search query, optional kind/path filters, and result limit.

        Returns:
            Ranked list of matching ``SymbolSummary`` items.
        """
        ...

    def list_paginated_summaries(self, query: SymbolPageQuery) -> SymbolPageResult:
        """Return a filtered, paginated page of ``SymbolSummary`` items.

        Args:
            query: Pagination and kind/file filter parameters.

        Returns:
            ``SymbolPageResult`` containing ``total_count`` and ``items``.
        """
        ...

    def resolve_candidates(
        self,
        repo_id: str | None,
        selector: str,
        limit: int = 20,
    ) -> list[SymbolSummary]:
        """Find candidate symbols matching an ID, ``file:symbol``, qualified name, or name.

        Args:
            repo_id: Optional repository scope filter.
            selector: User-provided symbol selector string.
            limit: Maximum number of candidate summaries to return.

        Returns:
            Ordered list of matching ``SymbolSummary`` candidates.
        """
        ...


@runtime_checkable
class EdgeRepositoryProtocol(Protocol):
    """Persistence contract for ``Edge`` relationships and reference queries."""

    def upsert(self, edge: Edge) -> None:
        """Insert or update a single graph edge.

        Args:
            edge: Graph edge entity to persist.
        """
        ...

    def upsert_many(self, edges: Sequence[Edge]) -> None:
        """Insert or update a batch of graph edges.

        Args:
            edges: Sequence of ``Edge`` entities to persist.
        """
        ...

    def get_by_id(self, edge_id: str) -> Edge | None:
        """Fetch an edge by its exact ID.

        Args:
            edge_id: Canonical edge identifier.

        Returns:
            Matching ``Edge`` or ``None`` if absent.
        """
        ...

    def list_for_repo(
        self,
        repo_id: str,
        kinds: Sequence[EdgeKind] | None = None,
    ) -> list[Edge]:
        """List edges belonging to ``repo_id``, optionally filtered by ``kinds``.

        Args:
            repo_id: Owning repository identifier.
            kinds: Optional sequence of ``EdgeKind`` values to include.

        Returns:
            Ordered list of ``Edge`` entities.
        """
        ...

    def list_for_file(self, file_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        """List edges evidenced by ``file_id``, optionally filtered by ``kind``.

        Args:
            file_id: Evidence file identifier.
            kind: Optional ``EdgeKind`` filter.

        Returns:
            Ordered list of ``Edge`` entities.
        """
        ...

    def list_outgoing(self, from_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        """List outgoing edges originating from ``from_id``.

        Args:
            from_id: Source symbol identifier.
            kind: Optional ``EdgeKind`` filter.

        Returns:
            Ordered list of outgoing ``Edge`` entities.
        """
        ...

    def list_incoming(self, to_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        """List incoming edges targeting ``to_id``.

        Args:
            to_id: Target symbol identifier.
            kind: Optional ``EdgeKind`` filter.

        Returns:
            Ordered list of incoming ``Edge`` entities.
        """
        ...

    def list_for_target_and_kinds(
        self,
        to_id: str,
        kinds: Sequence[EdgeKind],
    ) -> list[Edge]:
        """List incoming edges targeting ``to_id`` restricted to ``kinds``.

        Args:
            to_id: Target symbol identifier.
            kinds: Allowed ``EdgeKind`` values.

        Returns:
            Ordered list of matching ``Edge`` entities.
        """
        ...

    def list_directed(self, query: DirectedEdgeQuery) -> list[Edge]:
        """List incoming, outgoing, or bidirectional edges ordered by evidence site.

        Args:
            query: Direction, kind filter, and pagination parameters.

        Returns:
            Ordered list of matching ``Edge`` entities.
        """
        ...

    def count_directed(self, query: DirectedEdgeQuery) -> int:
        """Count total edges matching a ``DirectedEdgeQuery`` (ignoring limit/offset).

        Args:
            query: Direction and kind filter parameters.

        Returns:
            Total matching edge count.
        """
        ...

    def list_neighborhood_edges(
        self,
        symbol_ids: Sequence[str],
        kinds: Sequence[EdgeKind] | None = None,
    ) -> list[Edge]:
        """List edges touching any endpoint in ``symbol_ids``.

        Args:
            symbol_ids: Seed symbol identifiers in the neighborhood frontier.
            kinds: Optional sequence of ``EdgeKind`` values to include.

        Returns:
            Deduplicated list of ``Edge`` entities incident to ``symbol_ids``.
        """
        ...

    def list_referencing_sites(
        self,
        target_id: str,
        kinds: Sequence[EdgeKind],
    ) -> list[ReferencingSiteRef]:
        """List distinct ``(file_id, module_path)`` reference sites targeting ``target_id``.

        Args:
            target_id: Target symbol identifier.
            kinds: ``EdgeKind`` bucket to inspect.

        Returns:
            List of ``ReferencingSiteRef`` items.
        """
        ...

    def compute_bucket_stats(
        self,
        target_id: str,
        kinds: Sequence[EdgeKind],
    ) -> EdgeBucketStats:
        """Compute incoming edge, file, and module counts for ``target_id`` across ``kinds``.

        Args:
            target_id: Target symbol identifier.
            kinds: ``EdgeKind`` bucket to aggregate.

        Returns:
            ``EdgeBucketStats`` counts.
        """
        ...

    def has_edge_between_or_for(self, symbol_id: str, kind: EdgeKind) -> bool:
        """Return ``True`` if ``symbol_id`` participates in any edge of ``kind``.

        Args:
            symbol_id: Symbol identifier (checked on ``from_id`` or ``to_id``).
            kind: ``EdgeKind`` to check.

        Returns:
            ``True`` when at least one matching edge exists.
        """
        ...

    def has_incoming_kind(self, target_id: str, kind: EdgeKind) -> bool:
        """Return ``True`` if ``target_id`` has at least one incoming edge of ``kind``.

        Args:
            target_id: Target symbol identifier.
            kind: Incoming ``EdgeKind`` to check.

        Returns:
            ``True`` when at least one incoming edge of ``kind`` exists.
        """
        ...

    def sample_low_confidence_references(
        self,
        target_ids: Sequence[str],
        threshold: float = 0.8,
        limit: int = 25,
    ) -> LowConfidenceEdgeSample:
        """Sample incoming reference/dependency edges with ``confidence < threshold``.

        Args:
            target_ids: Target symbol identifiers to inspect.
            threshold: Exclusive upper confidence bound (default ``0.8``).
            limit: Maximum number of sampled edge IDs to return.

        Returns:
            ``LowConfidenceEdgeSample`` with sampled IDs and total matching count.
        """
        ...

    def latest_indexed_at_for_symbol(self, symbol_id: str) -> str | None:
        """Return the latest ``last_indexed_at`` timestamp across edges touching ``symbol_id``.

        Args:
            symbol_id: Target or source symbol identifier.

        Returns:
            Latest ISO 8601 timestamp string or ``None`` if no timestamped edges exist.
        """
        ...

    def update_resolved_targets(self, updates: Sequence[EdgeTargetUpdate]) -> int:
        """Apply batch ``to_id`` and ``confidence`` updates emitted by ``SymbolLinker``.

        Args:
            updates: Sequence of ``EdgeTargetUpdate`` specifications.

        Returns:
            Number of updated edge rows.
        """
        ...

    def replace_lsp_references_for_target(
        self,
        repo_id: str,
        target_symbol_id: str,
        edges: Sequence[Edge],
    ) -> None:
        """Atomically replace all LSP ``references`` edges targeting ``target_symbol_id``.

        Args:
            repo_id: Owning repository identifier.
            target_symbol_id: Enriched target symbol identifier.
            edges: Replacement LSP ``references`` edges to persist.
        """
        ...

    def delete_owned_for_file(self, file_id: str) -> int:
        """Delete edges evidenced by or originating from symbols inside ``file_id``.

        Preserves external callers' AST edges targeting symbols in ``file_id`` so
        ``SymbolLinker`` can reset them to unresolved placeholders cleanly.

        Args:
            file_id: Containing file identifier.

        Returns:
            Number of deleted edge rows.
        """
        ...

    def delete_all_for_file(self, file_id: str) -> int:
        """Delete all edges evidenced by or touching symbols inside ``file_id``.

        Args:
            file_id: Containing file identifier.

        Returns:
            Number of deleted edge rows.
        """
        ...

    def delete_lsp_references_for_file(self, file_id: str) -> int:
        """Delete LSP ``references`` edges evidenced by or targeting symbols in ``file_id``.

        Args:
            file_id: Modified or deleted file identifier.

        Returns:
            Number of deleted LSP reference edge rows.
        """
        ...

    def delete_lsp_references_for_symbols(self, symbol_ids: Sequence[str]) -> int:
        """Delete LSP ``references`` edges originating from or targeting ``symbol_ids``.

        Args:
            symbol_ids: Sequence of symbol identifiers whose LSP reference edges should be removed.

        Returns:
            Number of deleted LSP reference edge rows.
        """
        ...

    def delete_dangling_lsp_references(self, repo_id: str) -> int:
        """Delete LSP ``references`` edges in ``repo_id`` whose endpoints no longer exist.

        Args:
            repo_id: Owning repository identifier.

        Returns:
            Number of deleted dangling LSP reference edges.
        """
        ...


@runtime_checkable
class ReferenceRefreshRepositoryProtocol(Protocol):
    """Persistence contract for per-symbol LSP ``ReferenceRefreshState`` tracking."""

    def upsert(self, state: ReferenceRefreshState) -> None:
        """Insert or update the LSP reference refresh state for a target symbol.

        Args:
            state: Refresh state record to persist.
        """
        ...

    def get(self, target_symbol_id: str) -> ReferenceRefreshState:
        """Fetch refresh state for ``target_symbol_id`` (returning default if unrecorded).

        Args:
            target_symbol_id: Target symbol identifier.

        Returns:
            Stored or default ``ReferenceRefreshState``.
        """
        ...

    def mark_symbols_stale(self, symbol_ids: Sequence[str]) -> list[str]:
        """Mark existing refresh records for ``symbol_ids`` as stale/unavailable.

        Args:
            symbol_ids: Candidate symbol identifiers impacted by edits.

        Returns:
            List of symbol IDs whose refresh records were invalidated.
        """
        ...

    def mark_file_symbols_stale(self, file_id: str) -> list[str]:
        """Mark refresh records for symbols declared in or impacted by ``file_id`` as stale.

        Args:
            file_id: Modified or deleted file identifier.

        Returns:
            List of invalidated target symbol IDs.
        """
        ...

    def invalidate_for_file(self, file_id: str) -> int:
        """Delete or invalidate refresh state entries owned by ``file_id``.

        Args:
            file_id: Deleted or re-extracted file identifier.

        Returns:
            Number of invalidated or removed refresh rows.
        """
        ...

    def collect_impacted_symbol_ids(self, file_id: str) -> list[str]:
        """Collect target symbol IDs declared in or referenced from ``file_id``.

        Args:
            file_id: File identifier being updated or removed.

        Returns:
            Deduplicated list of impacted target symbol IDs.
        """
        ...


@runtime_checkable
class IndexRunRepositoryProtocol(Protocol):
    """Persistence contract for ``IndexRunRecord`` lifecycle auditing."""

    def start_run(
        self,
        repo_id: str,
        run_type: str = "full",
        *,
        run_id: str | None = None,
        started_at: str | None = None,
    ) -> str:
        """Insert a ``running`` audit record and return its ``run_id``.

        Args:
            repo_id: Target repository identifier.
            run_type: Run category (e.g., ``"full"``, ``"incremental"``).
            run_id: Optional caller-supplied run identifier.
            started_at: Optional explicit ISO 8601 start timestamp.

        Returns:
            Persisted ``run_id`` string.
        """
        ...

    def finish_run(self, run_id: str, status: str, metrics: IndexRunMetrics) -> None:
        """Complete an existing index run with final status and extraction counters.

        Args:
            run_id: Identifier of the run created by ``start_run``.
            status: Terminal status string (e.g., ``"completed"``, ``"failed"``).
            metrics: Scanned/extracted counters, completion timestamp, and optional error.
        """
        ...

    def list_recent(
        self,
        repo_id: str | None = None,
        *,
        limit: int = 20,
    ) -> list[IndexRunRecord]:
        """List recent indexing runs ordered newest first.

        Args:
            repo_id: Optional repository scope filter.
            limit: Maximum number of records to return.

        Returns:
            Ordered list of ``IndexRunRecord`` entities.
        """
        ...


@runtime_checkable
class DiagnosticsRepositoryProtocol(Protocol):
    """Persistence contract for read-only graph statistics and DB status inspection."""

    def get_repo_graph_stats(self, repo_id: str) -> RepoGraphStats:
        """Aggregate file, line, symbol-kind, and edge-kind statistics for ``repo_id``.

        Args:
            repo_id: Target repository identifier.

        Returns:
            Populated ``RepoGraphStats`` summary.
        """
        ...

    def get_status_report(
        self,
        db_path: Path,
        repo_selector: str | None = None,
    ) -> DatabaseStatusReport:
        """Build a full database health, schema version, table count, and LSP report.

        Args:
            db_path: Path to the SQLite database file on disk.
            repo_selector: Optional repository ID or short name to scope metrics.

        Returns:
            Populated ``DatabaseStatusReport`` model.
        """
        ...

    def list_repo_summaries(self) -> list[RepoStorageSummary]:
        """List per-repository file, node, and edge counts across the database.

        Returns:
            Ordered list of ``RepoStorageSummary`` items.
        """
        ...

    def get_table_list_report(self, db_path: Path) -> TableListReport:
        """Inspect ``PRAGMA user_version`` and exact user-table row counts.

        Args:
            db_path: Path to the SQLite database file on disk.

        Returns:
            Populated ``TableListReport`` model.
        """
        ...


@runtime_checkable
class TextSearchRepositoryProtocol(Protocol):
    """Persistence contract for the whole-repository UTF-8 ``text_files`` + ``text_fts`` index."""

    def upsert_many(self, documents: Sequence[TextDocumentRecord]) -> None:
        """Insert or update a batch of ``TextDocumentRecord`` rows and sync ``text_fts``.

        Args:
            documents: UTF-8 repository text documents to persist.
        """
        ...

    def delete_by_paths(self, repo_id: str, file_paths: Sequence[str]) -> int:
        """Delete ``text_files`` rows matching ``file_paths`` under ``repo_id``.

        Args:
            repo_id: Owning repository identifier.
            file_paths: Repository-relative POSIX paths to remove.

        Returns:
            Number of rows deleted.
        """
        ...

    def prune_not_in_ids(self, repo_id: str, keep_ids: set[str]) -> int:
        """Delete ``text_files`` rows for ``repo_id`` absent from ``keep_ids``.

        Args:
            repo_id: Owning repository identifier.
            keep_ids: Set of active ``file:...`` document IDs to retain.

        Returns:
            Number of stale rows pruned.
        """
        ...

    def list_metadata_for_repo(self, repo_id: str) -> list[TextDocumentMeta]:
        """List lightweight ``TextDocumentMeta`` rows for ``repo_id`` without loading ``content``.

        Args:
            repo_id: Owning repository identifier.

        Returns:
            Sorted list of ``TextDocumentMeta`` records.
        """
        ...

    def query_candidates(self, query: TextCandidateQuery) -> list[TextDocumentRecord]:
        """Fetch candidate ``TextDocumentRecord`` items pruned by ``required_trigrams`` when set.

        Args:
            query: ``TextCandidateQuery`` specifying repo, optional trigram phrases, and filters.

        Returns:
            Ordered list of matching ``TextDocumentRecord`` rows.
        """
        ...

    def check_fts_integrity(self) -> bool:
        """Run SQLite FTS5 ``'integrity-check'`` on ``text_fts`` and return ``True`` when valid."""
        ...
