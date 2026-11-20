"""SQLAlchemy Core adapter implementing ``ReferenceRefreshRepositoryProtocol``."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, literal, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from core.models.enums import EdgeKind, RefreshStatus
from core.models.runs import ReferenceRefreshState
from core.utils.logging import get_logger
from storage.utils.mappers import row_to_refresh_state
from storage.utils.schema import edges_table, nodes_table, reference_refresh_table
from storage.utils.sql_helpers import build_sqlite_upsert, chunked_sequence, db_operation

logger = get_logger("storage.services.refresh")

_CHUNK_SIZE = 400


class SqlReferenceRefreshRepository:
    """SQLite repository adapter for per-symbol ``ReferenceRefreshState`` records."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``."""
        self._conn = conn

    def _upsert_stale_from_node_select(self, node_filter: Any) -> None:
        """Upsert ``stale`` refresh entries for all nodes matching ``node_filter``."""
        source_select = select(
            nodes_table.c.id,
            literal(False),
            literal(None),
            literal(str(RefreshStatus.STALE)),
            literal(None),
        ).where(node_filter)

        ins = sqlite_insert(reference_refresh_table).from_select(
            [
                "target_symbol_id",
                "available",
                "last_refreshed_at",
                "refresh_status",
                "error_code",
            ],
            source_select,
        )
        stmt = ins.on_conflict_do_update(
            index_elements=[reference_refresh_table.c.target_symbol_id],
            set_={
                "available": False,
                "last_refreshed_at": None,
                "refresh_status": str(RefreshStatus.STALE),
                "error_code": None,
            },
        )
        self._conn.execute(stmt)

    def upsert(self, state: ReferenceRefreshState) -> None:
        """Insert or update the LSP reference refresh state for a target symbol."""
        stmt = build_sqlite_upsert(
            reference_refresh_table,
            conflict_col="target_symbol_id",
            exclude_cols=("target_symbol_id",),
        )
        payload = {
            "target_symbol_id": state.target_symbol_id,
            "available": bool(state.available),
            "last_refreshed_at": state.last_refreshed_at,
            "refresh_status": state.refresh_status,
            "error_code": state.error_code,
        }
        with db_operation(
            f"Failed to upsert refresh state for {state.target_symbol_id}",
            context={"target_symbol_id": state.target_symbol_id},
        ):
            self._conn.execute(stmt, payload)
            logger.debug(
                "Upserted reference refresh state for %s (available=%s)",
                state.target_symbol_id,
                state.available,
            )

    def get(self, target_symbol_id: str) -> ReferenceRefreshState:
        """Fetch refresh state for ``target_symbol_id`` (returning default if unrecorded)."""
        with db_operation(
            f"Failed to read refresh state for {target_symbol_id}",
            context={"target_symbol_id": target_symbol_id},
        ):
            row = (
                self._conn
                .execute(
                    select(reference_refresh_table).where(
                        reference_refresh_table.c.target_symbol_id == target_symbol_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return ReferenceRefreshState(
                target_symbol_id=target_symbol_id,
                available=False,
                last_refreshed_at=None,
                refresh_status=None,
                error_code=None,
            )
        return row_to_refresh_state(row)

    def mark_symbols_stale(self, symbol_ids: Sequence[str]) -> list[str]:
        """Mark existing refresh records for ``symbol_ids`` as stale/unavailable."""
        unique_ids = sorted({sid for sid in symbol_ids if sid})
        if not unique_ids:
            return []

        marked: list[str] = []
        with db_operation("Failed to mark symbols stale"):
            for chunk in chunked_sequence(unique_ids, _CHUNK_SIZE):
                existing_ids = [
                    str(nid)
                    for nid in self._conn
                    .execute(
                        select(nodes_table.c.id)
                        .where(nodes_table.c.id.in_(chunk))
                        .order_by(nodes_table.c.id)
                    )
                    .scalars()
                    .all()
                ]
                if not existing_ids:
                    continue
                self._upsert_stale_from_node_select(nodes_table.c.id.in_(chunk))
                marked.extend(existing_ids)
            logger.debug("Marked %d symbols stale in reference_refresh", len(marked))
        return marked

    def mark_file_symbols_stale(self, file_id: str) -> list[str]:
        """Mark refresh records for symbols declared in or impacted by ``file_id`` as stale."""
        impacted_ids = self.collect_impacted_symbol_ids(file_id)
        return self.mark_symbols_stale(impacted_ids) if impacted_ids else []

    def invalidate_for_file(self, file_id: str) -> int:
        """Invalidate refresh entries for ``file_id`` and delete its ``references`` edges."""
        file_ref_cond = (
            edges_table.c.kind == str(EdgeKind.REFERENCES),
            edges_table.c.evidence_file_id == file_id,
        )
        referenced_targets = select(edges_table.c.to_id).where(*file_ref_cond)
        with db_operation(
            f"Failed to invalidate refresh state for {file_id}",
            context={"file_id": file_id},
        ):
            self._upsert_stale_from_node_select(
                or_(
                    nodes_table.c.file_id == file_id,
                    nodes_table.c.id.in_(referenced_targets),
                )
            )
            res = self._conn.execute(delete(edges_table).where(*file_ref_cond))
            deleted = max(res.rowcount or 0, 0)
            logger.debug(
                "Invalidated refresh state and removed %d reference edges for file %s",
                deleted,
                file_id,
            )
            return deleted

    def collect_impacted_symbol_ids(self, file_id: str) -> list[str]:
        """Collect target symbol IDs declared in or referenced from ``file_id``."""
        with db_operation(
            f"Failed to collect impacted symbols for {file_id}",
            context={"file_id": file_id},
        ):
            declared_ids = {
                str(nid)
                for nid in self._conn
                .execute(select(nodes_table.c.id).where(nodes_table.c.file_id == file_id))
                .scalars()
                .all()
            }
            referenced_ids = {
                str(tid)
                for tid in self._conn
                .execute(
                    select(edges_table.c.to_id)
                    .distinct()
                    .where(
                        edges_table.c.kind == str(EdgeKind.REFERENCES),
                        edges_table.c.evidence_file_id == file_id,
                    )
                )
                .scalars()
                .all()
            }
        return sorted(declared_ids | referenced_ids)
