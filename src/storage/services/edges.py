"""SQLAlchemy Core adapter implementing ``EdgeRepositoryProtocol``."""

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
from sqlalchemy.engine import Connection

from core.models.edge import (
    DirectedEdgeQuery,
    Edge,
    EdgeBucketStats,
    EdgeTargetUpdate,
    LowConfidenceEdgeSample,
    ReferencingSiteRef,
)
from core.models.enums import REFERENCE_AND_DEPENDENCY_EDGE_KINDS, EdgeKind
from core.models.exceptions import ValidationError
from core.utils.logging import get_logger
from core.utils.timestamps import latest_timestamp
from storage.utils.mappers import edge_to_row, row_to_edge
from storage.utils.schema import edges_table, files_table, nodes_table
from storage.utils.sql_helpers import (
    build_sqlite_upsert,
    chunked_sequence,
    db_operation,
    delete_lsp_references_for_symbol_ids,
    evidence_char_expr,
    evidence_line_expr,
)

logger = get_logger("storage.services.edges")

_BATCH_SIZE = 400
_BIDIRECTIONAL_CHUNK_SIZE = 200


class SqlEdgeRepository:
    """SQLite repository adapter for ``Edge`` relationships and reference queries."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``."""
        self._conn = conn

    @staticmethod
    def _normalize_kinds(
        kinds: Sequence[EdgeKind] | None,
        default: Sequence[EdgeKind],
    ) -> list[str]:
        """Normalize an optional ``EdgeKind`` sequence into deduplicated string values."""
        source = default if kinds is None else kinds
        return list(dict.fromkeys(str(k) for k in source if k))

    @staticmethod
    def _direction_condition(
        symbol_id: str,
        direction: str,
        kind_values: Sequence[str],
    ) -> ColumnElement[bool]:
        """Build SQLAlchemy filter expression for ``incoming``, ``outgoing``, or ``both``."""
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

    @staticmethod
    def _referencing_site_join() -> tuple[ColumnElement[Any], Any]:
        """Return ``(resolved_file_id_expr, edges -> nodes -> files outerjoin)``."""
        resolved_file_id = func.coalesce(edges_table.c.evidence_file_id, nodes_table.c.file_id)
        joined = edges_table.outerjoin(
            nodes_table,
            edges_table.c.from_id == nodes_table.c.id,
        ).outerjoin(files_table, files_table.c.id == resolved_file_id)
        return resolved_file_id, joined

    def _query_edge_list(self, stmt: Select[Any], context_label: str) -> list[Edge]:
        """Execute an ``edges_table`` ``SELECT`` statement and map rows to ``Edge`` models."""
        with db_operation(f"Failed querying edges ({context_label})"):
            rows = self._conn.execute(stmt).mappings().all()
        return [row_to_edge(row) for row in rows]

    def upsert(self, edge: Edge) -> None:
        """Insert or update a single graph ``Edge``."""
        stmt = build_sqlite_upsert(edges_table)
        with db_operation(f"Failed to upsert edge {edge.id}", context={"edge_id": edge.id}):
            self._conn.execute(stmt, edge_to_row(edge))
            logger.debug("Upserted edge %s", edge.id)

    def upsert_many(self, edges: Sequence[Edge]) -> None:
        """Insert or update a batch of graph ``Edge`` entities in bounded chunks."""
        if not edges:
            return
        stmt = build_sqlite_upsert(edges_table)
        with db_operation(
            f"Failed to upsert {len(edges)} edges",
            context={"count": len(edges)},
        ):
            for chunk in chunked_sequence(edges, _BATCH_SIZE):
                self._conn.execute(stmt, [edge_to_row(e) for e in chunk])
            logger.debug("Upserted %d edges", len(edges))

    def get_by_id(self, edge_id: str) -> Edge | None:
        """Fetch an ``Edge`` by its exact identifier."""
        with db_operation(f"Failed to fetch edge {edge_id}", context={"edge_id": edge_id}):
            row = (
                self._conn
                .execute(select(edges_table).where(edges_table.c.id == edge_id))
                .mappings()
                .first()
            )
        return row_to_edge(row) if row is not None else None

    def list_for_repo(
        self,
        repo_id: str,
        kinds: Sequence[EdgeKind] | None = None,
    ) -> list[Edge]:
        """List edges belonging to ``repo_id``, optionally filtered by ``kinds``."""
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
        """List edges evidenced by ``file_id``, optionally filtered by ``kind``."""
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
        """List outgoing edges originating from ``from_id``."""
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
        """List incoming edges targeting ``to_id``."""
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
        """List incoming edges targeting ``to_id`` restricted to ``kinds``."""
        kind_vals = [str(k) for k in dict.fromkeys(kinds)]
        if not kind_vals:
            return []
        stmt = (
            select(edges_table)
            .where(edges_table.c.to_id == to_id, edges_table.c.kind.in_(kind_vals))
            .order_by(edges_table.c.kind, edges_table.c.from_id, edges_table.c.id)
        )
        return self._query_edge_list(stmt, f"target={to_id}")

    def list_directed(self, query: DirectedEdgeQuery) -> list[Edge]:
        """List incoming, outgoing, or bidirectional edges ordered by evidence site."""
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
        """Count total edges matching a ``DirectedEdgeQuery`` (ignoring ``limit``/``offset``)."""
        resolved_kinds = self._normalize_kinds(query.kinds, REFERENCE_AND_DEPENDENCY_EDGE_KINDS)
        if not resolved_kinds:
            return 0
        stmt = (
            select(func.count())
            .select_from(edges_table)
            .where(self._direction_condition(query.symbol_id, query.direction, resolved_kinds))
        )
        with db_operation(
            f"Failed to count directed edges for {query.symbol_id}",
            context={"symbol_id": query.symbol_id, "direction": query.direction},
        ):
            return int(self._conn.execute(stmt).scalar_one())

    def list_neighborhood_edges(
        self,
        symbol_ids: Sequence[str],
        kinds: Sequence[EdgeKind] | None = None,
    ) -> list[Edge]:
        """List deduplicated edges touching any endpoint in ``symbol_ids`` in chunks."""
        unique_ids = list(dict.fromkeys(sid for sid in symbol_ids if sid))
        if not unique_ids:
            return []
        kind_vals: list[str] | None = None
        if kinds is not None:
            kind_vals = [str(k) for k in dict.fromkeys(kinds)]
            if not kind_vals:
                return []

        edges_by_id: dict[str, Edge] = {}
        with db_operation("Failed to query neighborhood edges"):
            for chunk in chunked_sequence(unique_ids, _BIDIRECTIONAL_CHUNK_SIZE):
                stmt = select(edges_table).where(
                    or_(edges_table.c.from_id.in_(chunk), edges_table.c.to_id.in_(chunk))
                )
                if kind_vals is not None:
                    stmt = stmt.where(edges_table.c.kind.in_(kind_vals))
                for row in self._conn.execute(stmt).mappings().all():
                    edge = row_to_edge(row)
                    edges_by_id[edge.id] = edge

        return sorted(edges_by_id.values(), key=lambda e: (str(e.kind), e.from_id, e.to_id, e.id))

    def list_referencing_sites(
        self,
        target_id: str,
        kinds: Sequence[EdgeKind],
    ) -> list[ReferencingSiteRef]:
        """List distinct ``(file_id, module_path)`` reference sites targeting ``target_id``."""
        kind_vals = [str(k) for k in dict.fromkeys(kinds)]
        if not kind_vals:
            return []
        resolved_file_id, joined = self._referencing_site_join()
        stmt = (
            select(
                resolved_file_id.label("ref_file_id"),
                files_table.c.module_path.label("ref_module_path"),
            )
            .distinct()
            .select_from(joined)
            .where(edges_table.c.kind.in_(kind_vals), edges_table.c.to_id == target_id)
            .order_by(resolved_file_id, files_table.c.module_path)
        )
        with db_operation(
            f"Failed to list referencing sites for {target_id}",
            context={"target_id": target_id},
        ):
            rows = self._conn.execute(stmt).mappings().all()
        return [
            ReferencingSiteRef(file_id=row["ref_file_id"], module_path=row["ref_module_path"])
            for row in rows
        ]

    def compute_bucket_stats(
        self,
        target_id: str,
        kinds: Sequence[EdgeKind],
    ) -> EdgeBucketStats:
        """Compute incoming edge, file, and module counts for ``target_id`` across ``kinds``."""
        kind_vals = [str(k) for k in dict.fromkeys(kinds)]
        if not kind_vals:
            return EdgeBucketStats()

        resolved_file_id, joined = self._referencing_site_join()
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
            .select_from(joined)
            .where(edges_table.c.kind.in_(kind_vals), edges_table.c.to_id == target_id)
        )
        with db_operation(
            f"Failed to compute edge bucket stats for {target_id}",
            context={"target_id": target_id},
        ):
            row = self._conn.execute(stmt).mappings().first()
        if row is None:
            return EdgeBucketStats()
        return EdgeBucketStats(
            edge_count=int(row["edge_count"] or 0),
            file_count=int(row["file_count"] or 0),
            module_count=int(row["module_count"] or 0),
        )

    def has_edge_between_or_for(self, symbol_id: str, kind: EdgeKind) -> bool:
        """Return ``True`` if ``symbol_id`` participates in any edge of ``kind``."""
        stmt = (
            select(edges_table.c.id)
            .where(
                edges_table.c.kind == str(kind),
                or_(edges_table.c.from_id == symbol_id, edges_table.c.to_id == symbol_id),
            )
            .limit(1)
        )
        with db_operation(
            f"Failed to check edge participation for {symbol_id}",
            context={"symbol_id": symbol_id, "kind": str(kind)},
        ):
            return self._conn.execute(stmt).scalar_one_or_none() is not None

    def has_incoming_kind(self, target_id: str, kind: EdgeKind) -> bool:
        """Return ``True`` if ``target_id`` has at least one incoming edge of ``kind``."""
        stmt = (
            select(edges_table.c.id)
            .where(edges_table.c.kind == str(kind), edges_table.c.to_id == target_id)
            .limit(1)
        )
        with db_operation(
            f"Failed to check incoming edges for {target_id}",
            context={"target_id": target_id, "kind": str(kind)},
        ):
            return self._conn.execute(stmt).scalar_one_or_none() is not None

    def sample_low_confidence_references(
        self,
        target_ids: Sequence[str],
        threshold: float = 0.8,
        limit: int = 25,
    ) -> LowConfidenceEdgeSample:
        """Sample incoming ``references`` edges with ``confidence < threshold``."""
        unique_ids = list(dict.fromkeys(tid for tid in target_ids if tid))
        if not unique_ids or limit <= 0:
            return LowConfidenceEdgeSample()

        total_count = 0
        sampled_ids: list[str] = []
        with db_operation("Failed to sample low-confidence references"):
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

        return LowConfidenceEdgeSample(
            edge_ids=sorted(sampled_ids)[:limit],
            total_count=total_count,
        )

    def latest_indexed_at_for_symbol(self, symbol_id: str) -> str | None:
        """Return the latest ``last_indexed_at`` timestamp across edges touching ``symbol_id``."""
        stmt = (
            select(edges_table.c.last_indexed_at)
            .distinct()
            .where(
                or_(edges_table.c.from_id == symbol_id, edges_table.c.to_id == symbol_id),
                edges_table.c.last_indexed_at.is_not(None),
            )
        )
        with db_operation(
            f"Failed to read latest edge timestamp for {symbol_id}",
            context={"symbol_id": symbol_id},
        ):
            timestamps = [str(ts) for ts in self._conn.execute(stmt).scalars().all() if ts]
        return latest_timestamp(*timestamps) if timestamps else None

    def update_resolved_targets(self, updates: Sequence[EdgeTargetUpdate]) -> int:
        """Apply batch ``to_id`` and ``confidence`` updates emitted by ``SymbolLinker``."""
        if not updates:
            return 0
        stmt = (
            update(edges_table)
            .where(edges_table.c.id == bindparam("b_id"))
            .values(to_id=bindparam("b_to_id"), confidence=bindparam("b_confidence"))
        )
        updated = 0
        with db_operation(
            f"Failed to update {len(updates)} resolved edge targets",
            context={"count": len(updates)},
        ):
            for chunk in chunked_sequence(updates, _BATCH_SIZE):
                payload = [
                    {"b_id": item.edge_id, "b_to_id": item.to_id, "b_confidence": item.confidence}
                    for item in chunk
                ]
                res = self._conn.execute(stmt, payload)
                updated += max(res.rowcount or 0, 0)
            logger.debug("Updated %d resolved edge targets", updated)
        return updated

    def replace_lsp_references_for_target(
        self,
        repo_id: str,
        target_symbol_id: str,
        edges: Sequence[Edge],
    ) -> None:
        """Atomically replace all LSP ``references`` edges targeting ``target_symbol_id``."""
        with db_operation(
            f"Failed to replace LSP references for {target_symbol_id}",
            context={"repo_id": repo_id, "target_symbol_id": target_symbol_id},
        ):
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

    def _execute_delete(self, stmt: Any, context_label: str) -> int:
        """Execute a ``DELETE`` statement on ``edges_table`` and return affected row count."""
        with db_operation(f"Failed deleting edges ({context_label})"):
            res = self._conn.execute(stmt)
            deleted = max(res.rowcount or 0, 0)
            logger.debug("Deleted %d edges (%s)", deleted, context_label)
            return deleted

    def delete_owned_for_file(self, file_id: str) -> int:
        """Delete edges evidenced by or originating from symbols inside ``file_id``."""
        file_node_ids = select(nodes_table.c.id).where(nodes_table.c.file_id == file_id)
        stmt = delete(edges_table).where(
            or_(
                edges_table.c.evidence_file_id == file_id,
                edges_table.c.from_id.in_(file_node_ids),
            )
        )
        return self._execute_delete(stmt, f"owned_for_file={file_id}")

    def delete_all_for_file(self, file_id: str) -> int:
        """Delete all edges evidenced by or touching symbols inside ``file_id``."""
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
        """Delete LSP ``references`` edges evidenced by or targeting symbols in ``file_id``."""
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
        """Delete LSP ``references`` edges originating from or targeting ``symbol_ids``."""
        with db_operation("Failed deleting LSP references for symbols"):
            deleted = delete_lsp_references_for_symbol_ids(self._conn, symbol_ids)
            if deleted:
                logger.debug("Deleted %d LSP reference edges for symbol batch", deleted)
            return deleted

    def delete_dangling_lsp_references(self, repo_id: str) -> int:
        """Delete LSP ``references`` edges in ``repo_id`` whose endpoints no longer exist."""
        missing_from = ~exists(select(1).where(nodes_table.c.id == edges_table.c.from_id))
        missing_to = ~exists(select(1).where(nodes_table.c.id == edges_table.c.to_id))
        stmt = delete(edges_table).where(
            edges_table.c.repo_id == repo_id,
            edges_table.c.kind == str(EdgeKind.REFERENCES),
            or_(missing_from, missing_to),
        )
        return self._execute_delete(stmt, f"dangling_lsp_refs={repo_id}")
