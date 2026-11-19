"""SQLAlchemy Core adapter implementing ``EdgeRepositoryProtocol``."""

import sqlite3
from collections.abc import Sequence
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    bindparam,
    case,
    delete,
    distinct,
    exists,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from core.models.edge import (
    DirectedEdgeQuery,
    Edge,
    EdgeBucketStats,
    EdgeTargetUpdate,
    LowConfidenceEdgeSample,
    ReferencedByDetail,
    ReferencingSiteRef,
)
from core.models.enums import REFERENCE_AND_DEPENDENCY_EDGE_KINDS, EdgeKind
from core.models.exceptions import DatabaseError, ValidationError
from core.utils.logging import get_logger
from core.utils.timestamps import latest_timestamp
from storage.utils.mappers import edge_to_row, row_to_edge, row_to_referenced_by
from storage.utils.schema import edges_table, files_table, nodes_table
from storage.utils.sql_helpers import (
    chunked_sequence,
    db_error_message,
    evidence_char_expr,
    evidence_line_expr,
)

logger = get_logger("storage.services.edges")

_BATCH_SIZE = 400
_BIDIRECTIONAL_CHUNK_SIZE = 200


class SqlEdgeRepository:
    """SQLite repository adapter for ``Edge`` relationships and reference queries."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``.

        Args:
            conn: Active SQLAlchemy ``Connection``.
        """
        self._conn = conn

    @staticmethod
    def _build_upsert_stmt() -> Any:
        """Construct an ``INSERT ... ON CONFLICT(id) DO UPDATE`` statement for ``edges_table``.

        Returns:
            SQLite dialect upsert statement.
        """
        base = sqlite_insert(edges_table)
        return base.on_conflict_do_update(
            index_elements=[edges_table.c.id],
            set_={
                col.name: base.excluded[col.name] for col in edges_table.columns if col.name != "id"
            },
        )

    @staticmethod
    def _normalize_kinds(
        kinds: Sequence[EdgeKind] | None,
        default: Sequence[EdgeKind],
    ) -> list[str]:
        """Normalize an optional ``EdgeKind`` sequence into deduplicated string values.

        Args:
            kinds: Optional caller-supplied ``EdgeKind`` sequence.
            default: Default ``EdgeKind`` sequence used when ``kinds is None``.

        Returns:
            Deduplicated list of ``EdgeKind`` string values.
        """
        source = default if kinds is None else kinds
        return list(dict.fromkeys(str(k) for k in source if k))

    @staticmethod
    def _direction_condition(
        symbol_id: str,
        direction: str,
        kind_values: Sequence[str],
    ) -> ColumnElement[bool]:
        """Build SQLAlchemy filter expression for ``incoming``, ``outgoing``, or ``both``.

        Args:
            symbol_id: Anchor symbol identifier.
            direction: ``"incoming"``, ``"outgoing"``, or ``"both"``.
            kind_values: Non-empty sequence of allowed ``EdgeKind`` string values.

        Returns:
            SQLAlchemy boolean expression.

        Raises:
            ValidationError: If ``direction`` is not one of the three supported values.
        """
        kind_cond = edges_table.c.kind.in_(kind_values)
        if direction == "incoming":
            return and_(edges_table.c.to_id == symbol_id, kind_cond)
        if direction == "outgoing":
            return and_(edges_table.c.from_id == symbol_id, kind_cond)
        if direction == "both":
            return and_(
                or_(edges_table.c.to_id == symbol_id, edges_table.c.from_id == symbol_id),
                kind_cond,
            )
        raise ValidationError(
            f"Invalid direction {direction!r}; expected 'incoming', 'outgoing', or 'both'",
            context={"direction": direction},
        )

    def _query_edge_list(self, stmt: Select[Any], context_label: str) -> list[Edge]:
        """Execute an ``edges_table`` ``SELECT`` statement and map rows to ``Edge`` models.

        Args:
            stmt: Prepared SQLAlchemy ``Select`` query.
            context_label: Human-readable query context for error reporting.

        Returns:
            Ordered list of ``Edge`` instances.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            rows = self._conn.execute(stmt).mappings().all()
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed querying edges (%s): %s", context_label, msg)
            raise DatabaseError(f"Failed querying edges ({context_label}): {msg}") from exc
        return [row_to_edge(row) for row in rows]

    def upsert(self, edge: Edge) -> None:
        """Insert or update a single graph ``Edge``.

        Args:
            edge: Graph edge entity to persist.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            self._conn.execute(self._build_upsert_stmt(), edge_to_row(edge))
            logger.debug("Upserted edge %s", edge.id)
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed upserting edge %s: %s", edge.id, msg)
            raise DatabaseError(
                f"Failed to upsert edge {edge.id}: {msg}",
                context={"edge_id": edge.id},
            ) from exc

    def upsert_many(self, edges: Sequence[Edge]) -> None:
        """Insert or update a batch of graph ``Edge`` entities in bounded chunks.

        Args:
            edges: Sequence of ``Edge`` entities to persist.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        if not edges:
            return
        stmt = self._build_upsert_stmt()
        try:
            for chunk in chunked_sequence(edges, _BATCH_SIZE):
                self._conn.execute(stmt, [edge_to_row(e) for e in chunk])
            logger.debug("Upserted %d edges", len(edges))
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed batch-upserting %d edges: %s", len(edges), msg)
            raise DatabaseError(
                f"Failed to upsert {len(edges)} edges: {msg}",
                context={"count": len(edges)},
            ) from exc

    def get_by_id(self, edge_id: str) -> Edge | None:
        """Fetch an ``Edge`` by its exact identifier.

        Args:
            edge_id: Canonical edge identifier.

        Returns:
            Matching ``Edge`` or ``None`` if absent.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            row = (
                self._conn
                .execute(select(edges_table).where(edges_table.c.id == edge_id))
                .mappings()
                .first()
            )
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed fetching edge %s: %s", edge_id, msg)
            raise DatabaseError(
                f"Failed to fetch edge {edge_id}: {msg}",
                context={"edge_id": edge_id},
            ) from exc
        return row_to_edge(row) if row is not None else None

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

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        stmt = select(edges_table).where(edges_table.c.repo_id == repo_id)
        if kinds is not None:
            kind_vals = [str(k) for k in dict.fromkeys(kinds)]
            if not kind_vals:
                return []
            stmt = stmt.where(edges_table.c.kind.in_(kind_vals))
        stmt = stmt.order_by(
            edges_table.c.kind,
            edges_table.c.from_id,
            edges_table.c.to_id,
            edges_table.c.id,
        )
        return self._query_edge_list(stmt, f"repo_id={repo_id}")

    def list_for_file(self, file_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        """List edges evidenced by ``file_id``, optionally filtered by ``kind``.

        Args:
            file_id: Evidence file identifier.
            kind: Optional ``EdgeKind`` filter.

        Returns:
            Ordered list of ``Edge`` entities.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        stmt = select(edges_table).where(edges_table.c.evidence_file_id == file_id)
        if kind is not None:
            stmt = stmt.where(edges_table.c.kind == str(kind))
        stmt = stmt.order_by(
            edges_table.c.kind,
            evidence_line_expr(),
            evidence_char_expr(),
            edges_table.c.from_id,
            edges_table.c.to_id,
            edges_table.c.id,
        )
        return self._query_edge_list(stmt, f"file_id={file_id}")

    def list_outgoing(self, from_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        """List outgoing edges originating from ``from_id``.

        Args:
            from_id: Source symbol identifier.
            kind: Optional ``EdgeKind`` filter.

        Returns:
            Ordered list of outgoing ``Edge`` entities.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        stmt = select(edges_table).where(edges_table.c.from_id == from_id)
        if kind is not None:
            stmt = stmt.where(edges_table.c.kind == str(kind))
        stmt = stmt.order_by(
            edges_table.c.kind,
            edges_table.c.evidence_uri,
            evidence_line_expr(),
            evidence_char_expr(),
            edges_table.c.to_id,
            edges_table.c.id,
        )
        return self._query_edge_list(stmt, f"outgoing={from_id}")

    def list_incoming(self, to_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        """List incoming edges targeting ``to_id``.

        Args:
            to_id: Target symbol identifier.
            kind: Optional ``EdgeKind`` filter.

        Returns:
            Ordered list of incoming ``Edge`` entities.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        stmt = select(edges_table).where(edges_table.c.to_id == to_id)
        if kind is not None:
            stmt = stmt.where(edges_table.c.kind == str(kind))
        stmt = stmt.order_by(
            edges_table.c.kind,
            edges_table.c.evidence_uri,
            evidence_line_expr(),
            evidence_char_expr(),
            edges_table.c.from_id,
            edges_table.c.id,
        )
        return self._query_edge_list(stmt, f"incoming={to_id}")

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

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        kind_vals = [str(k) for k in dict.fromkeys(kinds)]
        if not kind_vals:
            return []
        stmt = (
            select(edges_table)
            .where(
                edges_table.c.to_id == to_id,
                edges_table.c.kind.in_(kind_vals),
            )
            .order_by(edges_table.c.kind, edges_table.c.from_id, edges_table.c.id)
        )
        return self._query_edge_list(stmt, f"target={to_id}")

    def list_directed(self, query: DirectedEdgeQuery) -> list[Edge]:
        """List incoming, outgoing, or bidirectional edges ordered by evidence site.

        Args:
            query: Direction, kind filter, and pagination parameters.

        Returns:
            Ordered list of matching ``Edge`` entities.

        Raises:
            ValidationError: If ``query.direction`` or ``query.offset`` is invalid.
            DatabaseError: If SQLite execution fails.
        """
        if query.offset < 0:
            raise ValidationError("offset must be >= 0", context={"offset": query.offset})
        if query.limit is not None and query.limit <= 0:
            return []
        resolved_kinds = self._normalize_kinds(query.kinds, REFERENCE_AND_DEPENDENCY_EDGE_KINDS)
        if not resolved_kinds:
            return []

        kind_priority = case(
            (edges_table.c.kind == str(EdgeKind.REFERENCES), 0),
            (edges_table.c.kind == str(EdgeKind.CALLS), 1),
            (edges_table.c.kind == str(EdgeKind.IMPORTS), 2),
            (edges_table.c.kind == str(EdgeKind.INHERITS), 3),
            else_=4,
        )
        stmt = (
            select(edges_table)
            .where(self._direction_condition(query.symbol_id, query.direction, resolved_kinds))
            .order_by(
                kind_priority.asc(),
                edges_table.c.evidence_uri,
                evidence_line_expr(),
                evidence_char_expr(),
                edges_table.c.id,
            )
        )
        if query.offset > 0:
            stmt = stmt.offset(query.offset)
        if query.limit is not None:
            stmt = stmt.limit(query.limit)
        return self._query_edge_list(stmt, f"directed={query.symbol_id}:{query.direction}")

    def count_directed(self, query: DirectedEdgeQuery) -> int:
        """Count total edges matching a ``DirectedEdgeQuery`` (ignoring ``limit``/``offset``).

        Args:
            query: Direction and kind filter parameters.

        Returns:
            Total matching edge count.

        Raises:
            ValidationError: If ``query.direction`` is invalid.
            DatabaseError: If SQLite execution fails.
        """
        resolved_kinds = self._normalize_kinds(query.kinds, REFERENCE_AND_DEPENDENCY_EDGE_KINDS)
        if not resolved_kinds:
            return 0
        stmt = (
            select(func.count())
            .select_from(edges_table)
            .where(self._direction_condition(query.symbol_id, query.direction, resolved_kinds))
        )
        try:
            return int(self._conn.execute(stmt).scalar_one())
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed counting directed edges for %s: %s", query.symbol_id, msg)
            raise DatabaseError(
                f"Failed to count directed edges for {query.symbol_id}: {msg}",
                context={"symbol_id": query.symbol_id, "direction": query.direction},
            ) from exc

    def list_neighborhood_edges(
        self,
        symbol_ids: Sequence[str],
        kinds: Sequence[EdgeKind] | None = None,
    ) -> list[Edge]:
        """List deduplicated edges touching any endpoint in ``symbol_ids`` in chunks.

        Args:
            symbol_ids: Seed symbol identifiers in the neighborhood frontier.
            kinds: Optional sequence of ``EdgeKind`` values to include.

        Returns:
            Deduplicated, deterministically ordered list of incident ``Edge`` entities.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        unique_ids = list(dict.fromkeys(sid for sid in symbol_ids if sid))
        if not unique_ids:
            return []
        kind_vals: list[str] | None = None
        if kinds is not None:
            kind_vals = [str(k) for k in dict.fromkeys(kinds)]
            if not kind_vals:
                return []

        edges_by_id: dict[str, Edge] = {}
        try:
            for chunk in chunked_sequence(unique_ids, _BIDIRECTIONAL_CHUNK_SIZE):
                stmt = select(edges_table).where(
                    or_(edges_table.c.from_id.in_(chunk), edges_table.c.to_id.in_(chunk))
                )
                if kind_vals is not None:
                    stmt = stmt.where(edges_table.c.kind.in_(kind_vals))
                for row in self._conn.execute(stmt).mappings().all():
                    edge = row_to_edge(row)
                    edges_by_id[edge.id] = edge
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed querying neighborhood edges (%d seeds): %s", len(unique_ids), msg)
            raise DatabaseError(f"Failed to query neighborhood edges: {msg}") from exc

        return sorted(edges_by_id.values(), key=lambda e: (str(e.kind), e.from_id, e.to_id, e.id))

    def list_referenced_by(
        self,
        target_id: str,
        kinds: Sequence[EdgeKind] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ReferencedByDetail]:
        """List distinct symbols referencing ``target_id`` joined with file metadata.

        Args:
            target_id: Target symbol identifier.
            kinds: Optional ``EdgeKind`` filter (defaults to reference/dependency kinds).
            limit: Optional page size limit.
            offset: Zero-based pagination offset.

        Returns:
            Ordered list of ``ReferencedByDetail`` items.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        if limit is not None and limit <= 0:
            return []
        resolved_kinds = self._normalize_kinds(kinds, REFERENCE_AND_DEPENDENCY_EDGE_KINDS)
        if not resolved_kinds:
            return []

        stmt = (
            select(
                nodes_table.c.id,
                nodes_table.c.repo_id,
                nodes_table.c.file_id,
                nodes_table.c.language,
                nodes_table.c.kind,
                nodes_table.c.name,
                nodes_table.c.qualified_name,
                nodes_table.c.uri,
                nodes_table.c.start_line,
                nodes_table.c.end_line,
                nodes_table.c.signature,
                nodes_table.c.scope,
                nodes_table.c.lexical_parent_id,
                files_table.c.file_path,
                files_table.c.module_path,
            )
            .distinct()
            .select_from(
                edges_table.join(nodes_table, edges_table.c.from_id == nodes_table.c.id).outerjoin(
                    files_table, files_table.c.id == nodes_table.c.file_id
                )
            )
            .where(edges_table.c.kind.in_(resolved_kinds), edges_table.c.to_id == target_id)
            .order_by(nodes_table.c.kind, nodes_table.c.qualified_name, nodes_table.c.id)
        )
        if offset > 0:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)

        try:
            rows = self._conn.execute(stmt).mappings().all()
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed listing referenced_by for %s: %s", target_id, msg)
            raise DatabaseError(
                f"Failed to list referencing symbols for {target_id}: {msg}",
                context={"target_id": target_id},
            ) from exc
        return [row_to_referenced_by(row) for row in rows]

    def list_referencing_modules(
        self,
        target_id: str,
        kinds: Sequence[EdgeKind] | None = None,
    ) -> list[str]:
        """List distinct Python ``module_path`` names referencing ``target_id``.

        Args:
            target_id: Target symbol identifier.
            kinds: Optional ``EdgeKind`` filter (defaults to reference/dependency kinds).

        Returns:
            Sorted list of referencing module paths.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        resolved_kinds = self._normalize_kinds(kinds, REFERENCE_AND_DEPENDENCY_EDGE_KINDS)
        if not resolved_kinds:
            return []

        resolved_file_id = func.coalesce(edges_table.c.evidence_file_id, nodes_table.c.file_id)
        stmt = (
            select(files_table.c.module_path)
            .distinct()
            .select_from(
                edges_table.outerjoin(nodes_table, edges_table.c.from_id == nodes_table.c.id).join(
                    files_table, files_table.c.id == resolved_file_id
                )
            )
            .where(edges_table.c.kind.in_(resolved_kinds), edges_table.c.to_id == target_id)
            .order_by(files_table.c.module_path)
        )
        try:
            rows = self._conn.execute(stmt).scalars().all()
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed listing referencing modules for %s: %s", target_id, msg)
            raise DatabaseError(
                f"Failed to list referencing modules for {target_id}: {msg}",
                context={"target_id": target_id},
            ) from exc
        return [str(mod) for mod in rows if mod]

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

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        kind_vals = [str(k) for k in dict.fromkeys(kinds)]
        if not kind_vals:
            return []
        resolved_file_id = func.coalesce(edges_table.c.evidence_file_id, nodes_table.c.file_id)
        stmt = (
            select(
                resolved_file_id.label("ref_file_id"),
                files_table.c.module_path.label("ref_module_path"),
            )
            .distinct()
            .select_from(
                edges_table.outerjoin(
                    nodes_table, edges_table.c.from_id == nodes_table.c.id
                ).outerjoin(files_table, files_table.c.id == resolved_file_id)
            )
            .where(edges_table.c.kind.in_(kind_vals), edges_table.c.to_id == target_id)
            .order_by(resolved_file_id, files_table.c.module_path)
        )
        try:
            rows = self._conn.execute(stmt).mappings().all()
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed listing referencing sites for %s: %s", target_id, msg)
            raise DatabaseError(
                f"Failed to list referencing sites for {target_id}: {msg}",
                context={"target_id": target_id},
            ) from exc
        return [
            ReferencingSiteRef(file_id=row["ref_file_id"], module_path=row["ref_module_path"])
            for row in rows
        ]

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

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        kind_vals = [str(k) for k in dict.fromkeys(kinds)]
        if not kind_vals:
            return EdgeBucketStats()

        resolved_file_id = func.coalesce(edges_table.c.evidence_file_id, nodes_table.c.file_id)
        resolved_mod_key = func.coalesce(
            files_table.c.module_path,
            edges_table.c.evidence_file_id,
            nodes_table.c.file_id,
        )
        stmt = (
            select(
                func.count().label("edge_count"),
                func.count(distinct(resolved_file_id)).label("file_count"),
                func.count(distinct(resolved_mod_key)).label("module_count"),
            )
            .select_from(
                edges_table.outerjoin(
                    nodes_table, edges_table.c.from_id == nodes_table.c.id
                ).outerjoin(files_table, files_table.c.id == resolved_file_id)
            )
            .where(edges_table.c.kind.in_(kind_vals), edges_table.c.to_id == target_id)
        )
        try:
            row = self._conn.execute(stmt).mappings().first()
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed computing bucket stats for %s: %s", target_id, msg)
            raise DatabaseError(
                f"Failed to compute edge bucket stats for {target_id}: {msg}",
                context={"target_id": target_id},
            ) from exc
        if row is None:
            return EdgeBucketStats()
        return EdgeBucketStats(
            edge_count=int(row["edge_count"] or 0),
            file_count=int(row["file_count"] or 0),
            module_count=int(row["module_count"] or 0),
        )

    def has_edge_between_or_for(self, symbol_id: str, kind: EdgeKind) -> bool:
        """Return ``True`` if ``symbol_id`` participates in any edge of ``kind``.

        Args:
            symbol_id: Symbol identifier (checked on ``from_id`` or ``to_id``).
            kind: ``EdgeKind`` to check.

        Returns:
            ``True`` when at least one matching edge exists.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        stmt = (
            select(edges_table.c.id)
            .where(
                edges_table.c.kind == str(kind),
                or_(edges_table.c.from_id == symbol_id, edges_table.c.to_id == symbol_id),
            )
            .limit(1)
        )
        try:
            return self._conn.execute(stmt).scalar_one_or_none() is not None
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed checking edge participation for %s (%s): %s", symbol_id, kind, msg)
            raise DatabaseError(
                f"Failed to check edge participation for {symbol_id}: {msg}",
                context={"symbol_id": symbol_id, "kind": str(kind)},
            ) from exc

    def has_incoming_kind(self, target_id: str, kind: EdgeKind) -> bool:
        """Return ``True`` if ``target_id`` has at least one incoming edge of ``kind``.

        Args:
            target_id: Target symbol identifier.
            kind: Incoming ``EdgeKind`` to check.

        Returns:
            ``True`` when at least one incoming edge of ``kind`` exists.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        stmt = (
            select(edges_table.c.id)
            .where(edges_table.c.kind == str(kind), edges_table.c.to_id == target_id)
            .limit(1)
        )
        try:
            return self._conn.execute(stmt).scalar_one_or_none() is not None
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed checking incoming %s edge for %s: %s", kind, target_id, msg)
            raise DatabaseError(
                f"Failed to check incoming edges for {target_id}: {msg}",
                context={"target_id": target_id, "kind": str(kind)},
            ) from exc

    def sample_low_confidence_references(
        self,
        target_ids: Sequence[str],
        threshold: float = 0.8,
        limit: int = 25,
    ) -> LowConfidenceEdgeSample:
        """Sample incoming ``references`` edges with ``confidence < threshold``.

        Args:
            target_ids: Target symbol identifiers to inspect.
            threshold: Exclusive upper confidence bound (default ``0.8``).
            limit: Maximum number of sampled edge IDs to return.

        Returns:
            ``LowConfidenceEdgeSample`` with sampled IDs and total matching count.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        unique_ids = list(dict.fromkeys(tid for tid in target_ids if tid))
        if not unique_ids or limit <= 0:
            return LowConfidenceEdgeSample()

        total_count = 0
        sampled_ids: list[str] = []
        try:
            for chunk in chunked_sequence(unique_ids, _BATCH_SIZE):
                filters = (
                    edges_table.c.kind == str(EdgeKind.REFERENCES),
                    edges_table.c.to_id.in_(chunk),
                    edges_table.c.confidence.is_not(None),
                    edges_table.c.confidence < threshold,
                )
                total_count += int(
                    self._conn.execute(
                        select(func.count()).select_from(edges_table).where(*filters)
                    ).scalar_one()
                )
                rows = (
                    self._conn
                    .execute(
                        select(edges_table.c.id)
                        .where(*filters)
                        .order_by(edges_table.c.id)
                        .limit(limit)
                    )
                    .scalars()
                    .all()
                )
                sampled_ids.extend(str(eid) for eid in rows)
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed sampling low-confidence references: %s", msg)
            raise DatabaseError(f"Failed to sample low-confidence references: {msg}") from exc

        return LowConfidenceEdgeSample(
            edge_ids=sorted(sampled_ids)[:limit],
            total_count=total_count,
        )

    def latest_indexed_at_for_symbol(self, symbol_id: str) -> str | None:
        """Return the latest ``last_indexed_at`` timestamp across edges touching ``symbol_id``.

        Args:
            symbol_id: Target or source symbol identifier.

        Returns:
            Latest ISO 8601 timestamp string or ``None`` if no timestamped edges exist.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        stmt = (
            select(edges_table.c.last_indexed_at)
            .distinct()
            .where(
                or_(edges_table.c.from_id == symbol_id, edges_table.c.to_id == symbol_id),
                edges_table.c.last_indexed_at.is_not(None),
            )
        )
        try:
            timestamps = [str(ts) for ts in self._conn.execute(stmt).scalars().all() if ts]
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed reading latest edge timestamp for %s: %s", symbol_id, msg)
            raise DatabaseError(
                f"Failed to read latest edge timestamp for {symbol_id}: {msg}",
                context={"symbol_id": symbol_id},
            ) from exc
        if not timestamps:
            return None
        return latest_timestamp(*timestamps)

    def update_resolved_targets(self, updates: Sequence[EdgeTargetUpdate]) -> int:
        """Apply batch ``to_id`` and ``confidence`` updates emitted by ``SymbolLinker``.

        Args:
            updates: Sequence of ``EdgeTargetUpdate`` specifications.

        Returns:
            Number of updated edge rows.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        if not updates:
            return 0
        stmt = (
            update(edges_table)
            .where(edges_table.c.id == bindparam("b_id"))
            .values(to_id=bindparam("b_to_id"), confidence=bindparam("b_confidence"))
        )
        updated = 0
        try:
            for chunk in chunked_sequence(updates, _BATCH_SIZE):
                payload = [
                    {"b_id": item.edge_id, "b_to_id": item.to_id, "b_confidence": item.confidence}
                    for item in chunk
                ]
                res = self._conn.execute(stmt, payload)
                updated += max(res.rowcount or 0, 0)
            logger.debug("Updated %d resolved edge targets", updated)
            return updated
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed updating %d resolved edge targets: %s", len(updates), msg)
            raise DatabaseError(
                f"Failed to update {len(updates)} resolved edge targets: {msg}",
                context={"count": len(updates)},
            ) from exc

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

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            with self._conn.begin_nested():
                self._conn.execute(
                    delete(edges_table).where(
                        edges_table.c.repo_id == repo_id,
                        edges_table.c.kind == str(EdgeKind.REFERENCES),
                        edges_table.c.to_id == target_symbol_id,
                    )
                )
                if edges:
                    self.upsert_many(edges)
            logger.debug(
                "Replaced %d LSP reference edges for target %s",
                len(edges),
                target_symbol_id,
            )
        except DatabaseError:
            raise
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed replacing LSP references for %s: %s", target_symbol_id, msg)
            raise DatabaseError(
                f"Failed to replace LSP references for {target_symbol_id}: {msg}",
                context={"repo_id": repo_id, "target_symbol_id": target_symbol_id},
            ) from exc

    def _execute_delete(self, stmt: Any, context_label: str) -> int:
        """Execute a ``DELETE`` statement on ``edges_table`` and return affected row count.

        Args:
            stmt: SQLAlchemy ``Delete`` statement.
            context_label: Human-readable label for error diagnostics.

        Returns:
            Number of deleted edge rows.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
            res = self._conn.execute(stmt)
            deleted = max(res.rowcount or 0, 0)
            logger.debug("Deleted %d edges (%s)", deleted, context_label)
            return deleted
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed deleting edges (%s): %s", context_label, msg)
            raise DatabaseError(f"Failed deleting edges ({context_label}): {msg}") from exc

    def delete_owned_for_file(self, file_id: str) -> int:
        """Delete edges evidenced by or originating from symbols inside ``file_id``.

        Args:
            file_id: Containing file identifier.

        Returns:
            Number of deleted edge rows.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        file_node_ids = select(nodes_table.c.id).where(nodes_table.c.file_id == file_id)
        stmt = delete(edges_table).where(
            or_(
                edges_table.c.evidence_file_id == file_id,
                edges_table.c.from_id.in_(file_node_ids),
            )
        )
        return self._execute_delete(stmt, f"owned_for_file={file_id}")

    def delete_all_for_file(self, file_id: str) -> int:
        """Delete all edges evidenced by or touching symbols inside ``file_id``.

        Args:
            file_id: Containing file identifier.

        Returns:
            Number of deleted edge rows.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        file_node_ids = select(nodes_table.c.id).where(nodes_table.c.file_id == file_id)
        stmt = delete(edges_table).where(
            or_(
                edges_table.c.evidence_file_id == file_id,
                edges_table.c.from_id.in_(file_node_ids),
                edges_table.c.to_id.in_(file_node_ids),
            )
        )
        return self._execute_delete(stmt, f"all_for_file={file_id}")

    def delete_lsp_references_for_file(self, file_id: str) -> int:
        """Delete LSP ``references`` edges evidenced by or targeting symbols in ``file_id``.

        Args:
            file_id: Modified or deleted file identifier.

        Returns:
            Number of deleted LSP reference edge rows.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        file_node_ids = select(nodes_table.c.id).where(nodes_table.c.file_id == file_id)
        stmt = delete(edges_table).where(
            edges_table.c.kind == str(EdgeKind.REFERENCES),
            or_(
                edges_table.c.evidence_file_id == file_id,
                edges_table.c.from_id.in_(file_node_ids),
                edges_table.c.to_id.in_(file_node_ids),
            ),
        )
        return self._execute_delete(stmt, f"lsp_refs_for_file={file_id}")

    def delete_lsp_references_for_symbols(self, symbol_ids: Sequence[str]) -> int:
        """Delete LSP ``references`` edges originating from or targeting ``symbol_ids``.

        Args:
            symbol_ids: Sequence of symbol identifiers whose LSP reference edges should be removed.

        Returns:
            Number of deleted LSP reference edge rows.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        unique_ids = list(dict.fromkeys(sid for sid in symbol_ids if sid))
        if not unique_ids:
            return 0
        deleted = 0
        try:
            for chunk in chunked_sequence(unique_ids, _BIDIRECTIONAL_CHUNK_SIZE):
                res = self._conn.execute(
                    delete(edges_table).where(
                        edges_table.c.kind == str(EdgeKind.REFERENCES),
                        or_(
                            edges_table.c.from_id.in_(chunk),
                            edges_table.c.to_id.in_(chunk),
                        ),
                    )
                )
                deleted += max(res.rowcount or 0, 0)
            logger.debug(
                "Deleted %d LSP reference edges across %d symbols",
                deleted,
                len(unique_ids),
            )
            return deleted
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed deleting LSP references for %d symbols: %s", len(unique_ids), msg)
            raise DatabaseError(f"Failed deleting LSP references for symbols: {msg}") from exc

    def delete_dangling_lsp_references(self, repo_id: str) -> int:
        """Delete LSP ``references`` edges in ``repo_id`` whose endpoints no longer exist.

        Args:
            repo_id: Owning repository identifier.

        Returns:
            Number of deleted dangling LSP reference edges.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        missing_from = ~exists(select(1).where(nodes_table.c.id == edges_table.c.from_id))
        missing_to = ~exists(select(1).where(nodes_table.c.id == edges_table.c.to_id))
        stmt = delete(edges_table).where(
            edges_table.c.repo_id == repo_id,
            edges_table.c.kind == str(EdgeKind.REFERENCES),
            or_(missing_from, missing_to),
        )
        return self._execute_delete(stmt, f"dangling_lsp_refs={repo_id}")
