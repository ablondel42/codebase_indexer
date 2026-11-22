"""Stateless ACP-to-timeline conversion helpers for persistent session messaging."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from api.models.chat import (
    SessionChatMessageItem,
    SessionChatMessagesResponse,
    SessionChatPlanStep,
)
from core.models.chat import (
    ChatHistoryPage,
    ChatItemRole,
    ChatPlanStep,
    ChatTimelineItem,
    ChatTimelineItemUpsert,
)

TIMELINE_UPDATE_KINDS: frozenset[str] = frozenset(
    {
        "user_message_chunk",
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
    }
)

_CHUNK_ROLE_BY_UPDATE: dict[str, ChatItemRole] = {
    "user_message_chunk": ChatItemRole.USER,
    "agent_message_chunk": ChatItemRole.AGENT,
    "agent_thought_chunk": ChatItemRole.THOUGHT,
}

_TOOL_STATUS_MAP: dict[str, str] = {
    "in_progress": "running",
    "running": "running",
    "pending": "pending",
    "failed": "error",
    "error": "error",
    "denied": "denied",
    "rejected": "denied",
}

_PLAN_STATUS_MAP: dict[str, str] = {
    "completed": "done",
    "done": "done",
    "in_progress": "running",
    "running": "running",
}


def _extract_bool_meta_flag(raw_params: object, flag_key: str) -> tuple[bool, object]:
    """Consume boolean ``_meta.<flag_key>`` without disturbing other request metadata."""
    if not isinstance(raw_params, Mapping):
        return False, raw_params
    raw_meta = raw_params.get("_meta")
    if not isinstance(raw_meta, Mapping):
        return False, raw_params
    raw_flag = raw_meta.get(flag_key)
    if not isinstance(raw_flag, bool):
        return False, raw_params

    copied = dict(raw_params)
    rem_meta = {k: v for k, v in raw_meta.items() if k != flag_key}
    if rem_meta:
        copied["_meta"] = rem_meta
    else:
        copied.pop("_meta", None)
    return raw_flag, copied


def extract_hydrate_only_flag(raw_params: object) -> tuple[bool, object]:
    """Consume ``_meta.hydrateOnly`` without disturbing other request metadata."""
    return _extract_bool_meta_flag(raw_params, "hydrateOnly")


def extract_metadata_only_flag(raw_params: object) -> tuple[bool, object]:
    """Consume ``_meta.metadataOnly`` without disturbing other request metadata."""
    return _extract_bool_meta_flag(raw_params, "metadataOnly")


def _describe_embedded_resource(resource: object) -> str:
    """Format an embedded ACP resource block into a concise timeline label."""
    if not isinstance(resource, Mapping):
        return ""
    if isinstance(resource.get("text"), str) and resource["text"]:
        return resource["text"]
    uri = resource.get("uri")
    return f"[{uri}]" if isinstance(uri, str) and uri else ""


def _describe_prompt_block(block: object) -> str:
    """Extract readable display text for one ACP prompt content block."""
    if not isinstance(block, Mapping):
        return ""
    kind = block.get("type")
    result = ""
    if kind == "text" and isinstance(block.get("text"), str):
        result = block["text"]
    elif kind == "resource_link":
        label = block.get("name") or block.get("uri")
        result = f"[{label}]" if isinstance(label, str) and label else ""
    elif kind == "resource":
        result = _describe_embedded_resource(block.get("resource"))
    elif kind in ("image", "audio"):
        result = f"[{kind}]"
    return result


def extract_prompt_user_upsert(
    request_id: int | str,
    params: Mapping[str, Any],
) -> ChatTimelineItemUpsert | None:
    """Convert an outbound ``session/prompt`` payload into a user timeline item."""
    raw_blocks = params.get("prompt")
    if not isinstance(raw_blocks, list):
        return None
    parts = [text for blk in raw_blocks if (text := _describe_prompt_block(blk))]
    if not parts:
        return None
    return ChatTimelineItemUpsert(
        item_id=f"prompt:{request_id}",
        role=ChatItemRole.USER,
        text="\n".join(parts),
        merge_mode="replace",
    )


def map_raw_tool_status(raw_status: object) -> str:
    """Normalize ACP tool status strings onto UI tool status values."""
    if not isinstance(raw_status, str):
        return "done"
    return _TOOL_STATUS_MAP.get(raw_status.lower(), "done")


def _extract_content_entry_text(entry: object) -> str:
    """Extract displayable text from a single tool content array entry."""
    if not isinstance(entry, Mapping):
        return ""
    if isinstance(entry.get("text"), str):
        return entry["text"]
    inner = entry.get("content")
    if isinstance(inner, Mapping) and isinstance(inner.get("text"), str):
        return inner["text"]
    if isinstance(entry.get("path"), str):
        return entry["path"]
    return ""


def extract_tool_text(update: Mapping[str, Any]) -> str:
    """Extract normalized textual output from ACP ``tool_call`` / ``tool_call_update``."""
    parts: list[str] = []
    raw_content = update.get("content")
    if isinstance(raw_content, list):
        parts.extend(text for entry in raw_content if (text := _extract_content_entry_text(entry)))
    if parts:
        return "\n".join(parts)

    raw_out = update.get("rawOutput")
    if isinstance(raw_out, str):
        return raw_out
    if isinstance(raw_out, Mapping):
        stdout = raw_out.get("stdout") if isinstance(raw_out.get("stdout"), str) else ""
        stderr = raw_out.get("stderr") if isinstance(raw_out.get("stderr"), str) else ""
        return "\n".join(part for part in (stdout, stderr) if part)
    return ""


def extract_tool_locations(update: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract non-empty location path strings from a tool update."""
    raw_locs = update.get("locations")
    if not isinstance(raw_locs, list):
        return ()
    locations: list[str] = []
    for entry in raw_locs:
        if isinstance(entry, str) and entry:
            locations.append(entry[:260])
        elif isinstance(entry, Mapping) and isinstance(entry.get("path"), str) and entry["path"]:
            locations.append(entry["path"][:260])
    return tuple(locations)


def parse_plan_steps(raw_entries: object) -> tuple[ChatPlanStep, ...]:
    """Convert raw ACP ``plan.entries`` into immutable ``ChatPlanStep`` items."""
    if not isinstance(raw_entries, list):
        return ()
    steps: list[ChatPlanStep] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            continue
        raw_title = (
            raw.get("content")
            if isinstance(raw.get("content"), str)
            else raw.get("title")
            if isinstance(raw.get("title"), str)
            else ""
        )
        if not raw_title:
            continue
        raw_status_obj = raw.get("status")
        raw_status = raw_status_obj if isinstance(raw_status_obj, str) else "pending"
        mapped = _PLAN_STATUS_MAP.get(raw_status, "pending")
        if mapped == "done":
            steps.append(ChatPlanStep(title=raw_title[:200], status="done"))
        elif mapped == "running":
            steps.append(ChatPlanStep(title=raw_title[:200], status="running"))
        else:
            steps.append(ChatPlanStep(title=raw_title[:200], status="pending"))
    return tuple(steps)


def _build_chunk_upsert(
    kind: str,
    update: Mapping[str, Any],
    auto_item_id: str,
) -> ChatTimelineItemUpsert | None:
    """Build an append-text delta for ``user``/``agent``/``thought`` message chunks."""
    role = _CHUNK_ROLE_BY_UPDATE.get(kind)
    if role is None:
        return None
    content = update.get("content")
    if not isinstance(content, Mapping) or content.get("type") != "text":
        return None
    text = content.get("text")
    if not isinstance(text, str) or not text:
        return None
    raw_id = update.get("messageId")
    item_id = raw_id[:160] if isinstance(raw_id, str) and raw_id else auto_item_id
    return ChatTimelineItemUpsert(
        item_id=item_id,
        role=role,
        text=text,
        merge_mode="append_text",
    )


def _build_tool_upsert(
    kind: str,
    update: Mapping[str, Any],
    auto_item_id: str,
) -> ChatTimelineItemUpsert:
    """Build a tool call creation or status update upsert."""
    raw_id = update.get("toolCallId")
    item_id = raw_id[:160] if isinstance(raw_id, str) and raw_id else auto_item_id
    raw_status = update.get("status")
    tool_status = (
        map_raw_tool_status(raw_status)
        if raw_status is not None
        else ("done" if kind == "tool_call" else None)
    )
    raw_out = update.get("rawOutput")
    exit_code: int | None = None
    if isinstance(raw_out, Mapping):
        raw_exit = raw_out.get("exitCode")
        if isinstance(raw_exit, int) and not isinstance(raw_exit, bool):
            exit_code = raw_exit
    locations = extract_tool_locations(update)
    return ChatTimelineItemUpsert(
        item_id=item_id,
        role=ChatItemRole.TOOL,
        text=extract_tool_text(update),
        merge_mode="replace",
        tool_kind=str(update["kind"])[:48] if isinstance(update.get("kind"), str) else None,
        tool_title=str(update["title"])[:160]
        if isinstance(update.get("title"), str) and update.get("title")
        else None,
        tool_status=tool_status,
        exit_code=exit_code,
        locations=locations if locations else None,
    )


def _build_plan_upsert(
    update: Mapping[str, Any],
    auto_item_id: str,
) -> ChatTimelineItemUpsert | None:
    """Build an execution-plan upsert when entries are non-empty."""
    steps = parse_plan_steps(update.get("entries"))
    if not steps:
        return None
    raw_plan_id = update.get("planId")
    item_id = (
        f"plan:{raw_plan_id[:140]}"
        if isinstance(raw_plan_id, str) and raw_plan_id
        else auto_item_id
    )
    raw_title = update.get("title")
    title = raw_title[:140] if isinstance(raw_title, str) and raw_title else "Execution plan"
    return ChatTimelineItemUpsert(
        item_id=item_id,
        role=ChatItemRole.PLAN,
        text="",
        merge_mode="replace",
        plan_title=title,
        plan_steps=steps,
    )


def build_update_upsert(
    update: Mapping[str, Any],
    *,
    auto_item_id: str,
) -> ChatTimelineItemUpsert | None:
    """Convert one ACP ``session/update`` payload into a ``ChatTimelineItemUpsert``."""
    kind = update.get("sessionUpdate")
    if not isinstance(kind, str):
        return None
    if kind in _CHUNK_ROLE_BY_UPDATE:
        return _build_chunk_upsert(kind, update, auto_item_id)
    if kind in ("tool_call", "tool_call_update"):
        return _build_tool_upsert(kind, update, auto_item_id)
    if kind == "plan":
        return _build_plan_upsert(update, auto_item_id)
    return None


def _item_to_wire(item: ChatTimelineItem) -> SessionChatMessageItem:
    """Convert one domain ``ChatTimelineItem`` into its API response representation."""
    return SessionChatMessageItem(
        id=item.item_id,
        seq=item.seq,
        turnIndex=item.turn_index,
        role=item.role.value,
        text=item.text,
        durationSec=item.duration_sec,
        createdAt=item.created_at,
        toolKind=item.tool_kind,
        toolTitle=item.tool_title,
        toolStatus=item.tool_status,
        exitCode=item.exit_code,
        locations=item.locations,
        planTitle=item.plan_title,
        planSteps=tuple(
            SessionChatPlanStep(title=step.title, status=step.status) for step in item.plan_steps
        ),
    )


def items_to_wire(items: Sequence[ChatTimelineItem]) -> tuple[SessionChatMessageItem, ...]:
    """Convert a sequence of domain ``ChatTimelineItem`` records to wire items."""
    return tuple(_item_to_wire(item) for item in items)


def history_page_to_wire(page: ChatHistoryPage) -> SessionChatMessagesResponse:
    """Convert a domain ``ChatHistoryPage`` into ``SessionChatMessagesResponse``."""
    return SessionChatMessagesResponse(
        sessionId=page.session_id,
        hydrated=page.hydrated,
        items=items_to_wire(page.items),
        hasMore=page.has_more,
        nextBeforeTurn=page.next_before_turn,
        totalTurns=page.total_turns,
        totalItems=page.total_items,
    )


def attach_chat_meta_to_response(
    raw_line: str,
    loaded: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> str:
    """Inject ``_meta.cbiChat`` onto a successful JSON-RPC session setup response frame."""
    res_obj = loaded.get("result")
    if not isinstance(res_obj, Mapping):
        return raw_line
    copied = dict(loaded)
    result_copy = dict(res_obj)
    raw_meta = result_copy.get("_meta")
    meta_copy = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
    meta_copy["cbiChat"] = dict(summary)
    result_copy["_meta"] = meta_copy
    copied["result"] = result_copy
    return json.dumps(copied, separators=(",", ":"))


__all__ = [
    "TIMELINE_UPDATE_KINDS",
    "attach_chat_meta_to_response",
    "build_update_upsert",
    "extract_hydrate_only_flag",
    "extract_metadata_only_flag",
    "extract_prompt_user_upsert",
    "extract_tool_locations",
    "extract_tool_text",
    "history_page_to_wire",
    "items_to_wire",
    "map_raw_tool_status",
    "parse_plan_steps",
]
