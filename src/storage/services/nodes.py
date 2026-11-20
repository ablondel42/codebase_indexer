"""SQLAlchemy Core adapter implementing ``NodeRepositoryProtocol``."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, case, func, literal_column, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from core.models.enums import PREFIX_SYMBOL, SymbolKind
from core.models.node import (
    SymbolNode,
    SymbolPageQuery,
    SymbolPageResult,
    SymbolSearchQuery,
    SymbolSummary,
)
from core.utils.logging import get_logger
from core.utils.pathing import normalize_rel_path_prefix
from storage.utils.mappers import node_to_row, row_to_node, row_to_symbol_summary
from storage.utils.schema import files_table, nodes_fts_table, nodes_table
from storage.utils.sql_helpers import (
    build_fts_match_expression,
    build_rel_path_prefix_clause,
    build_sqlite_upsert,
    chunked_sequence,
    db_operation,
    escape_like,
)

logger = get_logger("storage.services.nodes")

_BATCH_SIZE = 400


class SqlNodeRepository:
    """SQLite repository adapter for ``SymbolNode`` entities and ``SymbolSummary`` views."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``."""
        self._conn = conn

    @staticmethod
    def _summary_select() -> Select[Any]:
        """Return base ``SELECT`` joining ``nodes_table`` columns with ``files_table`` aliases."""
        return select(
            nodes_table,
            files_table.c.file_path.label("_file_path"),
            files_table.c.module_path.label("_module_path"),
            files_table.c.is_test.label("_is_test"),
        )

    @staticmethod
    def _is_private_member(summary: SymbolSummary) -> bool:
        """Return ``True`` when ``summary`` represents a private-like or local symbol."""
        if summary.visibility_hint in ("private_like", "local"):
            return True
        return summary.name.startswith("_") and not (
            summary.name.startswith("__") and summary.name.endswith("__")
        )

    def upsert(self, node: SymbolNode) -> None:
        """Insert or update a single ``SymbolNode``."""
        stmt = build_sqlite_upsert(nodes_table)
        with db_operation(f"Failed to upsert node {node.id}", context={"node_id": node.id}):
            self._conn.execute(stmt, node_to_row(node))
            logger.debug("Upserted node %s", node.id)

    def upsert_many(self, nodes: Sequence[SymbolNode]) -> None:
        """Insert or update a batch of ``SymbolNode`` entities in bounded chunks."""
        if not nodes:
            return
        stmt = build_sqlite_upsert(nodes_table)
        with db_operation(
            f"Failed to upsert {len(nodes)} nodes",
            context={"count": len(nodes)},
        ):
            for chunk in chunked_sequence(nodes, _BATCH_SIZE):
                self._conn.execute(stmt, [node_to_row(n) for n in chunk])
            logger.debug("Upserted %d nodes", len(nodes))

    def get_by_id(self, node_id: str) -> SymbolNode | None:
        """Fetch a ``SymbolNode`` by its exact identifier."""
        with db_operation(f"Failed to fetch node {node_id}", context={"node_id": node_id}):
            row = (
                self._conn
                .execute(select(nodes_table).where(nodes_table.c.id == node_id))
                .mappings()
                .first()
            )
        return row_to_node(row) if row is not None else None

    def get_by_ids(self, node_ids: Sequence[str]) -> dict[str, SymbolNode]:
        """Batch-fetch ``SymbolNode`` entities keyed by ``node_id`` in 400-ID chunks."""
        unique_ids = list(dict.fromkeys(nid for nid in node_ids if nid))
        if not unique_ids:
            return {}
        result: dict[str, SymbolNode] = {}
        with db_operation("Failed to batch-fetch nodes"):
            for chunk in chunked_sequence(unique_ids, _BATCH_SIZE):
                rows = (
                    self._conn
                    .execute(select(nodes_table).where(nodes_table.c.id.in_(chunk)))
                    .mappings()
                    .all()
                )
                for row in rows:
                    node = row_to_node(row)
                    result[node.id] = node
        return result

    def get_summary_by_id(self, node_id: str) -> SymbolSummary | None:
        """Fetch a single ``SymbolSummary`` joined with file and module metadata."""
        stmt = (
            self
            ._summary_select()
            .select_from(
                nodes_table.outerjoin(files_table, files_table.c.id == nodes_table.c.file_id)
            )
            .where(nodes_table.c.id == node_id)
        )
        with db_operation(
            f"Failed to fetch symbol summary {node_id}",
            context={"node_id": node_id},
        ):
            row = self._conn.execute(stmt).mappings().first()
        return row_to_symbol_summary(row) if row is not None else None

    def get_summaries_by_ids(self, node_ids: Sequence[str]) -> dict[str, SymbolSummary]:
        """Batch-fetch ``SymbolSummary`` projections keyed by ``node_id`` in 400-ID chunks."""
        unique_ids = list(dict.fromkeys(nid for nid in node_ids if nid))
        if not unique_ids:
            return {}
        joined = nodes_table.outerjoin(files_table, files_table.c.id == nodes_table.c.file_id)
        result: dict[str, SymbolSummary] = {}
        with db_operation("Failed to batch-fetch symbol summaries"):
            for chunk in chunked_sequence(unique_ids, _BATCH_SIZE):
                rows = (
                    self._conn
                    .execute(
                        self
                        ._summary_select()
                        .select_from(joined)
                        .where(nodes_table.c.id.in_(chunk))
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    summary = row_to_symbol_summary(row)
                    result[summary.id] = summary
        return result

    def get_by_qualified_name(
        self,
        repo_id: str,
        qualified_name: str,
        kind: SymbolKind | None = None,
    ) -> SymbolNode | None:
        """Fetch a unique ``SymbolNode`` by ``(repo_id, qualified_name)`` and optional ``kind``."""
        stmt = select(nodes_table).where(
            nodes_table.c.repo_id == repo_id,
            nodes_table.c.qualified_name == qualified_name,
        )
        if kind is not None:
            stmt = stmt.where(nodes_table.c.kind == str(kind))
        with db_operation(
            f"Failed to fetch symbol {qualified_name!r}",
            context={"repo_id": repo_id, "qualified_name": qualified_name},
        ):
            rows = self._conn.execute(stmt.limit(2)).mappings().all()
        return row_to_node(rows[0]) if len(rows) == 1 else None

    def _query_node_list(self, stmt: Select[Any], context_label: str) -> list[SymbolNode]:
        """Execute a ``nodes_table`` ``SELECT`` statement and map rows to ``SymbolNode``s."""
        with db_operation(f"Failed querying symbol nodes ({context_label})"):
            rows = self._conn.execute(stmt).mappings().all()
        return [row_to_node(row) for row in rows]

    def list_for_file(self, file_id: str) -> list[SymbolNode]:
        """List all symbol nodes declared in ``file_id`` ordered by source position."""
        stmt = (
            select(nodes_table)
            .where(nodes_table.c.file_id == file_id)
            .order_by(
                func.coalesce(nodes_table.c.start_line, 0),
                nodes_table.c.kind,
                nodes_table.c.qualified_name,
                nodes_table.c.id,
            )
        )
        return self._query_node_list(stmt, f"file_id={file_id}")

    def list_for_repo(self, repo_id: str) -> list[SymbolNode]:
        """List all symbol nodes belonging to ``repo_id``."""
        stmt = (
            select(nodes_table)
            .where(nodes_table.c.repo_id == repo_id)
            .order_by(nodes_table.c.kind, nodes_table.c.qualified_name, nodes_table.c.id)
        )
        return self._query_node_list(stmt, f"repo_id={repo_id}")

    def list_children(self, parent_id: str) -> list[SymbolNode]:
        """List direct structural child symbol nodes whose ``parent_id`` matches."""
        stmt = (
            select(nodes_table)
            .where(nodes_table.c.parent_id == parent_id)
            .order_by(
                func.coalesce(nodes_table.c.start_line, 0),
                nodes_table.c.kind,
                nodes_table.c.name,
                nodes_table.c.id,
            )
        )
        return self._query_node_list(stmt, f"parent_id={parent_id}")

    def list_lexical_children(self, lexical_parent_id: str) -> list[SymbolNode]:
        """List nested lexical child symbol nodes whose ``lexical_parent_id`` matches."""
        stmt = (
            select(nodes_table)
            .where(nodes_table.c.lexical_parent_id == lexical_parent_id)
            .order_by(
                func.coalesce(nodes_table.c.start_line, 0),
                nodes_table.c.kind,
                nodes_table.c.name,
                nodes_table.c.id,
            )
        )
        return self._query_node_list(stmt, f"lexical_parent_id={lexical_parent_id}")

    def list_member_summaries(
        self,
        parent_id: str,
        *,
        include_private: bool = True,
    ) -> list[SymbolSummary]:
        """List direct member ``SymbolSummary`` items of a class or module symbol."""
        stmt = (
            self
            ._summary_select()
            .select_from(
                nodes_table.outerjoin(files_table, files_table.c.id == nodes_table.c.file_id)
            )
            .where(nodes_table.c.parent_id == parent_id)
            .order_by(
                func.coalesce(nodes_table.c.start_line, 0),
                nodes_table.c.kind,
                nodes_table.c.qualified_name,
                nodes_table.c.id,
            )
        )
        with db_operation(
            f"Failed to list member summaries for {parent_id}",
            context={"parent_id": parent_id},
        ):
            rows = self._conn.execute(stmt).mappings().all()

        summaries = [row_to_symbol_summary(row) for row in rows]
        if include_private:
            return summaries
        return [item for item in summaries if not self._is_private_member(item)]

    def find_by_name(
        self,
        repo_id: str,
        name_pattern: str,
        kind: SymbolKind | None = None,
        limit: int = 50,
    ) -> list[SymbolNode]:
        """Find symbol nodes matching a literal substring via escaped SQL ``LIKE``."""
        if not name_pattern or limit <= 0:
            return []
        like_pattern = f"%{escape_like(name_pattern)}%"
        stmt = select(nodes_table).where(
            nodes_table.c.repo_id == repo_id,
            or_(
                nodes_table.c.name.like(like_pattern, escape="\\"),
                nodes_table.c.qualified_name.like(like_pattern, escape="\\"),
            ),
        )
        if kind is not None:
            stmt = stmt.where(nodes_table.c.kind == str(kind))
        stmt = stmt.order_by(
            nodes_table.c.kind,
            nodes_table.c.qualified_name,
            nodes_table.c.id,
        ).limit(limit)
        return self._query_node_list(stmt, f"find_by_name={name_pattern!r}")

    @staticmethod
    def _search_rank_case(raw_query: str, prefix_pattern: str) -> ColumnElement[int]:
        """Build deterministic exact/prefix match priority ranking ``CASE`` expression."""
        return case(
            (func.lower(nodes_table.c.name) == func.lower(raw_query), 0),
            (func.lower(nodes_table.c.qualified_name) == func.lower(raw_query), 1),
            (func.lower(nodes_table.c.name).like(func.lower(prefix_pattern), escape="\\"), 2),
            (
                func.lower(nodes_table.c.qualified_name).like(
                    func.lower(prefix_pattern),
                    escape="\\",
                ),
                3,
            ),
            else_=4,
        )

    @staticmethod
    def _apply_optional_symbol_filters(
        stmt: Select[Any],
        kind: SymbolKind | None,
        file_path: str | None,
    ) -> Select[Any]:
        """Apply optional ``kind`` and ``file_path`` filters to a joined symbol query."""
        if kind is not None:
            stmt = stmt.where(nodes_table.c.kind == str(kind))
        prefix_clause = build_rel_path_prefix_clause(files_table.c.file_path, file_path)
        if prefix_clause is not None:
            stmt = stmt.where(prefix_clause)
        return stmt

    def _search_like_fallback(
        self,
        query: SymbolSearchQuery,
        raw_query: str,
    ) -> list[SymbolSummary]:
        """Execute escaped ``LIKE`` symbol search when FTS5 is bypassed or returns 0 hits."""
        escaped = escape_like(raw_query)
        sub_pattern = f"%{escaped}%"
        prefix_pattern = f"{escaped}%"
        has_file_filter = bool(normalize_rel_path_prefix(query.file_path))
        from_clause = (
            nodes_table.join(files_table, files_table.c.id == nodes_table.c.file_id)
            if has_file_filter
            else nodes_table.outerjoin(files_table, files_table.c.id == nodes_table.c.file_id)
        )
        stmt = (
            self
            ._summary_select()
            .select_from(from_clause)
            .where(
                nodes_table.c.repo_id == query.repo_id,
                or_(
                    nodes_table.c.name.like(sub_pattern, escape="\\"),
                    nodes_table.c.qualified_name.like(sub_pattern, escape="\\"),
                    func.coalesce(nodes_table.c.signature, "").like(sub_pattern, escape="\\"),
                    func.coalesce(nodes_table.c.doc_summary, "").like(sub_pattern, escape="\\"),
                ),
            )
        )
        stmt = self._apply_optional_symbol_filters(stmt, query.kind, query.file_path)
        stmt = stmt.order_by(
            self._search_rank_case(raw_query, prefix_pattern).asc(),
            nodes_table.c.kind.asc(),
            nodes_table.c.qualified_name.asc(),
            nodes_table.c.id.asc(),
        ).limit(query.limit)

        with db_operation(
            f"Symbol LIKE search failed for {raw_query!r}",
            context={"repo_id": query.repo_id, "query": raw_query},
        ):
            rows = self._conn.execute(stmt).mappings().all()
        return [row_to_symbol_summary(row) for row in rows]

    def search_fts(self, query: SymbolSearchQuery) -> list[SymbolSummary]:
        """Search symbols via FTS5 (with deterministic ``LIKE`` fallback)."""
        raw_query = query.query.strip()
        if not raw_query or query.limit <= 0:
            return []

        fts_expr = build_fts_match_expression(raw_query)
        if fts_expr is None:
            return self._search_like_fallback(query, raw_query)

        prefix_pattern = f"{escape_like(raw_query)}%"
        has_file_filter = bool(normalize_rel_path_prefix(query.file_path))
        base_join = nodes_fts_table.join(
            nodes_table,
            literal_column("nodes.rowid") == nodes_fts_table.c.rowid,
        )
        from_clause = (
            base_join.join(files_table, files_table.c.id == nodes_table.c.file_id)
            if has_file_filter
            else base_join.outerjoin(files_table, files_table.c.id == nodes_table.c.file_id)
        )
        stmt = (
            self
            ._summary_select()
            .select_from(from_clause)
            .where(
                literal_column("nodes_fts").op("MATCH")(fts_expr),
                nodes_table.c.repo_id == query.repo_id,
            )
        )
        stmt = self._apply_optional_symbol_filters(stmt, query.kind, query.file_path)
        stmt = stmt.order_by(
            self._search_rank_case(raw_query, prefix_pattern).asc(),
            func.bm25(literal_column("nodes_fts"), 0.0, 0.0, 10.0, 8.0, 3.0, 1.0).asc(),
            nodes_table.c.kind.asc(),
            nodes_table.c.qualified_name.asc(),
            nodes_table.c.id.asc(),
        ).limit(query.limit)

        with db_operation(
            f"Symbol FTS search failed for {raw_query!r}",
            context={"repo_id": query.repo_id, "query": raw_query},
        ):
            try:
                rows = self._conn.execute(stmt).mappings().all()
            except OperationalError as exc:
                logger.warning(
                    "FTS5 query failed for %r in repo %s (%s); falling back to escaped LIKE",
                    raw_query,
                    query.repo_id,
                    exc,
                )
                return self._search_like_fallback(query, raw_query)

        if not rows:
            return self._search_like_fallback(query, raw_query)
        return [row_to_symbol_summary(row) for row in rows]

    def list_paginated_summaries(self, query: SymbolPageQuery) -> SymbolPageResult:
        """Return a filtered, paginated page of ``SymbolSummary`` items."""
        joined = nodes_table.join(files_table, files_table.c.id == nodes_table.c.file_id)
        count_stmt = self._apply_optional_symbol_filters(
            select(func.count()).select_from(joined).where(nodes_table.c.repo_id == query.repo_id),
            query.kind,
            query.file_path,
        )
        items_stmt = self._apply_optional_symbol_filters(
            self
            ._summary_select()
            .select_from(joined)
            .where(nodes_table.c.repo_id == query.repo_id),
            query.kind,
            query.file_path,
        ).order_by(
            files_table.c.file_path,
            func.coalesce(nodes_table.c.start_line, 0),
            nodes_table.c.kind,
            nodes_table.c.qualified_name,
            nodes_table.c.id,
        )
        items_stmt = items_stmt.limit(query.limit).offset(query.offset)

        with db_operation(
            f"Failed to list paginated symbols for {query.repo_id}",
            context={"repo_id": query.repo_id},
        ):
            total_count = int(self._conn.execute(count_stmt).scalar_one())
            rows = self._conn.execute(items_stmt).mappings().all()

        return SymbolPageResult(
            total_count=total_count,
            items=[row_to_symbol_summary(row) for row in rows],
        )

    def _execute_candidate_query(
        self,
        where_cond: ColumnElement[bool],
        repo_id: str | None,
        limit: int,
    ) -> list[SymbolSummary]:
        """Run a joined ``SymbolSummary`` candidate lookup with optional ``repo_id`` filter."""
        joined = nodes_table.outerjoin(files_table, files_table.c.id == nodes_table.c.file_id)
        stmt = self._summary_select().select_from(joined).where(where_cond)
        if repo_id is not None:
            stmt = stmt.where(nodes_table.c.repo_id == repo_id)
        stmt = stmt.order_by(
            nodes_table.c.kind,
            nodes_table.c.qualified_name,
            nodes_table.c.id,
        ).limit(limit)
        rows = self._conn.execute(stmt).mappings().all()
        return [row_to_symbol_summary(row) for row in rows]

    def _resolve_file_colon_candidates(
        self,
        repo_id: str | None,
        cleaned: str,
        limit: int,
    ) -> list[SymbolSummary]:
        """Resolve ``path/file.py:symbol`` selectors via exact then suffix ``file_path``."""
        raw_path, raw_sym = cleaned.split(":", 1)
        clean_path = normalize_rel_path_prefix(raw_path)
        clean_sym = raw_sym.strip()
        if not clean_path or not clean_sym:
            return []

        sym_cond = or_(
            nodes_table.c.qualified_name == clean_sym,
            nodes_table.c.name == clean_sym,
            nodes_table.c.qualified_name.like(f"%.{escape_like(clean_sym)}", escape="\\"),
        )
        file_tiers = (
            files_table.c.file_path == clean_path,
            files_table.c.file_path.like(f"%/{escape_like(clean_path)}", escape="\\"),
        )
        for file_cond in file_tiers:
            hits = self._execute_candidate_query(and_(file_cond, sym_cond), repo_id, limit)
            if hits:
                return hits
        return []

    def _resolve_name_tier_candidates(
        self,
        repo_id: str | None,
        cleaned: str,
        limit: int,
    ) -> list[SymbolSummary]:
        """Resolve qualified name, ``src.``-prefixed qualified name, short name, or suffix."""
        conditions: list[ColumnElement[bool]] = [nodes_table.c.qualified_name == cleaned]
        if not cleaned.startswith("src."):
            conditions.append(nodes_table.c.qualified_name == f"src.{cleaned}")
        conditions.append(nodes_table.c.name == cleaned)
        conditions.append(
            nodes_table.c.qualified_name.like(f"%.{escape_like(cleaned)}", escape="\\")
        )
        for cond in conditions:
            hits = self._execute_candidate_query(cond, repo_id, limit)
            if hits:
                return hits
        return []

    def resolve_candidates(
        self,
        repo_id: str | None,
        selector: str,
        limit: int = 20,
    ) -> list[SymbolSummary]:
        """Find candidate symbols matching an ID, ``file:symbol``, qualified name, or name."""
        cleaned = selector.strip()
        if not cleaned or limit <= 0:
            return []

        with db_operation(
            f"Failed to resolve symbol candidates for {cleaned!r}",
            context={"repo_id": repo_id, "selector": cleaned},
        ):
            if cleaned.startswith(PREFIX_SYMBOL):
                return self._execute_candidate_query(nodes_table.c.id == cleaned, repo_id, limit)
            if ":" in cleaned:
                return self._resolve_file_colon_candidates(repo_id, cleaned, limit)
            return self._resolve_name_tier_candidates(repo_id, cleaned, limit)
