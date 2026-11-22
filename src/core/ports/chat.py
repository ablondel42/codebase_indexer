"""Port contract for persistent, turn-paginated ACP conversation history storage."""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from core.models.chat import (
    ChatHistoryPage,
    ChatPageQuery,
    ChatSessionRecord,
    ChatTimelineItem,
    ChatTimelineItemUpsert,
)


@runtime_checkable
class ChatStoreProtocol(Protocol):
    """Driven persistence port for real-time ACP conversation timelines."""

    @property
    def db_path(self) -> Path:
        """Path to the backing SQLite chat database file (or ``':memory:'``)."""
        ...

    def get_session(self, session_id: str) -> ChatSessionRecord | None:
        """Return persisted metadata for ``session_id``, or ``None`` if unseen."""
        ...

    def mark_session_hydrated(
        self,
        session_id: str,
        *,
        reset_existing: bool = False,
    ) -> ChatSessionRecord:
        """Mark ``session_id`` as hydrated, optionally clearing prior partial rows."""
        ...

    def upsert_items(
        self,
        session_id: str,
        items: Sequence[ChatTimelineItemUpsert],
        *,
        reset_existing: bool = False,
        mark_hydrated: bool = False,
    ) -> tuple[ChatTimelineItem, ...]:
        """Atomically append or merge ``items`` for ``session_id`` and return stored rows."""
        ...

    def get_history_page(self, query: ChatPageQuery) -> ChatHistoryPage:
        """Return one turn-aligned window of timeline items ordered oldest-to-newest."""
        ...

    def delete_session(self, session_id: str) -> None:
        """Delete ``session_id`` and cascade-delete all stored timeline items."""
        ...

    def close(self) -> None:
        """Release any held backing database resources."""
        ...


__all__ = [
    "ChatStoreProtocol",
]
