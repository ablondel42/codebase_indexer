"""SQLAlchemy Core adapter implementing ``ReferenceRefreshRepositoryProtocol``."""

import sqlite3
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, literal, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from core.models.enums import EdgeKind, RefreshStatus
from core.models.exceptions import DatabaseError
from core.models.runs import ReferenceRefreshState
from core.utils.logging import get_logger
from storage.utils.mappers import row_to_refresh_state
from storage.utils.schema import edges_table, nodes_table, reference_refresh_table
from storage.utils.sql_helpers import chunked_sequence, db_error_message

logger = get_logger("storage.services.refresh")

_CHUNK_SIZE = 400


class SqlReferenceRefreshRepository:
    """SQLite repository adapter for per-symbol ``ReferenceRefreshState`` records."""

    def __init__(self, conn: Connection) -> None:
        """Initialize the repository over an active SQLAlchemy ``Connection``.

        Args:
            conn: Active SQLAlchemy ``Connection``.
        """
        self._conn = conn

    def _upsert_stale_from_node_select(self, node_filter: Any) -> None:
        """Upsert ``stale`` refresh entries for all nodes matching ``node_filter``.

        Args:
            node_filter: SQLAlchemy boolean expression on ``nodes_table``.
        """
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
        """Insert or update the LSP reference refresh state for a target symbol.

        Args:
            state: Refresh state record to persist.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        stmt = sqlite_insert(reference_refresh_table).values(
            target_symbol_id=state.target_symbol_id,
            available=bool(state.available),
            last_refreshed_at=state.last_refreshed_at,
            refresh_status=state.refresh_status,
            error_code=state.error_code,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[reference_refresh_table.c.target_symbol_id],
            set_={
                "available": stmt.excluded.available,
                "last_refreshed_at": stmt.excluded.last_refreshed_at,
                "refresh_status": stmt.excluded.refresh_status,
                "error_code": stmt.excluded.error_code,
            },
        )
        try:
            self._conn.execute(stmt)
            logger.debug(
                "Upserted reference refresh state for %s (available=%s)",
                state.target_symbol_id,
                state.available,
            )
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed upserting refresh state for %s: %s", state.target_symbol_id, msg)
            raise DatabaseError(
                f"Failed to upsert refresh state for {state.target_symbol_id}: {msg}",
                context={"target_symbol_id": state.target_symbol_id},
            ) from exc

    def get(self, target_symbol_id: str) -> ReferenceRefreshState:
        """Fetch refresh state for ``target_symbol_id`` (returning default if unrecorded).

        Args:
            target_symbol_id: Target symbol identifier.

        Returns:
            Stored or default ``ReferenceRefreshState``.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
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
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed reading refresh state for %s: %s", target_symbol_id, msg)
            raise DatabaseError(
                f"Failed to read refresh state for {target_symbol_id}: {msg}",
                context={"target_symbol_id": target_symbol_id},
            ) from exc

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
        """Mark existing refresh records for ``symbol_ids`` as stale/unavailable.

        Args:
            symbol_ids: Candidate symbol identifiers impacted by edits.

        Returns:
            Sorted list of existing symbol IDs whose refresh records were invalidated.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        unique_ids = sorted({sid for sid in symbol_ids if sid})
        if not unique_ids:
            return []

        marked: list[str] = []
        try:
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
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed marking %d symbols stale: %s", len(unique_ids), msg)
            raise DatabaseError(f"Failed to mark symbols stale: {msg}") from exc

    def mark_file_symbols_stale(self, file_id: str) -> list[str]:
        """Mark refresh records for symbols declared in or impacted by ``file_id`` as stale.

        Args:
            file_id: Modified or deleted file identifier.

        Returns:
            Sorted list of invalidated target symbol IDs.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        impacted_ids = self.collect_impacted_symbol_ids(file_id)
        if not impacted_ids:
            return []
        return self.mark_symbols_stale(impacted_ids)

    def invalidate_for_file(self, file_id: str) -> int:
        """Invalidate refresh entries for ``file_id`` and delete its ``references`` edges.

        Args:
            file_id: Deleted or re-extracted file identifier.

        Returns:
            Number of invalidated/deleted ``references`` edge rows evidenced by ``file_id``.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        referenced_targets = select(edges_table.c.to_id).where(
            edges_table.c.kind == str(EdgeKind.REFERENCES),
            edges_table.c.evidence_file_id == file_id,
        )
        try:
            self._upsert_stale_from_node_select(
                or_(
                    nodes_table.c.file_id == file_id,
                    nodes_table.c.id.in_(referenced_targets),
                )
            )
            res = self._conn.execute(
                delete(edges_table).where(
                    edges_table.c.kind == str(EdgeKind.REFERENCES),
                    edges_table.c.evidence_file_id == file_id,
                )
            )
            deleted = max(res.rowcount or 0, 0)
            logger.debug(
                "Invalidated refresh state and removed %d reference edges for file %s",
                deleted,
                file_id,
            )
            return deleted
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed invalidating refresh state for file %s: %s", file_id, msg)
            raise DatabaseError(
                f"Failed to invalidate refresh state for {file_id}: {msg}",
                context={"file_id": file_id},
            ) from exc

    def collect_impacted_symbol_ids(self, file_id: str) -> list[str]:
        """Collect target symbol IDs declared in or referenced from ``file_id``.

        Args:
            file_id: File identifier being updated or removed.

        Returns:
            Sorted deduplicated list of impacted target symbol IDs.

        Raises:
            DatabaseError: If SQLite execution fails.
        """
        try:
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
        except (SQLAlchemyError, sqlite3.Error) as exc:
            msg = db_error_message(exc)
            logger.error("Failed collecting impacted symbols for file %s: %s", file_id, msg)
            raise DatabaseError(
                f"Failed to collect impacted symbols for {file_id}: {msg}",
                context={"file_id": file_id},
            ) from exc
        return sorted(declared_ids | referenced_ids)
