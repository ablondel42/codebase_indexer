"""Real-time chat recorder and silent hydration coordinator for ACP connections."""

import asyncio
import time
import uuid
from collections.abc import Mapping
from typing import Any

from api.models.acp import AgentCapabilities
from api.utils.chat import (
    TIMELINE_UPDATE_KINDS,
    attach_chat_meta_to_response,
    build_update_upsert,
    extract_prompt_user_upsert,
)
from core.models.chat import ChatItemRole, ChatTimelineItemUpsert
from core.models.exceptions import IndexerError
from core.ports.chat import ChatStoreProtocol
from core.utils.logging import get_logger

logger = get_logger("api.services.chat")


class SessionChatRecorder:
    """Buffers rapid streaming chunks and persists timeline updates in real time."""

    def __init__(
        self,
        connection_id: str,
        store: ChatStoreProtocol | None,
        *,
        flush_interval_seconds: float = 0.2,
    ) -> None:
        """Initialize per-connection real-time buffers and silent hydration state."""
        self._connection_id = connection_id
        self._store = store
        self._flush_interval_seconds = flush_interval_seconds
        self._io_lock = asyncio.Lock()
        self._pending_setups: dict[int | str, tuple[str, bool, bool]] = {}
        self._metadata_only_reqs: set[int | str] = set()
        self._suppressed_sessions: dict[str, bool] = {}
        self._hydration_buffers: dict[str, list[ChatTimelineItemUpsert]] = {}
        self._live_buffers: dict[str, list[ChatTimelineItemUpsert]] = {}
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._auto_stream_state: dict[str, tuple[str, str]] = {}
        self._active_plan_ids: dict[str, str] = {}
        self._pending_user_echo: dict[str, str] = {}
        self._item_started_at: dict[tuple[str, str], float] = {}
        self._active_item_by_session: dict[str, tuple[str, ChatItemRole]] = {}

    def resolve_setup_routing(
        self,
        method: str,
        session_id: str,
        caps: AgentCapabilities,
        *,
        hydrate_only: bool,
    ) -> tuple[str, bool, bool]:
        """Choose ``(outbound_method, suppress_replay, hydrate_reset)`` for setup RPCs."""
        if not hydrate_only or self._store is None:
            return method, False, False

        can_load = bool(caps.load_session or caps.session_capabilities.load is not None)
        can_resume = caps.session_capabilities.resume is not None
        record = self._store.get_session(session_id)
        if record is not None and record.hydrated:
            if can_resume:
                return "session/resume", False, False
            return "session/load", True, False
        if can_load:
            return "session/load", True, True
        return "session/resume", False, True

    def register_pending_setup(
        self,
        req_id: int | str,
        session_id: str,
        *,
        suppress_replay: bool,
        hydrate_reset: bool,
    ) -> None:
        """Track an in-flight ``hydrateOnly`` setup request until its response arrives."""
        self._pending_setups[req_id] = (session_id, suppress_replay, hydrate_reset)
        if suppress_replay:
            self._suppressed_sessions[session_id] = hydrate_reset
        if hydrate_reset:
            self._hydration_buffers[session_id] = []
            self._auto_stream_state.pop(session_id, None)
            self._active_plan_ids.pop(session_id, None)

    def register_metadata_only_load(self, req_id: int | str, session_id: str) -> None:
        """Suppress timeline chunks on a metadata-only ``session/load`` without touching storage."""
        self._metadata_only_reqs.add(req_id)
        self._pending_setups[req_id] = (session_id, True, False)
        self._suppressed_sessions[session_id] = False

    async def record_prompt_sent(
        self,
        session_id: str,
        req_id: int | str,
        params: Mapping[str, Any],
    ) -> None:
        """Persist outbound ``session/prompt`` user text immediately."""
        if self._store is None:
            return
        self._auto_stream_state.pop(session_id, None)
        self._active_plan_ids.pop(session_id, None)
        upsert = extract_prompt_user_upsert(req_id, params)
        if upsert is None:
            return
        self._pending_user_echo[session_id] = upsert.text
        self._enqueue_item(self._live_buffers.setdefault(session_id, []), upsert)
        await self.flush_session(session_id)

    def _allocate_auto_item_id(self, session_id: str, update_kind: str) -> str:
        """Return stable coalesced IDs for un-ID'd message chunks and per-turn plans."""
        if update_kind == "plan":
            existing_plan_id = self._active_plan_ids.get(session_id)
            if existing_plan_id is None:
                existing_plan_id = f"plan_{uuid.uuid4().hex[:12]}"
                self._active_plan_ids[session_id] = existing_plan_id
            return existing_plan_id

        if update_kind in ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk"):
            current = self._auto_stream_state.get(session_id)
            if current is not None and current[0] == update_kind:
                return current[1]
            fresh_id = f"auto_{uuid.uuid4().hex[:12]}"
            self._auto_stream_state[session_id] = (update_kind, fresh_id)
            return fresh_id

        self._auto_stream_state.pop(session_id, None)
        return f"auto_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _enqueue_item(
        buffer: list[ChatTimelineItemUpsert],
        upsert: ChatTimelineItemUpsert,
    ) -> bool:
        """Coalesce consecutive ``append_text`` chunks on matching buffer tail; return boundary."""
        if buffer and buffer[-1].item_id == upsert.item_id:
            tail = buffer[-1]
            if upsert.merge_mode == "append_text":
                buffer[-1] = tail.model_copy(update={"text": f"{tail.text}{upsert.text}"})
                return False
            buffer[-1] = tail.model_copy(
                update={
                    "text": upsert.text if upsert.text else tail.text,
                    "duration_sec": (
                        upsert.duration_sec
                        if upsert.duration_sec is not None
                        else tail.duration_sec
                    ),
                    "tool_kind": (
                        upsert.tool_kind if upsert.tool_kind is not None else tail.tool_kind
                    ),
                    "tool_title": (
                        upsert.tool_title if upsert.tool_title is not None else tail.tool_title
                    ),
                    "tool_status": (
                        upsert.tool_status if upsert.tool_status is not None else tail.tool_status
                    ),
                    "exit_code": (
                        upsert.exit_code if upsert.exit_code is not None else tail.exit_code
                    ),
                    "locations": (
                        upsert.locations
                        if upsert.locations is not None and len(upsert.locations) > 0
                        else tail.locations
                    ),
                    "plan_title": (
                        upsert.plan_title if upsert.plan_title is not None else tail.plan_title
                    ),
                    "plan_steps": (
                        upsert.plan_steps if upsert.plan_steps is not None else tail.plan_steps
                    ),
                }
            )
            return True

        role_switched = bool(buffer) and buffer[-1].role != upsert.role
        buffer.append(upsert)
        return role_switched or upsert.merge_mode != "append_text"

    def _schedule_flush(self, session_id: str, *, immediate: bool) -> None:
        """Schedule debounced (200ms) or immediate background persistence for ``session_id``."""
        existing = self._flush_tasks.get(session_id)
        if existing is not None and not existing.done():
            if not immediate:
                return
            existing.cancel()
        delay = 0.0 if immediate else self._flush_interval_seconds
        self._flush_tasks[session_id] = asyncio.create_task(
            self._delayed_flush(session_id, delay)
        )

    async def _delayed_flush(self, session_id: str, delay: float) -> None:
        """Sleep for debounce window and flush ``session_id`` buffer cleanly."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self.flush_session(session_id)
        except asyncio.CancelledError:
            raise
        except (IndexerError, OSError, RuntimeError) as exc:
            logger.warning(
                "[%s] Failed background chat flush for session '%s': %s",
                self._connection_id,
                session_id,
                exc,
            )

    @staticmethod
    def _rounded_elapsed(start_mono: float, now_mono: float) -> float:
        """Compute rounded positive elapsed seconds down to tenths."""
        delta = max(0.0, now_mono - start_mono)
        return max(0.1, round(delta, 1)) if delta >= 0.05 else 0.1

    def _stamp_previous_item_duration(self, session_id: str, now_mono: float) -> None:
        """Attach elapsed duration onto the preceding open live item before phase switch."""
        prev = self._active_item_by_session.pop(session_id, None)
        if prev is None:
            return
        prev_id, prev_role = prev
        started = self._item_started_at.pop((session_id, prev_id), None)
        if started is None:
            return
        stamp = ChatTimelineItemUpsert(
            item_id=prev_id,
            role=prev_role,
            text="",
            duration_sec=self._rounded_elapsed(started, now_mono),
            merge_mode="replace",
        )
        self._enqueue_item(self._live_buffers.setdefault(session_id, []), stamp)

    def _track_live_duration(
        self,
        session_id: str,
        upsert: ChatTimelineItemUpsert,
    ) -> ChatTimelineItemUpsert:
        """Record monotonic start times and emit ``duration_sec`` on item completion."""
        now_mono = time.monotonic()
        if upsert.role == ChatItemRole.TOOL:
            self._stamp_previous_item_duration(session_id, now_mono)
            key = (session_id, upsert.item_id)
            started = self._item_started_at.setdefault(key, now_mono)
            if upsert.tool_status in ("done", "error", "denied"):
                self._item_started_at.pop(key, None)
                return upsert.model_copy(
                    update={"duration_sec": self._rounded_elapsed(started, now_mono)}
                )
            return upsert

        if upsert.role in (ChatItemRole.THOUGHT, ChatItemRole.AGENT):
            current = self._active_item_by_session.get(session_id)
            if current is not None and current[0] != upsert.item_id:
                self._stamp_previous_item_duration(session_id, now_mono)
            self._item_started_at.setdefault((session_id, upsert.item_id), now_mono)
            self._active_item_by_session[session_id] = (upsert.item_id, upsert.role)
        else:
            self._stamp_previous_item_duration(session_id, now_mono)
        return upsert

    def _apply_live_upsert(
        self,
        session_id: str,
        upsert: ChatTimelineItemUpsert,
    ) -> None:
        """Deduplicate echoed live user prompts, coalesce into buffer, and schedule flush."""
        if upsert.role == ChatItemRole.USER:
            pending_echo = self._pending_user_echo.pop(session_id, None)
            self._active_plan_ids.pop(session_id, None)
            if pending_echo is not None and upsert.text == pending_echo:
                return
        else:
            self._pending_user_echo.pop(session_id, None)

        timed_upsert = self._track_live_duration(session_id, upsert)
        immediate = self._enqueue_item(self._live_buffers.setdefault(session_id, []), timed_upsert)
        self._schedule_flush(session_id, immediate=immediate)

    def observe_session_update(self, session_id: str, update: Mapping[str, Any]) -> bool:
        """Record timeline updates in real time and return ``False`` if suppressed replay."""
        kind = update.get("sessionUpdate")
        if not isinstance(kind, str) or kind not in TIMELINE_UPDATE_KINDS:
            return True

        suppressed_mode = self._suppressed_sessions.get(session_id)
        if suppressed_mode is False:
            return False

        auto_id = self._allocate_auto_item_id(session_id, kind)
        upsert = build_update_upsert(update, auto_item_id=auto_id)
        if upsert is not None:
            if suppressed_mode is True:
                self._enqueue_item(self._hydration_buffers.setdefault(session_id, []), upsert)
            elif self._store is not None:
                self._apply_live_upsert(session_id, upsert)
        return suppressed_mode is None

    async def flush_session(self, session_id: str, *, finalize_active: bool = False) -> None:
        """Synchronously persist any queued live timeline deltas for ``session_id``."""
        task = self._flush_tasks.pop(session_id, None)
        current_task = asyncio.current_task()
        if task is not None and task is not current_task and not task.done():
            task.cancel()
        if finalize_active:
            self._stamp_previous_item_duration(session_id, time.monotonic())
        if self._store is None:
            self._live_buffers.pop(session_id, None)
            return

        async with self._io_lock:
            batch = self._live_buffers.pop(session_id, [])
            if not batch:
                return
            await asyncio.to_thread(self._store.upsert_items, session_id, batch)

    async def flush_all(self) -> None:
        """Flush all pending buffered sessions during WebSocket teardown."""
        pending_ids = tuple(set(self._live_buffers) | set(self._flush_tasks))
        for session_id in pending_ids:
            try:
                await self.flush_session(session_id)
            except (IndexerError, OSError, RuntimeError) as exc:
                logger.warning(
                    "[%s] Ignoring teardown chat flush error for '%s': %s",
                    self._connection_id,
                    session_id,
                    exc,
                )

    async def complete_setup_response(
        self,
        req_id: int | str,
        raw_line: str,
        loaded: Mapping[str, Any],
        *,
        succeeded: bool,
    ) -> str:
        """Commit buffered replay history (if hydrating) and attach ``_meta.cbiChat``."""
        pending = self._pending_setups.pop(req_id, None)
        is_metadata_only = req_id in self._metadata_only_reqs
        self._metadata_only_reqs.discard(req_id)
        if pending is None:
            return raw_line

        session_id, _, hydrate_reset = pending
        self._suppressed_sessions.pop(session_id, None)
        buffered = self._hydration_buffers.pop(session_id, [])
        self._auto_stream_state.pop(session_id, None)
        self._active_plan_ids.pop(session_id, None)
        self._active_item_by_session.pop(session_id, None)
        self._item_started_at = {
            key: val for key, val in self._item_started_at.items() if key[0] != session_id
        }
        if is_metadata_only or not succeeded or self._store is None:
            return raw_line

        async with self._io_lock:
            if hydrate_reset:
                if buffered:
                    await asyncio.to_thread(
                        self._store.upsert_items,
                        session_id,
                        buffered,
                        reset_existing=True,
                        mark_hydrated=True,
                    )
                else:
                    await asyncio.to_thread(
                        self._store.mark_session_hydrated,
                        session_id,
                        reset_existing=False,
                    )
            record = await asyncio.to_thread(self._store.get_session, session_id)

        summary = {
            "hydrated": bool(record.hydrated) if record is not None else hydrate_reset,
            "totalTurns": record.last_turn_index if record is not None else 0,
            "totalItems": record.total_items if record is not None else 0,
        }
        return attach_chat_meta_to_response(raw_line, loaded, summary)

    async def mark_new_session(self, session_id: str) -> None:
        """Mark a newly created ACP session as hydrated in ``ChatStoreProtocol``."""
        if self._store is None:
            return
        async with self._io_lock:
            await asyncio.to_thread(self._store.mark_session_hydrated, session_id)

    async def delete_session(self, session_id: str) -> None:
        """Cancel pending flushes and delete all stored history for ``session_id``."""
        task = self._flush_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._live_buffers.pop(session_id, None)
        self._hydration_buffers.pop(session_id, None)
        self._suppressed_sessions.pop(session_id, None)
        self._auto_stream_state.pop(session_id, None)
        self._active_plan_ids.pop(session_id, None)
        self._pending_user_echo.pop(session_id, None)
        self._active_item_by_session.pop(session_id, None)
        self._item_started_at = {
            key: val for key, val in self._item_started_at.items() if key[0] != session_id
        }
        if self._store is None:
            return
        async with self._io_lock:
            await asyncio.to_thread(self._store.delete_session, session_id)


__all__ = [
    "SessionChatRecorder",
]
