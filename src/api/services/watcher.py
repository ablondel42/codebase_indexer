"""Real-time filesystem watcher synchronizing external Antigravity logs into ``cbi_chat.db``."""

import asyncio
from pathlib import Path

from api.models.chat import (
    ChatSyncEventKind,
    ChatSyncNotificationParams,
    TranscriptParseResult,
    TranscriptSessionSyncState,
)
from api.services.gateway import AcpWebSocketGateway
from api.utils.chat import items_to_wire
from api.utils.transcript import (
    classify_profile_watch_path,
    format_chat_sync_notification,
    read_transcript_delta,
    resolve_transcript_path,
)
from core.models.chat import ChatSessionRecord, ChatTimelineItem, ChatTranscriptCursor
from core.models.exceptions import IndexerError
from core.ports.chat import ChatStoreProtocol
from core.ports.watch import DirectoryWatchHandleProtocol, WatchEventKind
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine

logger = get_logger("api.services.watcher")

_BULK_RESET_THRESHOLD = 50


class AgyProfileSyncWatcher:
    """Watches ``<profile_root>`` for external updates and pushes ``cbi/chat_sync`` frames."""

    def __init__(
        self,
        profile_root: Path,
        chat_store: ChatStoreProtocol,
        gateway: AcpWebSocketGateway,
        engine: IndexerEngine,
        *,
        enabled: bool = True,
    ) -> None:
        """Initialize the profile sync watcher without starting filesystem observers."""
        self._profile_root = profile_root
        self._store = chat_store
        self._gateway = gateway
        self._engine = engine
        self._enabled = enabled
        self._transcript_debounce_seconds = 0.08
        self._sessions_debounce_seconds = 0.3
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watch_handle: DirectoryWatchHandleProtocol | None = None
        self._stopping = False
        self._debounce_timers: dict[tuple[str, str | None], asyncio.TimerHandle] = {}
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._sync_states: dict[str, TranscriptSessionSyncState] = {}
        self._deferred_sessions: set[str] = set()
        self._known_conversation_ids: set[str] = set()

    def _scan_conversation_ids(self) -> set[str]:
        """Return stems of non-symlink ``*.db`` files under ``conversations/``."""
        conv_dir = self._profile_root / "conversations"
        try:
            if not conv_dir.is_dir():
                return set()
            return {entry.stem for entry in conv_dir.glob("*.db") if not entry.is_symlink()}
        except OSError as exc:
            logger.debug("Skipping conversation directory scan in %s: %s", conv_dir, exc)
            return set(self._known_conversation_ids)

    def set_debounce_windows(
        self,
        *,
        transcript_seconds: float = 0.08,
        sessions_seconds: float = 0.3,
    ) -> None:
        """Configure debounce delays used to coalesce rapid disk writes."""
        self._transcript_debounce_seconds = max(0.01, transcript_seconds)
        self._sessions_debounce_seconds = max(0.01, sessions_seconds)

    def start(self) -> None:
        """Start watching ``profile_root``, ``brain/``, and ``conversations/``."""
        if not self._enabled or self._watch_handle is not None:
            return
        if not self._profile_root.is_dir():
            logger.debug("Skipping profile watch; directory absent: %s", self._profile_root)
            return
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        self._known_conversation_ids = self._scan_conversation_ids()
        brain_dir = self._profile_root / "brain"
        conv_dir = self._profile_root / "conversations"
        watches: list[tuple[Path, bool]] = [
            (brain_dir, True),
            (conv_dir, False),
        ]
        if not brain_dir.is_dir() or not conv_dir.is_dir():
            watches.insert(0, (self._profile_root, False))
        try:
            self._watch_handle = self._engine.watch_directories(watches, self._on_fs_event)
            logger.info(
                "Watching Antigravity profile directory for live sync: %s",
                self._profile_root,
            )
        except (IndexerError, OSError, RuntimeError) as exc:
            logger.warning("Failed starting Antigravity profile watcher: %s", exc)
            self._watch_handle = None

    async def stop(self) -> None:
        """Stop filesystem observation, cancel pending timers, and await active sync tasks."""
        self._stopping = True
        for timer in list(self._debounce_timers.values()):
            timer.cancel()
        self._debounce_timers.clear()

        handle = self._watch_handle
        self._watch_handle = None
        if handle is not None:
            await asyncio.to_thread(handle.stop)

        tasks = [task for task in self._active_tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()
        self._loop = None

    def _on_fs_event(
        self,
        kind: WatchEventKind,
        changed_path: Path,
        is_directory: bool,
    ) -> None:
        """Bridge raw ``watchdog`` callbacks onto the running FastAPI event loop."""
        if self._stopping:
            return
        category, target_id = classify_profile_watch_path(
            self._profile_root,
            changed_path,
            is_directory=is_directory,
        )
        if category == "ignore":
            return
        if category == "watch_subdir":
            if kind in ("created", "moved") and self._watch_handle is not None:
                self._watch_handle.add_watch(changed_path, recursive=(target_id == "brain"))
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._schedule_debounced, category, target_id)
        except RuntimeError as exc:
            logger.debug("Skipping watcher event on closed event loop: %s", exc)

    def _schedule_debounced(self, category: str, target_id: str | None) -> None:
        """Schedule or reset the debounce timer for ``(category, target_id)``."""
        loop = self._loop
        if self._stopping or loop is None or loop.is_closed():
            return
        key = (category, target_id)
        existing = self._debounce_timers.pop(key, None)
        if existing is not None:
            existing.cancel()
        delay = (
            self._sessions_debounce_seconds
            if category == "sessions"
            else self._transcript_debounce_seconds
        )
        self._debounce_timers[key] = loop.call_later(
            delay,
            self._launch_debounced_task,
            key,
        )

    def _launch_debounced_task(self, key: tuple[str, str | None]) -> None:
        """Spawn the async sync task once a key's debounce timer fires."""
        self._debounce_timers.pop(key, None)
        if self._stopping:
            return
        category, target_id = key
        task = asyncio.create_task(self._execute_sync_event(category, target_id))
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    async def _execute_sync_event(self, category: str, target_id: str | None) -> None:
        """Execute one debounced transcript, artifact, or sessions synchronization."""
        try:
            if category == "transcript" and target_id is not None:
                await self.sync_session_now(
                    target_id,
                    broadcast=True,
                    defer_if_prompt_active=True,
                )
            elif category == "artifact" and target_id is not None:
                await self._broadcast_event("artifacts_changed", session_id=target_id)
            elif category == "sessions":
                current_ids = await asyncio.to_thread(self._scan_conversation_ids)
                if current_ids != self._known_conversation_ids:
                    self._known_conversation_ids = current_ids
                    await self._broadcast_event("sessions_changed", session_id=None)
        except (IndexerError, OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Profile sync task failed for (%s, %s): %s",
                category,
                target_id,
                exc,
            )

    async def _broadcast_event(
        self,
        event: ChatSyncEventKind,
        *,
        session_id: str | None,
    ) -> None:
        """Send a lightweight ``cbi/chat_sync`` metadata invalidation notification."""
        frame = format_chat_sync_notification(
            ChatSyncNotificationParams(sessionId=session_id, event=event)
        )
        await self._gateway.broadcast_notification(frame)

    async def sync_session_now(
        self,
        session_id: str,
        *,
        broadcast: bool,
        defer_if_prompt_active: bool = True,
    ) -> bool:
        """Catch up ``session_id`` from ``transcript_full.jsonl`` into ``cbi_chat.db``."""
        if not self._enabled:
            return False
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await self._sync_session_locked(
                session_id,
                broadcast=broadcast,
                defer_if_prompt_active=defer_if_prompt_active,
            )

    async def flush_deferred_session(self, session_id: str) -> None:
        """Advance deferred transcript cursor after a local ``session/prompt`` completes."""
        if not self._enabled or session_id not in self._deferred_sessions:
            return
        self._deferred_sessions.discard(session_id)
        try:
            await self.sync_session_now(
                session_id,
                broadcast=False,
                defer_if_prompt_active=False,
            )
        except (IndexerError, OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Failed catching up deferred transcript cursor for '%s': %s",
                session_id,
                exc,
            )

    def _update_cached_state(self, session_id: str, parsed: TranscriptParseResult) -> None:
        """Update in-memory pending tool batch, background tasks, and turn timestamp caches."""
        if (
            parsed.pending_batch is None
            and not parsed.pending_tasks
            and parsed.turn_started_at is None
            and not parsed.turn_active
        ):
            self._sync_states.pop(session_id, None)
            return
        self._sync_states[session_id] = TranscriptSessionSyncState(
            pending_batch=parsed.pending_batch,
            pending_tasks=parsed.pending_tasks,
            turn_started_at=parsed.turn_started_at,
            turn_active=parsed.turn_active,
        )

    async def _parse_with_reset_fallback(
        self,
        session_id: str,
        transcript_path: Path,
        *,
        full_reset: bool,
        start_offset: int,
        last_step: int | None,
    ) -> tuple[bool, TranscriptParseResult]:
        """Parse transcript delta, automatically falling back to offset 0 on truncation."""
        cached = None if full_reset else self._sync_states.get(session_id)
        parsed = await asyncio.to_thread(
            read_transcript_delta,
            transcript_path,
            start_offset=start_offset,
            last_step_index=last_step,
            pending_batch=cached.pending_batch if cached is not None else None,
            pending_tasks=cached.pending_tasks if cached is not None else (),
            turn_started_at=cached.turn_started_at if cached is not None else None,
        )
        if not full_reset and parsed.needs_reset:
            full_reset = True
            parsed = await asyncio.to_thread(
                read_transcript_delta,
                transcript_path,
                start_offset=0,
                last_step_index=None,
            )
        return full_reset, parsed

    @staticmethod
    def _has_nonempty_transcript(transcript_path: Path) -> bool:
        """Return ``True`` when ``transcript_path`` exists and is non-empty."""
        try:
            return transcript_path.stat().st_size > 0
        except OSError as exc:
            logger.debug("Transcript stat failed for %s: %s", transcript_path, exc)
            return False

    @staticmethod
    def _needs_initial_reset(record: ChatSessionRecord | None, has_file_bytes: bool) -> bool:
        """Return ``True`` when persisted state is missing, unparsed, or self-heal-eligible."""
        if record is None or record.transcript_step_index is None:
            return True
        return (
            record.total_items == 0
            and record.transcript_step_index >= 0
            and has_file_bytes
        )

    async def _persist_transcript_sync(
        self,
        session_id: str,
        transcript_path: Path,
    ) -> tuple[tuple[ChatTimelineItem, ...], bool, TranscriptParseResult | None, bool]:
        """Parse delta from ``transcript_path`` and atomically persist items + cursor."""
        record = await asyncio.to_thread(self._store.get_session, session_id)
        had_prior_items = record is not None and record.total_items > 0
        has_file_bytes = self._has_nonempty_transcript(transcript_path)
        initial_reset = self._needs_initial_reset(record, has_file_bytes)
        start_offset = 0 if initial_reset or record is None else record.transcript_offset
        last_step = None if initial_reset or record is None else record.transcript_step_index
        full_reset, parsed = await self._parse_with_reset_fallback(
            session_id,
            transcript_path,
            full_reset=initial_reset,
            start_offset=start_offset,
            last_step=last_step,
        )
        if full_reset and not parsed.upserts and had_prior_items and has_file_bytes:
            logger.warning(
                "Refusing to wipe %d stored items for '%s' on empty parse of non-empty transcript",
                record.total_items if record is not None else 0,
                session_id,
            )
            return (), False, None, False

        self._update_cached_state(session_id, parsed)
        cursor = ChatTranscriptCursor(
            step_index=parsed.last_step_index,
            byte_offset=parsed.next_offset,
        )
        unchanged = (
            not full_reset
            and not parsed.upserts
            and record is not None
            and record.transcript_offset == cursor.byte_offset
            and record.transcript_step_index == cursor.step_index
        )
        if unchanged:
            return (), False, None, False

        stored = await asyncio.to_thread(
            self._store.upsert_items,
            session_id,
            parsed.upserts,
            reset_existing=full_reset,
            mark_hydrated=True,
            transcript_cursor=cursor,
        )
        return (
            stored,
            full_reset,
            parsed,
            (full_reset and had_prior_items) or len(stored) > _BULK_RESET_THRESHOLD,
        )

    async def _sync_session_locked(
        self,
        session_id: str,
        *,
        broadcast: bool,
        defer_if_prompt_active: bool,
    ) -> bool:
        """Synchronize ``session_id`` under its per-session lock."""
        transcript_path = resolve_transcript_path(self._profile_root, session_id)
        if transcript_path is None:
            return False
        if defer_if_prompt_active and self._gateway.has_active_prompt(session_id):
            self._deferred_sessions.add(session_id)
            return False

        self._deferred_sessions.discard(session_id)
        prev_cached = self._sync_states.get(session_id)
        was_turn_active = prev_cached.turn_active if prev_cached is not None else False
        stored, reset_performed, parsed, emit_reset = await self._persist_transcript_sync(
            session_id,
            transcript_path,
        )
        if parsed is None:
            return False

        should_emit = broadcast and (
            bool(stored)
            or reset_performed
            or (parsed.turn_active and parsed.turn_started_at is not None)
            or (was_turn_active and not parsed.turn_active)
        )
        if should_emit:
            summary = await asyncio.to_thread(self._store.get_session, session_id)
            params = ChatSyncNotificationParams(
                sessionId=session_id,
                event="timeline_reset" if emit_reset else "items_upserted",
                items=() if emit_reset else items_to_wire(stored),
                totalTurns=summary.last_turn_index if summary is not None else None,
                totalItems=summary.total_items if summary is not None else None,
                turnActive=parsed.turn_active,
                turnStartedAt=parsed.turn_started_at,
            )
            await self._gateway.broadcast_notification(format_chat_sync_notification(params))
            if session_id not in self._known_conversation_ids:
                self._known_conversation_ids.add(session_id)
                await self._broadcast_event("sessions_changed", session_id=None)
        return bool(stored or reset_performed)

    def clear_cached_state(self, session_id: str) -> None:
        """Drop in-memory batch and turn state after deletion of ``session_id``."""
        self._sync_states.pop(session_id, None)
        self._deferred_sessions.discard(session_id)


__all__ = [
    "AgyProfileSyncWatcher",
]
