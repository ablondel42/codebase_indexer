"""SQLite WAL adapter implementing ``ChatStoreProtocol`` for real-time chat persistence."""

import sqlite3
import threading
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Connection

from core.models.chat import (
    ChatHistoryPage,
    ChatPageQuery,
    ChatSessionRecord,
    ChatTimelineItem,
    ChatTimelineItemUpsert,
)
from core.utils.logging import get_logger
from storage.models.options import ConnectionOptions
from storage.services.connection import ConnectionFactory
from storage.utils.chat_schema import (
    advance_turn_cursor,
    build_new_chat_item,
    chat_item_to_row,
    chat_items_table,
    chat_metadata,
    chat_sessions_table,
    merge_chat_item,
    row_to_chat_item,
    row_to_chat_session,
    utc_now_iso,
)
from storage.utils.sql_helpers import chunked_sequence, db_operation

logger = get_logger("storage.services.chat")

_ITEM_LOOKUP_CHUNK_SIZE = 400


class SqlChatStore:
    """Thread-safe SQLite WAL store for turn-indexed ACP conversation timelines."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        """Initialize SQLite chat storage and create schema if missing."""
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._connection_factory = connection_factory or ConnectionFactory()
        self._memory_uri: str | None = None
        self._memory_anchor: sqlite3.Connection | None = None
        if str(self._db_path) == ":memory:":
            mem_uri = f"file:chat_mem_{uuid.uuid4().hex}?mode=memory&cache=shared"
            with db_operation("Failed to initialize in-memory chat SQLite database"):
                self._memory_anchor = sqlite3.connect(
                    mem_uri,
                    uri=True,
                    check_same_thread=False,
                )
                self._memory_uri = mem_uri
        self._ensure_schema()

    @property
    def db_path(self) -> Path:
        """Path to the backing SQLite chat database file (or ``':memory:'``)."""
        return self._db_path

    @contextmanager
    def _open_conn(self, *, read_only: bool = False) -> Generator[Connection, None, None]:
        """Open and automatically close one configured SQLAlchemy SQLite connection."""
        options = ConnectionOptions(
            db_path=self._db_path,
            read_only=read_only,
            must_exist=read_only and self._memory_uri is None,
            with_select_authorizer=False,
            sqlite_uri=self._memory_uri,
        )
        conn = self._connection_factory.open(options)
        try:
            yield conn
        finally:
            self._connection_factory.close(conn)

    def _ensure_schema(self) -> None:
        """Create ``chat_sessions`` and ``chat_items`` tables and indexes if absent."""
        with (
            self._lock,
            db_operation(
                "Failed initializing SQLite chat schema",
                context={"db_path": str(self._db_path)},
            ),
            self._open_conn() as conn,
        ):
            chat_metadata.create_all(conn)
            conn.commit()

    @staticmethod
    def _load_or_init_session_state(
        conn: Connection,
        session_id: str,
        *,
        reset_existing: bool,
        now_iso: str,
    ) -> tuple[bool, int, int, str | None, int]:
        """Ensure ``chat_sessions`` row exists and optionally clear prior items."""
        if reset_existing:
            conn.execute(
                delete(chat_items_table).where(chat_items_table.c.session_id == session_id)
            )
        row = (
            conn.execute(
                select(chat_sessions_table).where(chat_sessions_table.c.session_id == session_id)
            )
            .mappings()
            .first()
        )
        if row is None:
            conn.execute(
                insert(chat_sessions_table).values(
                    session_id=session_id,
                    hydrated=False,
                    last_seq=0,
                    last_turn_index=0,
                    last_turn_kind=None,
                    total_items=0,
                    updated_at=now_iso,
                )
            )
            return False, 0, 0, None, 0
        if reset_existing:
            return bool(row["hydrated"]), 0, 0, None, 0
        raw_kind = row["last_turn_kind"]
        return (
            bool(row["hydrated"]),
            int(row["last_seq"]),
            int(row["last_turn_index"]),
            str(raw_kind) if raw_kind is not None else None,
            int(row["total_items"]),
        )

    @staticmethod
    def _load_existing_items(
        conn: Connection,
        session_id: str,
        items: Sequence[ChatTimelineItemUpsert],
    ) -> dict[str, ChatTimelineItem]:
        """Pre-load existing items matching incoming ``item_id`` values in chunks."""
        unique_ids = list(dict.fromkeys(item.item_id for item in items))
        found: dict[str, ChatTimelineItem] = {}
        for batch in chunked_sequence(unique_ids, _ITEM_LOOKUP_CHUNK_SIZE):
            rows = (
                conn.execute(
                    select(chat_items_table).where(
                        chat_items_table.c.session_id == session_id,
                        chat_items_table.c.item_id.in_(batch),
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                parsed = row_to_chat_item(row)
                found[parsed.item_id] = parsed
        return found

    @staticmethod
    def _write_session_summary(
        conn: Connection,
        session_id: str,
        cursor: tuple[bool, int, int, str | None, int],
        updated_at: str,
    ) -> ChatSessionRecord:
        """Persist updated counters and hydration flag to ``chat_sessions``."""
        hydrated, last_seq, last_turn_index, last_turn_kind, total_items = cursor
        conn.execute(
            update(chat_sessions_table)
            .where(chat_sessions_table.c.session_id == session_id)
            .values(
                hydrated=hydrated,
                last_seq=last_seq,
                last_turn_index=last_turn_index,
                last_turn_kind=last_turn_kind,
                total_items=total_items,
                updated_at=updated_at,
            )
        )
        return ChatSessionRecord(
            session_id=session_id,
            hydrated=hydrated,
            last_seq=last_seq,
            last_turn_index=last_turn_index,
            total_items=total_items,
            updated_at=updated_at,
        )

    @staticmethod
    def _update_existing_item(
        conn: Connection,
        existing: ChatTimelineItem,
        delta: ChatTimelineItemUpsert,
        now_iso: str,
    ) -> ChatTimelineItem:
        """Merge ``delta`` onto ``existing`` and update its row in SQLite."""
        merged = merge_chat_item(existing, delta, now_iso)
        conn.execute(
            update(chat_items_table)
            .where(
                chat_items_table.c.session_id == existing.session_id,
                chat_items_table.c.seq == existing.seq,
            )
            .values(**chat_item_to_row(merged))
        )
        return merged

    @staticmethod
    def _insert_new_item(
        conn: Connection,
        session_id: str,
        delta: ChatTimelineItemUpsert,
        cursor: tuple[int, int, str | None, int],
        now_iso: str,
    ) -> tuple[ChatTimelineItem, tuple[int, int, str | None, int]]:
        """Insert a brand-new timeline item and advance ``cursor``."""
        next_seq = cursor[0] + 1
        next_turn, next_kind = advance_turn_cursor(cursor[1], cursor[2], delta.role)
        created = build_new_chat_item(session_id, (next_seq, next_turn), delta, now_iso)
        conn.execute(insert(chat_items_table).values(**chat_item_to_row(created)))
        return created, (next_seq, next_turn, next_kind, cursor[3] + 1)

    def _apply_one_upsert(
        self,
        conn: Connection,
        delta: ChatTimelineItemUpsert,
        existing_by_id: dict[str, ChatTimelineItem],
        cursor: tuple[int, int, str | None, int],
        session_and_time: tuple[str, str],
    ) -> tuple[ChatTimelineItem, tuple[int, int, str | None, int]]:
        """Insert or merge one timeline item and keep ``existing_by_id`` current."""
        existing = existing_by_id.get(delta.item_id)
        if existing is not None:
            merged = self._update_existing_item(conn, existing, delta, session_and_time[1])
            existing_by_id[delta.item_id] = merged
            return merged, cursor
        created, next_cursor = self._insert_new_item(
            conn,
            session_and_time[0],
            delta,
            cursor,
            session_and_time[1],
        )
        existing_by_id[delta.item_id] = created
        return created, next_cursor

    def _execute_upsert_batch(
        self,
        conn: Connection,
        session_id: str,
        items: Sequence[ChatTimelineItemUpsert],
        flags: tuple[bool, bool],
        now_iso: str,
    ) -> tuple[ChatTimelineItem, ...]:
        """Run one transactional batch of timeline upserts on an open connection."""
        reset_existing, mark_hydrated = flags
        state = self._load_or_init_session_state(
            conn,
            session_id,
            reset_existing=reset_existing,
            now_iso=now_iso,
        )
        existing_by_id = (
            {} if reset_existing else self._load_existing_items(conn, session_id, items)
        )
        touched: dict[str, ChatTimelineItem] = {}
        cursor = (state[1], state[2], state[3], state[4])
        for delta in items:
            stored, cursor = self._apply_one_upsert(
                conn,
                delta,
                existing_by_id,
                cursor,
                (session_id, now_iso),
            )
            touched[stored.item_id] = stored
        self._write_session_summary(
            conn,
            session_id,
            (state[0] or mark_hydrated, cursor[0], cursor[1], cursor[2], cursor[3]),
            now_iso,
        )
        conn.commit()
        return tuple(sorted(touched.values(), key=lambda entry: entry.seq))

    def get_session(self, session_id: str) -> ChatSessionRecord | None:
        """Return persisted metadata for ``session_id``, or ``None`` if unseen."""
        with (
            self._lock,
            db_operation(
                f"Failed reading chat session '{session_id}'",
                context={"session_id": session_id},
            ),
            self._open_conn() as conn,
        ):
            row = (
                conn.execute(
                    select(chat_sessions_table).where(
                        chat_sessions_table.c.session_id == session_id
                    )
                )
                .mappings()
                .first()
            )
            return row_to_chat_session(row) if row is not None else None

    def mark_session_hydrated(
        self,
        session_id: str,
        *,
        reset_existing: bool = False,
    ) -> ChatSessionRecord:
        """Mark ``session_id`` as hydrated, optionally clearing prior partial rows."""
        now_iso = utc_now_iso()
        with (
            self._lock,
            db_operation(
                f"Failed marking chat session '{session_id}' hydrated",
                context={"session_id": session_id},
            ),
            self._open_conn() as conn,
        ):
            state = self._load_or_init_session_state(
                conn,
                session_id,
                reset_existing=reset_existing,
                now_iso=now_iso,
            )
            record = self._write_session_summary(
                conn,
                session_id,
                (True, state[1], state[2], state[3], state[4]),
                now_iso,
            )
            conn.commit()
            return record

    def upsert_items(
        self,
        session_id: str,
        items: Sequence[ChatTimelineItemUpsert],
        *,
        reset_existing: bool = False,
        mark_hydrated: bool = False,
    ) -> tuple[ChatTimelineItem, ...]:
        """Atomically append or merge ``items`` for ``session_id`` and return stored rows."""
        if not items:
            if reset_existing or mark_hydrated:
                self.mark_session_hydrated(session_id, reset_existing=reset_existing)
            return ()

        now_iso = utc_now_iso()
        with (
            self._lock,
            db_operation(
                f"Failed upserting chat items for session '{session_id}'",
                context={"session_id": session_id, "count": len(items)},
            ),
            self._open_conn() as conn,
        ):
            return self._execute_upsert_batch(
                conn,
                session_id,
                items,
                (reset_existing, mark_hydrated),
                now_iso,
            )

    @staticmethod
    def _read_page_rows(
        conn: Connection,
        session_id: str,
        bounds: tuple[int, int],
    ) -> tuple[tuple[ChatTimelineItem, ...], bool]:
        """Fetch timeline items inside ``bounds=(lower, upper)`` and check for older rows."""
        lower_turn, upper_turn = bounds
        rows = (
            conn.execute(
                select(chat_items_table)
                .where(
                    chat_items_table.c.session_id == session_id,
                    chat_items_table.c.turn_index >= lower_turn,
                    chat_items_table.c.turn_index <= upper_turn,
                )
                .order_by(chat_items_table.c.seq.asc())
            )
            .mappings()
            .all()
        )
        older_row = conn.execute(
            select(chat_items_table.c.seq)
            .where(
                chat_items_table.c.session_id == session_id,
                chat_items_table.c.turn_index < lower_turn,
            )
            .limit(1)
        ).first()
        return tuple(row_to_chat_item(row) for row in rows), older_row is not None

    def get_history_page(self, query: ChatPageQuery) -> ChatHistoryPage:
        """Return one turn-aligned window of timeline items ordered oldest-to-newest."""
        with (
            self._lock,
            db_operation(
                f"Failed reading chat history page for '{query.session_id}'",
                context={"session_id": query.session_id},
            ),
            self._open_conn() as conn,
        ):
            session_row = (
                conn.execute(
                    select(chat_sessions_table).where(
                        chat_sessions_table.c.session_id == query.session_id
                    )
                )
                .mappings()
                .first()
            )
            if session_row is None:
                return ChatHistoryPage(session_id=query.session_id, hydrated=False)
            meta = row_to_chat_session(session_row)
            max_turn = meta.last_turn_index
            upper_turn = (
                max_turn if query.before_turn is None else min(max_turn, query.before_turn - 1)
            )
            if max_turn == 0 or upper_turn < 1:
                return ChatHistoryPage(
                    session_id=query.session_id,
                    hydrated=meta.hydrated,
                    total_turns=max_turn,
                    total_items=meta.total_items,
                )
            lower_turn = max(1, upper_turn - query.limit_turns + 1)
            items, has_more = self._read_page_rows(
                conn,
                query.session_id,
                (lower_turn, upper_turn),
            )
            return ChatHistoryPage(
                session_id=query.session_id,
                hydrated=meta.hydrated,
                items=items,
                has_more=has_more,
                next_before_turn=lower_turn if has_more else None,
                total_turns=max_turn,
                total_items=meta.total_items,
            )

    def delete_session(self, session_id: str) -> None:
        """Delete ``session_id`` and cascade-delete all stored timeline items."""
        with (
            self._lock,
            db_operation(
                f"Failed deleting chat session '{session_id}'",
                context={"session_id": session_id},
            ),
            self._open_conn() as conn,
        ):
            conn.execute(
                delete(chat_sessions_table).where(
                    chat_sessions_table.c.session_id == session_id
                )
            )
            conn.commit()

    def close(self) -> None:
        """Release any held shared-memory SQLite anchor connection."""
        with self._lock:
            anchor = self._memory_anchor
            self._memory_anchor = None
            if anchor is None:
                return
            try:
                anchor.close()
                logger.debug("Closed shared-memory chat SQLite anchor (%s)", self._memory_uri)
            except sqlite3.Error as exc:
                logger.debug("Ignoring sqlite3 error closing chat anchor: %s", exc)


__all__ = [
    "SqlChatStore",
]
