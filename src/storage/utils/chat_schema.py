"""SQLAlchemy Core schema and stateless mappers for persistent ACP chat history."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from core.models.chat import (
    ChatItemRole,
    ChatPlanStep,
    ChatSessionRecord,
    ChatTimelineItem,
    ChatTimelineItemUpsert,
)
from storage.utils.schema import JsonText

chat_metadata = MetaData()

CHAT_TRANSCRIPT_PARSER_VERSION = 3

chat_sessions_table = Table(
    "chat_sessions",
    chat_metadata,
    Column("session_id", Text, primary_key=True),
    Column(
        "hydrated",
        Boolean(create_constraint=False),
        nullable=False,
        default=False,
        server_default=text("0"),
    ),
    Column("last_seq", Integer, nullable=False, default=0, server_default=text("0")),
    Column("last_turn_index", Integer, nullable=False, default=0, server_default=text("0")),
    Column("last_turn_kind", Text, nullable=True),
    Column("total_items", Integer, nullable=False, default=0, server_default=text("0")),
    Column("transcript_step_index", Integer, nullable=True),
    Column("transcript_offset", Integer, nullable=False, default=0, server_default=text("0")),
    Column(
        "parser_version",
        Integer,
        nullable=False,
        default=CHAT_TRANSCRIPT_PARSER_VERSION,
        server_default=text(str(CHAT_TRANSCRIPT_PARSER_VERSION)),
    ),
    Column("updated_at", Text, nullable=False),
)

chat_items_table = Table(
    "chat_items",
    chat_metadata,
    Column(
        "session_id",
        Text,
        ForeignKey("chat_sessions.session_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("seq", Integer, primary_key=True),
    Column("turn_index", Integer, nullable=False),
    Column("item_id", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("text", Text, nullable=False, default="", server_default=text("''")),
    Column("duration_sec", Float, nullable=True),
    Column("tool_kind", Text, nullable=True),
    Column("tool_title", Text, nullable=True),
    Column("tool_status", Text, nullable=True),
    Column("exit_code", Integer, nullable=True),
    Column("locations_json", JsonText, nullable=True),
    Column("plan_title", Text, nullable=True),
    Column("plan_steps_json", JsonText, nullable=True),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    UniqueConstraint("session_id", "item_id", name="uq_chat_items_session_item"),
)

Index(
    "idx_chat_items_session_turn_seq",
    chat_items_table.c.session_id,
    chat_items_table.c.turn_index,
    chat_items_table.c.seq,
)


def utc_now_iso() -> str:
    """Return current UTC ISO-8601 timestamp string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def turn_kind_for_role(role: ChatItemRole) -> str:
    """Map an item role into coarse conversation turn ownership (``user`` vs ``assistant``)."""
    return "user" if role == ChatItemRole.USER else "assistant"


def advance_turn_cursor(
    last_turn_index: int,
    last_turn_kind: str | None,
    role: ChatItemRole,
) -> tuple[int, str]:
    """Return ``(turn_index, turn_kind)`` advancing when user/assistant ownership flips."""
    kind = turn_kind_for_role(role)
    if last_turn_index == 0 or last_turn_kind != kind:
        return last_turn_index + 1, kind
    return last_turn_index, kind


def serialize_plan_steps(
    steps: tuple[ChatPlanStep, ...] | None,
) -> list[dict[str, str]] | None:
    """Serialize ``ChatPlanStep`` tuples for JSON storage."""
    if steps is None:
        return None
    return [{"title": step.title, "status": step.status} for step in steps]


def _deserialize_plan_steps(raw: object) -> tuple[ChatPlanStep, ...]:
    """Parse stored plan steps JSON array back into immutable ``ChatPlanStep`` models."""
    if not isinstance(raw, list):
        return ()
    parsed: list[ChatPlanStep] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        title = entry.get("title")
        if not isinstance(title, str) or not title:
            continue
        raw_status = entry.get("status")
        status: Literal["done", "running", "pending"] = (
            cast(Literal["done", "running", "pending"], raw_status)
            if raw_status in ("done", "running", "pending")
            else "pending"
        )
        parsed.append(ChatPlanStep(title=title, status=status))
    return tuple(parsed)


def _deserialize_locations(raw: object) -> tuple[str, ...]:
    """Parse stored locations JSON list back into a tuple of strings."""
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)


def row_to_chat_session(row: Mapping[Any, Any]) -> ChatSessionRecord:
    """Convert a SQLAlchemy ``chat_sessions`` row mapping into ``ChatSessionRecord``."""
    raw_step = row["transcript_step_index"]
    raw_offset = row["transcript_offset"]
    return ChatSessionRecord(
        session_id=str(row["session_id"]),
        hydrated=bool(row["hydrated"]),
        last_seq=int(row["last_seq"]),
        last_turn_index=int(row["last_turn_index"]),
        total_items=int(row["total_items"]),
        transcript_step_index=int(raw_step) if raw_step is not None else None,
        transcript_offset=int(raw_offset) if raw_offset is not None else 0,
        updated_at=str(row["updated_at"]),
    )


def row_to_chat_item(row: Mapping[Any, Any]) -> ChatTimelineItem:
    """Convert a SQLAlchemy ``chat_items`` row mapping into ``ChatTimelineItem``."""
    raw_dur = row["duration_sec"]
    return ChatTimelineItem(
        session_id=str(row["session_id"]),
        seq=int(row["seq"]),
        turn_index=int(row["turn_index"]),
        item_id=str(row["item_id"]),
        role=ChatItemRole(str(row["role"])),
        text=str(row["text"] or ""),
        duration_sec=float(raw_dur) if raw_dur is not None else None,
        tool_kind=str(row["tool_kind"]) if row["tool_kind"] is not None else None,
        tool_title=str(row["tool_title"]) if row["tool_title"] is not None else None,
        tool_status=str(row["tool_status"]) if row["tool_status"] is not None else None,
        exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
        locations=_deserialize_locations(row["locations_json"]),
        plan_title=str(row["plan_title"]) if row["plan_title"] is not None else None,
        plan_steps=_deserialize_plan_steps(row["plan_steps_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def merge_chat_item(
    existing: ChatTimelineItem,
    delta: ChatTimelineItemUpsert,
    now_iso: str,
) -> ChatTimelineItem:
    """Merge an incoming ``ChatTimelineItemUpsert`` onto an existing persisted item."""
    if delta.merge_mode == "append_text":
        next_text = f"{existing.text}{delta.text}"
    else:
        next_text = delta.text if delta.text else existing.text
    return existing.model_copy(
        update={
            "text": next_text,
            "duration_sec": (
                delta.duration_sec if delta.duration_sec is not None else existing.duration_sec
            ),
            "tool_kind": delta.tool_kind if delta.tool_kind is not None else existing.tool_kind,
            "tool_title": delta.tool_title if delta.tool_title is not None else existing.tool_title,
            "tool_status": (
                delta.tool_status if delta.tool_status is not None else existing.tool_status
            ),
            "exit_code": delta.exit_code if delta.exit_code is not None else existing.exit_code,
            "locations": (
                delta.locations
                if delta.locations is not None and len(delta.locations) > 0
                else existing.locations
            ),
            "plan_title": delta.plan_title if delta.plan_title is not None else existing.plan_title,
            "plan_steps": (
                delta.plan_steps if delta.plan_steps is not None else existing.plan_steps
            ),
            "updated_at": now_iso,
        }
    )


def build_new_chat_item(
    session_id: str,
    seq_and_turn: tuple[int, int],
    delta: ChatTimelineItemUpsert,
    now_iso: str,
) -> ChatTimelineItem:
    """Create a fresh ``ChatTimelineItem`` from an unseen ``ChatTimelineItemUpsert``."""
    seq, turn_index = seq_and_turn
    return ChatTimelineItem(
        session_id=session_id,
        seq=seq,
        turn_index=turn_index,
        item_id=delta.item_id,
        role=delta.role,
        text=delta.text,
        duration_sec=delta.duration_sec,
        tool_kind=delta.tool_kind,
        tool_title=delta.tool_title,
        tool_status=delta.tool_status,
        exit_code=delta.exit_code,
        locations=delta.locations or (),
        plan_title=delta.plan_title,
        plan_steps=delta.plan_steps or (),
        created_at=now_iso,
        updated_at=now_iso,
    )


def chat_item_to_row(item: ChatTimelineItem) -> dict[str, Any]:
    """Convert a ``ChatTimelineItem`` into SQLite column values."""
    return {
        "session_id": item.session_id,
        "seq": item.seq,
        "turn_index": item.turn_index,
        "item_id": item.item_id,
        "role": str(item.role),
        "text": item.text,
        "duration_sec": item.duration_sec,
        "tool_kind": item.tool_kind,
        "tool_title": item.tool_title,
        "tool_status": item.tool_status,
        "exit_code": item.exit_code,
        "locations_json": list(item.locations) if item.locations else None,
        "plan_title": item.plan_title,
        "plan_steps_json": (
            serialize_plan_steps(item.plan_steps) if item.plan_steps else None
        ),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


__all__ = [
    "CHAT_TRANSCRIPT_PARSER_VERSION",
    "advance_turn_cursor",
    "build_new_chat_item",
    "chat_item_to_row",
    "chat_items_table",
    "chat_metadata",
    "chat_sessions_table",
    "merge_chat_item",
    "row_to_chat_item",
    "row_to_chat_session",
    "serialize_plan_steps",
    "turn_kind_for_role",
    "utc_now_iso",
]
