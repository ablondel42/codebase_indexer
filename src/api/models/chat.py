"""Wire response models for paginated ACP conversation history REST endpoints."""

from typing import Literal

from pydantic import ConfigDict, Field

from core.models.base import FrozenModel

_CHAT_WIRE_CONFIG = ConfigDict(frozen=True, populate_by_name=True)


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


__all__ = [
    "SessionChatMessageItem",
    "SessionChatMessagesResponse",
    "SessionChatPlanStep",
]
