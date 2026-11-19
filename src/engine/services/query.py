"""Side-effect-free read query engine over repository graphs and diagnostics."""

from collections.abc import Sequence
from typing import Any

from context.models.outline import FileOutline
from context.models.source import SnippetOptions, SymbolSnippet
from context.models.symbol_context import ContextBuildOptions, SymbolContext
from context.services.builder import SymbolContextBuilder
from context.services.outline import OutlineBuilder
from context.services.source import SourceReader
from core.models.config import AppConfig
from core.models.diagnostics import DatabaseStatusReport, SqlQueryResult, TableListReport
from core.models.exceptions import DatabaseError, IndexerError, ValidationError
from core.models.file import (
    FilePageQuery,
    FileRecord,
    FileSymbolCountRow,
    TrackedFileSummary,
)
from core.models.node import SymbolPageQuery, SymbolSearchQuery, SymbolSummary
from core.ports.uow import UnitOfWorkFactoryProtocol, UnitOfWorkProtocol
from core.utils.logging import get_logger
from engine.models.queries import (
    ContextQuery,
    ListFilesQuery,
    ListSymbolsQuery,
    OutlineQuery,
    SearchSymbolsQuery,
    SourceQuery,
    SubgraphQuery,
    SymbolMembersQuery,
    SymbolReferencesQuery,
    SymbolSnippetsQuery,
)
from engine.models.responses import (
    FileListPage,
    IndexRunListReport,
    RepoOverview,
    RepoSummaryListReport,
    SymbolListPage,
    SymbolSearchPage,
)
from engine.services.command import CommandEngine
from engine.services.resolver import SymbolTargetResolver
from engine.utils.formatting import (
    aggregate_top_directories,
    format_cli_freshness_label,
    matches_file_pattern,
    normalize_path_prefix,
    normalize_repo_file_filter,
    relativize_if_under_root,
)
from graph.models.references import ReferenceQueryOptions, SymbolReferencesReport
from graph.models.risk import RiskAssessment
from graph.models.subgraph import SubgraphOptions, SubgraphSlice
from graph.services.navigator import GraphNavigator
from graph.services.references import ReferenceAnalyzer
from graph.services.risk import RiskAnalyzer

logger = get_logger("engine.services.query")

_ALLOWED_COLLABORATOR_KEYS: frozenset[str] = frozenset({
    "navigator",
    "reference_analyzer",
    "risk_analyzer",
    "source_reader",
    "outline_builder",
    "context_builder",
})


class QueryEngine:
    """Executes read-only graph, context, snippet, risk, and diagnostic queries."""

    def __init__(
        self,
        config: AppConfig,
        uow_factory: UnitOfWorkFactoryProtocol,
        *,
        resolver: SymbolTargetResolver | None = None,
        command_engine: CommandEngine | None = None,
        **collaborators: Any,
    ) -> None:
        """Initialize the query engine with read-side domain collaborators.

        Args:
            config: Immutable runtime ``AppConfig``.
            uow_factory: ``UnitOfWorkFactoryProtocol`` bound to the target database.
            resolver: Optional ``SymbolTargetResolver`` collaborator.
            command_engine: Optional ``CommandEngine`` invoked solely when
                ``SymbolReferencesQuery.refresh_lsp`` is ``True``.
            **collaborators: Optional read-domain service overrides (``navigator``,
                ``reference_analyzer``, ``risk_analyzer``, ``source_reader``,
                ``outline_builder``, ``context_builder``).

        Raises:
            TypeError: If unexpected keyword arguments are supplied in ``collaborators``.
        """
        unknown = sorted(set(collaborators) - _ALLOWED_COLLABORATOR_KEYS)
        if unknown:
            raise TypeError(
                f"Unexpected keyword argument(s) for QueryEngine: {', '.join(unknown)}"
            )

        self._config = config
        self._uow_factory = uow_factory
        self._resolver = resolver if resolver is not None else SymbolTargetResolver()
        self._command_engine = command_engine
        self._bind_domain_services(config, collaborators)

    def _bind_domain_services(
        self,
        config: AppConfig,
        collaborators: dict[str, Any],
    ) -> None:
        """Wire injected or default read-domain services onto ``self``.

        Args:
            config: Active ``AppConfig``.
            collaborators: Validated dictionary of optional collaborator overrides.
        """
        nav: GraphNavigator | None = collaborators.get("navigator")
        self._navigator = nav if nav is not None else GraphNavigator(config)

        ref_analyzer: ReferenceAnalyzer | None = collaborators.get("reference_analyzer")
        self._reference_analyzer = (
            ref_analyzer if ref_analyzer is not None else ReferenceAnalyzer(config)
        )

        risk_analyzer: RiskAnalyzer | None = collaborators.get("risk_analyzer")
        self._risk_analyzer = risk_analyzer if risk_analyzer is not None else RiskAnalyzer(config)

        reader: SourceReader | None = collaborators.get("source_reader")
        self._source_reader = reader if reader is not None else SourceReader(config)

        outline: OutlineBuilder | None = collaborators.get("outline_builder")
        self._outline_builder = (
            outline
            if outline is not None
            else OutlineBuilder(config, navigator=self._navigator)
        )

        ctx_builder: SymbolContextBuilder | None = collaborators.get("context_builder")
        self._context_builder = (
            ctx_builder
            if ctx_builder is not None
            else SymbolContextBuilder(
                config,
                navigator=self._navigator,
                source_reader=self._source_reader,
                reference_analyzer=self._reference_analyzer,
            )
        )

    @staticmethod
    def _record_to_file_summary(
        record: FileRecord,
        *,
        symbol_count: int = 0,
        is_fresh: bool = True,
        stale_reason: str | None = None,
    ) -> TrackedFileSummary:
        """Convert a ``FileRecord`` and optional counts/freshness into ``TrackedFileSummary``.

        Args:
            record: Source ``FileRecord`` entity.
            symbol_count: Extracted symbol count for the file.
            is_fresh: Live disk freshness status boolean.
            stale_reason: Optional machine-readable staleness reason.

        Returns:
            Populated ``TrackedFileSummary`` model.
        """
        return TrackedFileSummary(
            id=record.id,
            file_path=record.file_path,
            module_path=record.module_path,
            language=record.language,
            line_count=record.line_count,
            symbol_count=symbol_count,
            size_bytes=record.size_bytes,
            is_test=record.is_test,
            content_hash=record.content_hash,
            last_indexed_at=record.last_indexed_at,
            freshness=format_cli_freshness_label(is_fresh, stale_reason),
            is_fresh=is_fresh,
            stale_reason=stale_reason,
        )

    @staticmethod
    def _filter_symbol_count_rows(
        rows: Sequence[FileSymbolCountRow],
        clean_prefix: str | None,
        pattern: str | None,
    ) -> list[FileSymbolCountRow]:
        """Filter ``FileSymbolCountRow`` items by normalized ``path_prefix`` and ``pattern``.

        Args:
            rows: Candidate file-and-symbol-count rows for a repository.
            clean_prefix: Optional normalized POSIX directory/file prefix.
            pattern: Optional glob or substring pattern.

        Returns:
            Filtered list of ``FileSymbolCountRow`` items preserving order.
        """
        has_prefix = bool(clean_prefix and clean_prefix != ".")
        prefix_slash = f"{clean_prefix}/" if has_prefix else ""
        matched: list[FileSymbolCountRow] = []
        for row in rows:
            fpath = row.file.file_path
            if has_prefix and fpath != clean_prefix and not fpath.startswith(prefix_slash):
                continue
            if matches_file_pattern(fpath, row.file.module_path, pattern):
                matched.append(row)
        return matched

    def _list_files_enriched(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        query: ListFilesQuery,
        clean_prefix: str | None,
    ) -> FileListPage:
        """List files with symbol counts, pattern matching, and optional live disk checks.

        Args:
            uow: Active read Unit of Work.
            repo_id: Resolved repository identifier.
            query: File list query parameters.
            clean_prefix: Optional normalized path prefix.

        Returns:
            Paginated ``FileListPage`` with ``symbol_count`` and freshness metadata.
        """
        all_rows = uow.files.list_with_symbol_counts(repo_id, include_tests=query.include_tests)
        matched = self._filter_symbol_count_rows(all_rows, clean_prefix, query.pattern)
        total_count = len(matched)
        sliced = matched[query.offset : query.offset + query.limit]

        repo_record = uow.repos.get_by_id(repo_id) if query.check_freshness else None
        root_path = repo_record.root_path if repo_record is not None else None

        entries: list[TrackedFileSummary] = []
        for row in sliced:
            if query.check_freshness:
                check = self._navigator.check_file_freshness(
                    uow,
                    row.file.id,
                    file_record=row.file,
                    repo_root=root_path,
                )
                is_fresh = check.is_fresh
                stale_reason = check.stale_reason
            else:
                is_fresh = True
                stale_reason = None
            entries.append(
                self._record_to_file_summary(
                    row.file,
                    symbol_count=row.symbol_count,
                    is_fresh=is_fresh,
                    stale_reason=stale_reason,
                )
            )

        return FileListPage(
            repo_id=repo_id,
            total_count=total_count,
            returned_count=len(entries),
            offset=query.offset,
            limit=query.limit,
            has_more=(query.offset + len(entries)) < total_count,
            files=entries,
        )

    def get_repo_overview(self, repo: str | None = None) -> RepoOverview:
        """Retrieve high-level overview, graph statistics, top directories, and recent runs.

        Args:
            repo: Optional repository ID, short name, or root path.

        Returns:
            Populated ``RepoOverview`` model.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository resolution or statistics queries fail.
        """
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            repo_id = uow.repos.resolve_id(repo)
            repo_record = uow.repos.get_by_id(repo_id)
            if repo_record is None:
                raise DatabaseError(
                    f"Repository not found: {repo_id}",
                    error_code="repo_not_found",
                    context={"repo_id": repo_id},
                )
            stats = uow.diagnostics.get_repo_graph_stats(repo_id)
            non_test_files = uow.files.list_for_repo(repo_id, include_tests=False)
            top_dirs = aggregate_top_directories(non_test_files, limit=15)
            recent_runs = uow.runs.list_recent(repo_id, limit=3)
            return RepoOverview(
                repo=repo_record,
                stats=stats,
                top_directories=top_dirs,
                recent_runs=recent_runs,
            )

    def list_files(self, query: ListFilesQuery) -> FileListPage:
        """List tracked repository files with pagination, filtering, and optional freshness.

        Args:
            query: ``ListFilesQuery`` specifying ``repo``, ``path_prefix``, ``pattern``,
                ``include_tests``, ``check_freshness``, ``limit``, and ``offset``.

        Returns:
            Paginated ``FileListPage`` response.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository resolution or file queries fail.
        """
        clean_prefix = (
            normalize_path_prefix(query.path_prefix)
            if query.path_prefix is not None
            else None
        )
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            repo_id = uow.repos.resolve_id(query.repo)
            if query.pattern is not None or query.check_freshness:
                return self._list_files_enriched(uow, repo_id, query, clean_prefix)

            page = uow.files.list_page(
                FilePageQuery(
                    repo_id=repo_id,
                    path_prefix=clean_prefix,
                    include_tests=query.include_tests,
                    limit=query.limit,
                    offset=query.offset,
                )
            )
            items = [self._record_to_file_summary(rec) for rec in page.items]
            return FileListPage(
                repo_id=repo_id,
                total_count=page.total_count,
                returned_count=len(items),
                offset=query.offset,
                limit=query.limit,
                has_more=(query.offset + len(items)) < page.total_count,
                files=items,
            )

    def get_file_outline(self, query: OutlineQuery) -> FileOutline:
        """Build a hierarchical symbol outline for an indexed file.

        Relativizes ``query.file_path`` when supplied as an absolute path inside
        the resolved repository root.

        Args:
            query: ``OutlineQuery`` specifying ``file_path``, ``repo``, ``include_private``,
                and ``max_depth``.

        Returns:
            Populated ``FileOutline`` model.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository resolution fails.
            IndexerError: With ``error_code="file_not_found"`` if ``file_path`` is unindexed.
        """
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            repo_id = uow.repos.resolve_id(query.repo)
            repo_record = uow.repos.get_by_id(repo_id)
            rel_path = relativize_if_under_root(
                query.file_path,
                repo_record.root_path if repo_record is not None else None,
            )
            return self._outline_builder.build_outline(
                uow,
                repo_id,
                rel_path,
                include_private=query.include_private,
                max_depth=query.max_depth,
            )

    def get_symbol(self, symbol: str, *, repo: str | None = None) -> SymbolSummary:
        """Resolve and return a single ``SymbolSummary`` projection.

        Args:
            symbol: Symbol ID (``sym:...``), ``file:symbol``, qualified name, or short name.
            repo: Optional repository ID, short name, or root path.

        Returns:
            Uniquely resolved ``SymbolSummary`` model.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository resolution fails.
            SymbolNotFoundError: If ``symbol`` is empty, missing, or ambiguous.
        """
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            return self._resolver.resolve_one(uow, symbol, repo_selector=repo)

    def search_symbols(self, query: SearchSymbolsQuery) -> SymbolSearchPage:
        """Search symbols in a repository via FTS5 with ranked ``LIKE`` fallback.

        Args:
            query: ``SearchSymbolsQuery`` specifying ``query``, ``repo``, ``kind``,
                ``file_path``, and ``limit``.

        Returns:
            Populated ``SymbolSearchPage`` model.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository resolution or symbol search fails.
        """
        clean_file = (
            normalize_repo_file_filter(query.file_path)
            if query.file_path is not None
            else None
        )
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            repo_id = uow.repos.resolve_id(query.repo)
            items = uow.nodes.search_fts(
                SymbolSearchQuery(
                    repo_id=repo_id,
                    query=query.query,
                    kind=query.kind,
                    file_path=clean_file,
                    limit=query.limit,
                )
            )
            return SymbolSearchPage(
                repo_id=repo_id,
                query=query.query,
                count=len(items),
                symbols=items,
            )

    def list_symbols(self, query: ListSymbolsQuery) -> SymbolListPage:
        """List repository symbols with optional ``kind``/``file_path`` filters and pagination.

        Args:
            query: ``ListSymbolsQuery`` specifying ``repo``, ``kind``, ``file_path``,
                ``limit``, and ``offset``.

        Returns:
            Paginated ``SymbolListPage`` model.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository resolution or symbol listing fails.
        """
        clean_file = (
            normalize_repo_file_filter(query.file_path)
            if query.file_path is not None
            else None
        )
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            repo_id = uow.repos.resolve_id(query.repo)
            page = uow.nodes.list_paginated_summaries(
                SymbolPageQuery(
                    repo_id=repo_id,
                    kind=query.kind,
                    file_path=clean_file,
                    limit=query.limit,
                    offset=query.offset,
                )
            )
            return SymbolListPage(
                repo_id=repo_id,
                total_count=page.total_count,
                returned_count=len(page.items),
                offset=query.offset,
                limit=query.limit,
                has_more=(query.offset + len(page.items)) < page.total_count,
                symbols=page.items,
            )

    def get_symbol_members(self, query: SymbolMembersQuery) -> list[SymbolSummary]:
        """List direct structural member symbols of a class or module symbol.

        Args:
            query: ``SymbolMembersQuery`` specifying ``symbol``, ``repo``, and
                ``include_private``.

        Returns:
            Ordered list of member ``SymbolSummary`` projections.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository resolution fails.
            SymbolNotFoundError: If ``query.symbol`` cannot be uniquely resolved.
        """
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            target = self._resolver.resolve_one(uow, query.symbol, repo_selector=query.repo)
            return self._navigator.get_members(
                uow,
                target.id,
                include_private=query.include_private,
            )

    def get_symbol_source(self, query: SourceQuery) -> SymbolSnippet:
        """Read a confined, hash-verified ``SymbolSnippet`` for a single symbol.

        Args:
            query: ``SourceQuery`` specifying ``symbol``, ``repo``, ``max_lines``,
                ``offset``, and ``padding_lines``.

        Returns:
            Populated ``SymbolSnippet`` carrying both ``raw_source`` and ``numbered_source``.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository or file metadata is missing.
            SymbolNotFoundError: If ``query.symbol`` cannot be uniquely resolved.
            PathSecurityError: If the source file violates repository confinement.
            ParseError: If the source file cannot be read or exceeds size bounds.
        """
        opts = SnippetOptions(
            max_lines=query.max_lines,
            offset=query.offset,
            padding_lines=query.padding_lines,
        )
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            target = self._resolver.resolve_one(uow, query.symbol, repo_selector=query.repo)
            return self._source_reader.read_symbol_snippet(uow, target.id, opts)

    def get_symbol_snippets(self, query: SymbolSnippetsQuery) -> list[SymbolSnippet]:
        """Batch-read ``SymbolSnippet`` slices reading each unique file at most once.

        Args:
            query: ``SymbolSnippetsQuery`` specifying ``symbols``, ``repo``, ``max_lines``,
                ``offset``, and ``padding_lines``.

        Returns:
            Ordered deduplicated list of ``SymbolSnippet`` models.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            ValidationError: If ``query.symbols`` is empty.
            SymbolNotFoundError: If any requested symbol cannot be uniquely resolved.
            PathSecurityError: If any backing file violates repository confinement.
            ParseError: If any backing file cannot be read or exceeds size bounds.
        """
        opts = SnippetOptions(
            max_lines=query.max_lines,
            offset=query.offset,
            padding_lines=query.padding_lines,
        )
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            targets = self._resolver.resolve_many(uow, query.symbols, repo_selector=query.repo)
            return self._source_reader.read_symbol_snippets(
                uow,
                [target.id for target in targets],
                opts,
            )

    def get_symbol_context(self, query: ContextQuery) -> SymbolContext:
        """Assemble bounded hierarchical, edge, freshness, and confidence symbol context.

        Args:
            query: ``ContextQuery`` specifying ``symbol``, ``repo``, ``include_source``,
                ``max_source_lines``, ``edge_limit``, ``child_limit``, and ``sibling_limit``.

        Returns:
            Populated immutable ``SymbolContext`` model.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository resolution fails.
            SymbolNotFoundError: If ``query.symbol`` cannot be uniquely resolved.
        """
        opts = ContextBuildOptions(
            include_source=query.include_source,
            max_source_lines=query.max_source_lines,
            child_limit=query.child_limit,
            edge_limit=query.edge_limit,
            sibling_limit=query.sibling_limit,
        )
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            target = self._resolver.resolve_one(uow, query.symbol, repo_selector=query.repo)
            return self._context_builder.build_context(uow, target.id, opts)

    def get_symbol_subgraph(self, query: SubgraphQuery) -> SubgraphSlice:
        """Extract a bounded BFS neighborhood ``SubgraphSlice`` around ``query.symbols``.

        Args:
            query: ``SubgraphQuery`` specifying ``symbols``, ``repo``, ``depth``,
                ``max_nodes``, and optional ``kinds``.

        Returns:
            ``SubgraphSlice`` with deduplicated ``SymbolSummary`` nodes and closed ``Edge``s.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            ValidationError: If ``query.symbols`` is empty.
            SymbolNotFoundError: If any root symbol cannot be uniquely resolved.
        """
        opts = SubgraphOptions(
            depth=query.depth,
            max_nodes=query.max_nodes,
            kinds=query.kinds,
        )
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            targets = self._resolver.resolve_many(uow, query.symbols, repo_selector=query.repo)
            return self._navigator.build_subgraph(
                uow,
                [target.id for target in targets],
                opts,
            )

    def get_symbol_references(self, query: SymbolReferencesQuery) -> SymbolReferencesReport:
        """Inspect paginated symbol references, optionally refreshing LSP references first.

        Args:
            query: ``SymbolReferencesQuery`` specifying ``symbol``, ``repo``, ``direction``,
                ``refresh_lsp``, ``limit``, ``offset``, and ``include_previews``.

        Returns:
            Populated ``SymbolReferencesReport`` with reference stats and site previews.

        Raises:
            IndexerError: With ``error_code="references_unavailable"`` when ``refresh_lsp``
                is ``True`` without an attached ``CommandEngine``.
            DatabaseNotFoundError: If the database file does not exist.
            SymbolNotFoundError: If ``query.symbol`` cannot be uniquely resolved.
            LspError: If ``refresh_lsp=True`` and LSP reference enrichment fails.
        """
        refreshed_id: str | None = None
        if query.refresh_lsp:
            if self._command_engine is None:
                raise IndexerError(
                    "LSP reference refresh requires an attached CommandEngine.",
                    error_code="references_unavailable",
                    context={"symbol": query.symbol},
                )
            enrichment = self._command_engine.refresh_symbol_references(
                query.symbol,
                repo=query.repo,
            )
            refreshed_id = enrichment.target_symbol_id

        opts = ReferenceQueryOptions(
            direction=query.direction,
            limit=query.limit,
            offset=query.offset,
            include_previews=query.include_previews,
        )
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            target_id = (
                refreshed_id
                if refreshed_id is not None
                else self._resolver.resolve_one(uow, query.symbol, repo_selector=query.repo).id
            )
            return self._reference_analyzer.inspect_references(uow, target_id, opts)

    def analyze_risk(
        self,
        symbols: Sequence[str],
        *,
        repo: str | None = None,
    ) -> RiskAssessment:
        """Analyze deterministic change-impact risk across one or more symbols.

        Args:
            symbols: Non-empty sequence of symbol IDs, ``file:symbol``, qualified names,
                or unique short names.
            repo: Optional repository ID, short name, or root path.

        Returns:
            Populated ``RiskAssessment`` report.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            ValidationError: If ``symbols`` is empty.
            SymbolNotFoundError: If any symbol cannot be uniquely resolved.
        """
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            targets = self._resolver.resolve_many(uow, symbols, repo_selector=repo)
            return self._risk_analyzer.analyze_targets(
                uow,
                [target.id for target in targets],
            )

    def get_db_status(self, repo: str | None = None) -> DatabaseStatusReport:
        """Inspect database file size, schema version, table counts, and LSP coverage.

        Opens the database without running migrations (``initialize=False``) so
        unupgraded databases still report ``schema_ok=False`` cleanly.

        Args:
            repo: Optional repository ID, short name, or root path to scope metrics.

        Returns:
            Populated ``DatabaseStatusReport`` model.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If reading database status fails.
        """
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=False) as uow:
            return uow.diagnostics.get_status_report(
                self._uow_factory.db_path,
                repo_selector=repo,
            )

    def list_repos(self) -> RepoSummaryListReport:
        """List all indexed repositories and their file, node, and edge counts.

        Returns:
            Populated ``RepoSummaryListReport`` model.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If schema verification or querying fails.
        """
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            repos = uow.diagnostics.list_repo_summaries()
            return RepoSummaryListReport(
                db_path=str(self._uow_factory.db_path),
                repos=repos,
            )

    def list_tables(self) -> TableListReport:
        """List user SQLite tables, ``PRAGMA user_version``, and exact row counts.

        Returns:
            Populated ``TableListReport`` model.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If table inspection fails.
        """
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=False) as uow:
            return uow.diagnostics.get_table_list_report(self._uow_factory.db_path)

    def list_index_runs(
        self,
        repo: str | None = None,
        *,
        limit: int = 20,
    ) -> IndexRunListReport:
        """List recent indexing runs ordered newest first.

        Args:
            repo: Optional repository ID, short name, or root path filter.
            limit: Maximum number of run records to return (must be ``>= 1``).

        Returns:
            Populated ``IndexRunListReport`` model.

        Raises:
            ValidationError: If ``limit < 1``.
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If repository resolution or run querying fails.
        """
        if limit < 1:
            raise ValidationError("limit must be >= 1", context={"limit": limit})
        has_repo = bool(repo is not None and repo.strip())
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=True) as uow:
            repo_id = uow.repos.resolve_id(repo) if has_repo else None
            runs = uow.runs.list_recent(repo_id=repo_id, limit=limit)
            return IndexRunListReport(
                db_path=str(self._uow_factory.db_path),
                repo_id=repo_id,
                runs=runs,
            )

    def execute_readonly_sql(self, query: str, *, limit: int = 100) -> SqlQueryResult:
        """Execute a SQLite authorizer-guarded ``SELECT``/``PRAGMA`` read-only SQL query.

        Args:
            query: Raw read-only SQL statement.
            limit: Maximum number of rows to return (must be ``>= 1``).

        Returns:
            Populated ``SqlQueryResult`` model.

        Raises:
            ValidationError: If ``limit < 1`` or ``query`` is blank.
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If SQLite denies or fails executing ``query``.
        """
        if limit < 1:
            raise ValidationError("limit must be >= 1", context={"limit": limit})
        cleaned = query.strip()
        if not cleaned:
            raise ValidationError("SQL query must not be empty.")
        return self._uow_factory.execute_readonly_sql(cleaned, limit=limit)

    def resolve_active_repo(self, repo: str | None = None) -> str | None:
        """Resolve the active ``repo_id`` for an interactive session when unambiguous.

        Args:
            repo: Optional explicit repository ID, short name, or root path.

        Returns:
            Canonical ``repo_id`` when ``repo`` is given or exactly one repository is
            indexed; ``None`` when zero or multiple repositories exist without ``repo``.

        Raises:
            DatabaseNotFoundError: If the database file does not exist.
            DatabaseError: If an explicit ``repo`` cannot be resolved.
        """
        with self._uow_factory.open(read_only=True, must_exist=True, initialize=False) as uow:
            if repo is not None and repo.strip():
                return uow.repos.resolve_id(repo)
            all_repos = uow.repos.list_all()
            if len(all_repos) == 1:
                return all_repos[0].id
            return None


__all__ = [
    "QueryEngine",
]
