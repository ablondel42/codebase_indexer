"""Wire response models and real-time sync payloads for ACP conversation timelines."""

from typing import Literal

from pydantic import ConfigDict, Field

from core.models.base import FrozenModel
from core.models.chat import ChatTimelineItemUpsert

_CHAT_WIRE_CONFIG = ConfigDict(frozen=True, populate_by_name=True)

ChatSyncEventKind = Literal[
    "items_upserted",
    "timeline_reset",
    "artifacts_changed",
    "sessions_changed",
]


class SessionChatPlanStep(FrozenModel):
    """Serializable step inside a persisted assistant execution plan."""

    title: str
    status: Literal["done", "running", "pending"] = "pending"


class SessionChatMessageItem(FrozenModel):
    """Frontend-compatible timeline item emitted by ``GET /api/sessions/{id}/messages``."""

    model_config = _CHAT_WIRE_CONFIG

    id: str
    seq: int = Field(ge=1)
    turn_index: int = Field(alias="turnIndex", ge=1)
    role: Literal["user", "agent", "thought", "tool", "plan"]
    text: str = ""
    duration_sec: float | None = Field(default=None, alias="durationSec")
    created_at: str | None = Field(default=None, alias="createdAt")
    tool_kind: str | None = Field(default=None, alias="toolKind")
    tool_title: str | None = Field(default=None, alias="toolTitle")
    tool_status: str | None = Field(default=None, alias="toolStatus")
    exit_code: int | None = Field(default=None, alias="exitCode")
    locations: tuple[str, ...] = ()
    plan_title: str | None = Field(default=None, alias="planTitle")
    plan_steps: tuple[SessionChatPlanStep, ...] = Field(default=(), alias="planSteps")


class SessionChatMessagesResponse(FrozenModel):
    """Turn-aligned paginated message slice returned over HTTP."""

    model_config = _CHAT_WIRE_CONFIG

    session_id: str = Field(alias="sessionId")
    hydrated: bool = False
    items: tuple[SessionChatMessageItem, ...] = ()
    has_more: bool = Field(default=False, alias="hasMore")
    next_before_turn: int | None = Field(default=None, alias="nextBeforeTurn")
    total_turns: int = Field(default=0, alias="totalTurns")
    total_items: int = Field(default=0, alias="totalItems")


class ChatSyncNotificationParams(FrozenModel):
    """JSON-RPC ``cbi/chat_sync`` push notification payload sent over ``/acp``."""

    model_config = _CHAT_WIRE_CONFIG

    session_id: str | None = Field(default=None, alias="sessionId")
    event: ChatSyncEventKind
    items: tuple[SessionChatMessageItem, ...] = ()
    total_turns: int | None = Field(default=None, alias="totalTurns")
    total_items: int | None = Field(default=None, alias="totalItems")
    has_more: bool | None = Field(default=None, alias="hasMore")
    next_before_turn: int | None = Field(default=None, alias="nextBeforeTurn")
    turn_active: bool | None = Field(default=None, alias="turnActive")
    turn_started_at: str | None = Field(default=None, alias="turnStartedAt")


class TranscriptToolBatchState(FrozenModel):
    """In-flight tool-call batch carried across incremental NDJSON tail reads."""

    planner_step: int
    next_tool_idx: int
    total_tools: int
    planner_item_id: str | None = None
    planner_role: Literal["thought", "agent"] | None = None
    planner_created_at: str | None = None
    duration_updated: bool = False


class TranscriptBackgroundTask(FrozenModel):
    """In-flight background command tracked until its completion ``SYSTEM_MESSAGE`` arrives."""

    task_id: str
    item_id: str
    started_at: str | None = None


class TranscriptSessionSyncState(FrozenModel):
    """Cached incremental parser state carried between watcher runs for one session."""

    pending_batch: TranscriptToolBatchState | None = None
    pending_tasks: tuple[TranscriptBackgroundTask, ...] = ()
    turn_started_at: str | None = None
    turn_active: bool = False


class TranscriptParseResult(FrozenModel):
    """Structured result from reading a ``transcript_full.jsonl`` byte-offset delta."""

    upserts: tuple[ChatTimelineItemUpsert, ...] = ()
    last_step_index: int = Field(ge=-1)
    next_offset: int = Field(ge=0)
    needs_reset: bool = False
    pending_batch: TranscriptToolBatchState | None = None
    pending_tasks: tuple[TranscriptBackgroundTask, ...] = ()
    turn_active: bool = False
    turn_started_at: str | None = None


__all__ = [
    "ChatSyncEventKind",
    "ChatSyncNotificationParams",
    "SessionChatMessageItem",
    "SessionChatMessagesResponse",
    "SessionChatPlanStep",
    "TranscriptBackgroundTask",
    "TranscriptParseResult",
    "TranscriptSessionSyncState",
    "TranscriptToolBatchState",
]
