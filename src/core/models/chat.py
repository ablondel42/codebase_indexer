"""Immutable domain models for persistent ACP conversation timelines and pagination."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from core.models.base import FrozenModel


class ChatItemRole(StrEnum):
    """Role of a timeline entry stored for an ACP conversation."""

    USER = "user"
    AGENT = "agent"
    THOUGHT = "thought"
    TOOL = "tool"
    PLAN = "plan"


class ChatPlanStep(FrozenModel):
    """Single step within a persisted ACP agent execution plan."""

    title: str
    status: Literal["done", "running", "pending"] = "pending"


class ChatTranscriptCursor(FrozenModel):
    """Monotonic byte and step cursor for incremental ``transcript_full.jsonl`` tailing."""

    step_index: int = Field(ge=-1)
    byte_offset: int = Field(ge=0)


class ChatTimelineItem(FrozenModel):
    """Persisted ordered item belonging to an ACP conversation timeline."""

    session_id: str
    seq: int = Field(ge=1)
    turn_index: int = Field(ge=1)
    item_id: str
    role: ChatItemRole
    text: str = ""
    duration_sec: float | None = Field(default=None, ge=0.0)
    tool_kind: str | None = None
    tool_title: str | None = None
    tool_status: str | None = None
    exit_code: int | None = None
    locations: tuple[str, ...] = ()
    plan_title: str | None = None
    plan_steps: tuple[ChatPlanStep, ...] = ()
    created_at: str
    updated_at: str


class ChatTimelineItemUpsert(FrozenModel):
    """Upsert delta applied in real time to an ACP conversation timeline."""

    item_id: str = Field(min_length=1, max_length=160)
    role: ChatItemRole
    text: str = ""
    merge_mode: Literal["append_text", "replace"] = "replace"
    duration_sec: float | None = Field(default=None, ge=0.0)
    created_at: str | None = None
    tool_kind: str | None = None
    tool_title: str | None = None
    tool_status: str | None = None
    exit_code: int | None = None
    locations: tuple[str, ...] | None = None
    plan_title: str | None = None
    plan_steps: tuple[ChatPlanStep, ...] | None = None


class ChatSessionRecord(FrozenModel):
    """Summary metadata and hydration marker for one persisted ACP chat session."""

    session_id: str
    hydrated: bool = False
    last_seq: int = Field(default=0, ge=0)
    last_turn_index: int = Field(default=0, ge=0)
    total_items: int = Field(default=0, ge=0)
    transcript_step_index: int | None = None
    transcript_offset: int = Field(default=0, ge=0)
    updated_at: str


class ChatPageQuery(FrozenModel):
    """Cursor query fetching a turn-aligned window of conversation history."""

    session_id: str = Field(min_length=1)
    limit_turns: int = Field(default=8, ge=1, le=50)
    before_turn: int | None = Field(default=None, ge=1)


class ChatHistoryPage(FrozenModel):
    """Turn-aligned slice of persisted conversation timeline items."""

    session_id: str
    hydrated: bool = False
    items: tuple[ChatTimelineItem, ...] = ()
    has_more: bool = False
    next_before_turn: int | None = None
    total_turns: int = Field(default=0, ge=0)
    total_items: int = Field(default=0, ge=0)


__all__ = [
    "ChatHistoryPage",
    "ChatItemRole",
    "ChatPageQuery",
    "ChatPlanStep",
    "ChatSessionRecord",
    "ChatTimelineItem",
    "ChatTimelineItemUpsert",
    "ChatTranscriptCursor",
]
