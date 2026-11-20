"""SQLAlchemy Core adapter implementing ``DiagnosticsRepositoryProtocol``."""

from pathlib import Path

from sqlalchemy import MetaData, Table, case, func, inspect, select
from sqlalchemy.engine import Connection

from core.models.diagnostics import (
    DatabaseStatusReport,
    LspCoverageMetrics,
    RepoGraphStats,
    RepoStorageSummary,
    SelectedRepoSummary,
    TableCountEntry,
    TableListReport,
)
from core.models.enums import CALLABLE_KINDS, LOCAL_CALLABLE_KINDS, EdgeKind, SymbolKind
from core.models.exceptions import DatabaseError
from core.models.repo import RepoRecord
from core.utils.logging import get_logger
from storage.models.options import SchemaStatusMetrics
from storage.services.migrator import SchemaMigrator
from storage.services.repos import SqlRepoRepository
from storage.services.runs import SqlIndexRunRepository
from storage.utils.schema import (
    SCHEMA_VERSION,
    edges_table,
    files_table,
    nodes_table,
    reference_refresh_table,
)
from storage.utils.sql_helpers import db_operation

logger = get_logger("storage.services.diagnostics")

_REQUIRED_STATUS_TABLES: frozenset[str] = frozenset({
    "repos",
    "nodes",
    "edges",
    "index_runs",
    "reference_refresh",
    "text_files",
})


class SqlDiagnosticsRepository:
    """SQLite repository adapter for graph statistics and database health inspection."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``."""
        self._conn = conn
        self._migrator = SchemaMigrator()
        self._repos = SqlRepoRepository(conn)
        self._runs = SqlIndexRunRepository(conn)

    def get_repo_graph_stats(self, repo_id: str) -> RepoGraphStats:
        """Aggregate file, line, symbol-kind, and edge-kind statistics for ``repo_id``."""
        with db_operation(
            f"Failed to query graph statistics for {repo_id}",
            context={"repo_id": repo_id},
        ):
            kind_rows = (
                self._conn
                .execute(
                    select(nodes_table.c.kind, func.count().label("count"))
                    .where(nodes_table.c.repo_id == repo_id)
                    .group_by(nodes_table.c.kind)
                )
                .mappings()
                .all()
            )
            edge_rows = (
                self._conn
                .execute(
                    select(edges_table.c.kind, func.count().label("count"))
                    .where(edges_table.c.repo_id == repo_id)
                    .group_by(edges_table.c.kind)
                )
                .mappings()
                .all()
            )
            file_stats = (
                self._conn
                .execute(
                    select(
                        func.count().label("file_count"),
                        func.coalesce(
                            func.sum(case((files_table.c.is_test.is_(True), 1), else_=0)),
                            0,
                        ).label("test_file_count"),
                        func.coalesce(func.sum(files_table.c.line_count), 0).label("total_lines"),
                    )
                    .select_from(files_table)
                    .where(files_table.c.repo_id == repo_id)
                )
                .mappings()
                .first()
            )

        kind_counts = {str(r["kind"]): int(r["count"]) for r in kind_rows}
        edge_kind_counts = {str(r["kind"]): int(r["count"]) for r in edge_rows}
        callable_count = sum(kind_counts.get(str(k), 0) for k in CALLABLE_KINDS)
        local_callable_count = sum(kind_counts.get(str(k), 0) for k in LOCAL_CALLABLE_KINDS)

        return RepoGraphStats(
            repo_id=repo_id,
            file_count=int(file_stats["file_count"]) if file_stats else 0,
            test_file_count=int(file_stats["test_file_count"]) if file_stats else 0,
            total_lines=int(file_stats["total_lines"]) if file_stats else 0,
            node_count=sum(kind_counts.values()),
            edge_count=sum(edge_kind_counts.values()),
            module_count=kind_counts.get(str(SymbolKind.MODULE), 0),
            class_count=kind_counts.get(str(SymbolKind.CLASS), 0),
            callable_count=callable_count,
            local_callable_count=local_callable_count,
            kind_counts=kind_counts,
            edge_kind_counts=edge_kind_counts,
        )

    @staticmethod
    def _read_db_size_bytes(db_path: Path) -> int:
        """Read the on-disk size of ``db_path`` in bytes (``0`` for ``:memory:``)."""
        if str(db_path) == ":memory:":
            return 0
        try:
            return db_path.stat().st_size
        except OSError as exc:
            logger.error("Failed reading database file size for %s: %s", db_path, exc)
            raise DatabaseError(
                f"Failed to read database file size for {db_path}: {exc}",
                context={"db_path": str(db_path)},
            ) from exc

    def _list_table_counts(self) -> list[TableCountEntry]:
        """Return exact row counts for all user tables and the root FTS virtual table."""
        with db_operation("Failed to read table row counts"):
            table_names = [
                name
                for name in sorted(inspect(self._conn).get_table_names())
                if not name.startswith(("nodes_fts_", "text_fts_"))
            ]
            temp_meta = MetaData()
            entries: list[TableCountEntry] = []
            for name in table_names:
                tbl = Table(name, temp_meta)
                row_count = int(
                    self._conn.execute(select(func.count()).select_from(tbl)).scalar_one()
                )
                entries.append(TableCountEntry(table=name, row_count=row_count))
            return entries

    @staticmethod
    def _has_compatible_schema(user_version: int, tables: list[TableCountEntry]) -> bool:
        """Return ``True`` when ``user_version == SCHEMA_VERSION`` and core tables exist."""
        table_names = {entry.table for entry in tables}
        missing = sorted(_REQUIRED_STATUS_TABLES - table_names)
        if user_version != SCHEMA_VERSION or missing:
            logger.warning(
                "Database schema not ready for full status inspection "
                "(user_version=%d, expected=%d, missing_tables=%s)",
                user_version,
                SCHEMA_VERSION,
                missing,
            )
            return False
        return True

    def _resolve_status_repo(
        self,
        repo_selector: str | None,
    ) -> tuple[list[RepoRecord], RepoRecord | None]:
        """Load all indexed repositories and resolve the selected repository if unambiguous."""
        all_repos = self._repos.list_all()
        if repo_selector:
            resolved_id = self._repos.resolve_id(repo_selector)
            return all_repos, self._repos.get_by_id(resolved_id)
        if len(all_repos) == 1:
            return all_repos, all_repos[0]
        return all_repos, None

    def _fetch_edge_counts_by_kind(self, repo_id: str | None) -> dict[str, int]:
        """Query edge counts grouped by ``kind``, optionally scoped to ``repo_id``."""
        cnt_col = func.count().label("cnt")
        stmt = select(edges_table.c.kind, cnt_col)
        if repo_id is not None:
            stmt = stmt.where(edges_table.c.repo_id == repo_id)
        stmt = stmt.group_by(edges_table.c.kind).order_by(cnt_col.desc(), edges_table.c.kind.asc())
        rows = self._conn.execute(stmt).mappings().all()
        return {str(r["kind"]): int(r["cnt"]) for r in rows}

    def _fetch_lsp_coverage(self, repo_id: str | None) -> LspCoverageMetrics:
        """Compute non-module symbol LSP refresh coverage, optionally scoped to ``repo_id``."""
        sym_stmt = (
            select(func.count())
            .select_from(nodes_table)
            .where(nodes_table.c.kind != str(SymbolKind.MODULE))
        )
        edge_stmt = (
            select(func.count())
            .select_from(edges_table)
            .where(edges_table.c.kind == str(EdgeKind.REFERENCES))
        )
        if repo_id is not None:
            sym_stmt = sym_stmt.where(nodes_table.c.repo_id == repo_id)
            edge_stmt = edge_stmt.where(edges_table.c.repo_id == repo_id)
            ref_stmt = (
                select(func.count())
                .select_from(
                    reference_refresh_table.join(
                        nodes_table,
                        nodes_table.c.id == reference_refresh_table.c.target_symbol_id,
                    )
                )
                .where(
                    nodes_table.c.repo_id == repo_id,
                    reference_refresh_table.c.available.is_(True),
                )
            )
        else:
            ref_stmt = (
                select(func.count())
                .select_from(reference_refresh_table)
                .where(reference_refresh_table.c.available.is_(True))
            )

        total_syms = int(self._conn.execute(sym_stmt).scalar_one())
        refreshed_syms = int(self._conn.execute(ref_stmt).scalar_one())
        lsp_edges = int(self._conn.execute(edge_stmt).scalar_one())
        pct = round((refreshed_syms / total_syms) * 100.0, 1) if total_syms > 0 else 0.0
        return LspCoverageMetrics(
            non_module_symbols=total_syms,
            refreshed_symbols=refreshed_syms,
            coverage_pct=pct,
            lsp_reference_edges=lsp_edges,
        )

    def _collect_schema_metrics(self, repo_selector: str | None) -> SchemaStatusMetrics:
        """Collect v2 repository, edge, LSP coverage, and latest-run status metrics."""
        with db_operation(
            "Failed to collect database status metrics",
            context={"repo_selector": repo_selector},
        ):
            all_repos, selected_repo = self._resolve_status_repo(repo_selector)
            repo_id = selected_repo.id if selected_repo is not None else None
            recent_runs = self._runs.list_recent(repo_id, limit=1)
            return SchemaStatusMetrics(
                all_repos=all_repos,
                selected_repo=selected_repo,
                edge_counts_by_kind=self._fetch_edge_counts_by_kind(repo_id),
                lsp_coverage=self._fetch_lsp_coverage(repo_id),
                latest_run=recent_runs[0] if recent_runs else None,
            )

    def get_status_report(
        self,
        db_path: Path,
        repo_selector: str | None = None,
    ) -> DatabaseStatusReport:
        """Build a full database health, schema version, table count, and LSP report."""
        size_bytes = self._read_db_size_bytes(db_path)
        user_version = self._migrator.read_user_version(self._conn)
        tables = self._list_table_counts()
        metrics = (
            self._collect_schema_metrics(repo_selector)
            if self._has_compatible_schema(user_version, tables)
            else SchemaStatusMetrics()
        )
        selected_summary = (
            SelectedRepoSummary(
                id=metrics.selected_repo.id,
                name=metrics.selected_repo.name,
                root_path=metrics.selected_repo.root_path,
                last_indexed_at=metrics.selected_repo.last_indexed_at,
            )
            if metrics.selected_repo is not None
            else None
        )
        return DatabaseStatusReport(
            db_path=str(db_path),
            size_bytes=size_bytes,
            user_version=user_version,
            expected_schema_version=SCHEMA_VERSION,
            schema_ok=(user_version == SCHEMA_VERSION),
            repo_count=len(metrics.all_repos),
            selected_repo=selected_summary,
            tables=tables,
            edge_counts_by_kind=metrics.edge_counts_by_kind,
            lsp_coverage=metrics.lsp_coverage,
            latest_run=metrics.latest_run,
        )

    def _count_by_repo(self, table: Table) -> dict[str, int]:
        """Count rows in ``table`` grouped by ``repo_id``."""
        rows = (
            self._conn
            .execute(select(table.c.repo_id, func.count().label("cnt")).group_by(table.c.repo_id))
            .mappings()
            .all()
        )
        return {str(r["repo_id"]): int(r["cnt"]) for r in rows}

    def list_repo_summaries(self) -> list[RepoStorageSummary]:
        """List per-repository file, node, and edge counts across the database."""
        self._migrator.initialize(self._conn)
        with db_operation("Failed to query repository summaries"):
            repos = self._repos.list_all()
            file_counts = self._count_by_repo(files_table)
            node_counts = self._count_by_repo(nodes_table)
            edge_counts = self._count_by_repo(edges_table)

        return [
            RepoStorageSummary(
                id=repo.id,
                name=repo.name,
                root_path=repo.root_path,
                language=str(repo.default_language),
                files=file_counts.get(repo.id, 0),
                nodes=node_counts.get(repo.id, 0),
                edges=edge_counts.get(repo.id, 0),
                created_at=repo.created_at,
                last_indexed_at=repo.last_indexed_at,
            )
            for repo in repos
        ]

    def get_table_list_report(self, db_path: Path) -> TableListReport:
        """Inspect ``PRAGMA user_version`` and exact user-table row counts."""
        user_version = self._migrator.read_user_version(self._conn)
        tables = self._list_table_counts()
        return TableListReport(
            db_path=str(db_path),
            user_version=user_version,
            tables=tables,
        )
