"""Stateless parsers for external Antigravity profile logs and sync notifications."""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from api.models.chat import (
    ChatSyncNotificationParams,
    TranscriptBackgroundTask,
    TranscriptParseResult,
    TranscriptToolBatchState,
)
from api.utils.artifacts import validate_artifact_session_id
from core.models.chat import ChatItemRole, ChatTimelineItemUpsert
from core.models.exceptions import AcpError
from core.utils.logging import get_logger

logger = get_logger("api.utils.transcript")

MAX_TOOL_OUTPUT_CHARS = 8000
_SEARCH_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "find_by_name",
        "get_outline",
        "get_repo_overview",
        "grep_search",
        "list_dir",
        "list_files",
        "search_code",
        "search_references",
        "search_symbols",
    }
)
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_CONTEXT_SUMMARY_RE = re.compile(r"<CONTEXT_SUMMARY>.*?</CONTEXT_SUMMARY>", re.DOTALL)
_ADDITIONAL_META_RE = re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.DOTALL)
_CREATED_AT_RE = re.compile(r"^Created At:\s*(\S+)\s*$", re.MULTILINE)
_COMPLETED_AT_RE = re.compile(r"^Completed At:\s*(\S+)\s*$", re.MULTILINE)
_EXIT_CODE_RE = re.compile(r"^Command exited with code (-?\d+)\b", re.MULTILINE)
_BG_TASK_ID_RE = re.compile(r"\ATool is running as a background task with task id:\s*(\S+)")
_BG_LOG_OUTPUT_RE = re.compile(r"\nLog output:\n(.*)", re.DOTALL)
_BG_AGENT_DIRECTIVE_RE = re.compile(
    r"\n*YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS:.*",
    re.DOTALL,
)
_SYSTEM_MSG_BLOCK_RE = re.compile(r"<SYSTEM_MESSAGE>\s*(.*?)\s*</SYSTEM_MESSAGE>", re.DOTALL)
_TASK_RESULT_RE = re.compile(
    r"\A\[Message\]\s*timestamp=(\S+)\s+sender=\S+\s+priority=\S+\s+"
    r'content=Task id "([^"]+)" (finished|was canceled|failed) with result:\n?(.*)',
    re.DOTALL,
)
_TRAILING_LOG_URI_RE = re.compile(r"\n?Log:\s*file://\S+\s*$")


def _safe_session_id(raw_sid: str) -> str | None:
    """Validate ``raw_sid`` without propagating path-validation errors on ignore paths."""
    try:
        return validate_artifact_session_id(raw_sid)
    except AcpError as exc:
        logger.debug("Ignoring non-session path segment '%s': %s", raw_sid, exc)
        return None


def resolve_transcript_path(profile_root: Path, session_id: str) -> Path | None:
    """Return contained ``transcript_full.jsonl`` path for ``session_id``, or ``None``."""
    clean_id = _safe_session_id(session_id)
    if clean_id is None:
        return None
    try:
        resolved_root = profile_root.resolve()
        candidate = profile_root / "brain" / clean_id / ".system_generated" / "logs" / (
            "transcript_full.jsonl"
        )
        if candidate.is_symlink():
            return None
        resolved_file = candidate.resolve()
    except OSError as exc:
        logger.debug("Failed resolving transcript path for '%s': %s", session_id, exc)
        return None
    if not resolved_file.is_relative_to(resolved_root) or not resolved_file.is_file():
        return None
    return resolved_file


def _classify_brain_parts(parts: tuple[str, ...]) -> tuple[str, str | None]:
    """Classify relative path parts rooted under ``<profile_root>/brain``."""
    if len(parts) < 3:
        return ("ignore", None)
    sid = _safe_session_id(parts[1])
    if sid is None:
        return ("ignore", None)
    if parts[2:] == (".system_generated", "logs", "transcript_full.jsonl"):
        return ("transcript", sid)
    if len(parts) == 3 and (parts[2].endswith(".md") or parts[2].endswith(".md.metadata.json")):
        return ("artifact", sid)
    return ("ignore", None)


def _classify_session_db_name(filename: str) -> str | None:
    """Extract session ID from exact ``conversations/<sid>.db`` filenames when valid."""
    if filename.endswith(".db"):
        return _safe_session_id(filename[:-3])
    return None


def _classify_relative_parts(
    parts: tuple[str, ...],
    *,
    is_directory: bool,
) -> tuple[str, str | None]:
    """Classify relative non-escaped path parts under ``<profile_root>``."""
    result: tuple[str, str | None] = ("ignore", None)
    if parts and not parts[0].startswith("cbi_chat.db"):
        if is_directory and len(parts) == 1 and parts[0] in ("brain", "conversations"):
            result = ("watch_subdir", parts[0])
        elif not is_directory and parts[0] == "brain":
            result = _classify_brain_parts(parts)
        elif (
            not is_directory
            and parts[0] == "conversations"
            and len(parts) == 2
            and parts[1].endswith(".db")
        ):
            result = ("sessions", _classify_session_db_name(parts[1]))
    return result


def classify_profile_watch_path(
    profile_root: Path,
    changed_path: Path,
    *,
    is_directory: bool,
) -> tuple[str, str | None]:
    """Classify one filesystem watch path within ``profile_root`` into sync categories."""
    if changed_path.is_symlink():
        return ("ignore", None)
    try:
        resolved_root = profile_root.resolve()
        resolved_target = changed_path.resolve()
    except OSError as exc:
        logger.debug("Skipping unresolvable watch path %s: %s", changed_path, exc)
        return ("ignore", None)
    if not resolved_target.is_relative_to(resolved_root):
        return ("ignore", None)
    parts = resolved_target.relative_to(resolved_root).parts
    return _classify_relative_parts(parts, is_directory=is_directory)


def extract_user_request_text(raw_content: str) -> str:
    """Extract clean user prompt text from ``USER_INPUT`` transcript steps."""
    match = _USER_REQUEST_RE.search(raw_content)
    if match is not None:
        return match.group(1).strip()
    cleaned = _CONTEXT_SUMMARY_RE.sub("", raw_content)
    cleaned = _ADDITIONAL_META_RE.sub("", cleaned)
    return cleaned.strip()


def _parse_iso_timestamp(raw: object) -> float | None:
    """Parse an ISO-8601 timestamp string into POSIX epoch seconds."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    normalized = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError as exc:
        logger.debug("Ignoring invalid ISO timestamp %r: %s", raw, exc)
        return None


def _round_elapsed_seconds(start_ts: float | None, end_ts: float | None) -> float | None:
    """Compute bounded elapsed seconds rounded to tenths when valid."""
    if start_ts is None or end_ts is None or end_ts < start_ts:
        return None
    delta = end_ts - start_ts
    if delta > 86_400:
        return None
    return max(0.1, round(delta, 1)) if delta >= 0.05 else 0.0


def _describe_file_tool(name: str, args: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    """Map file read/edit/write tool calls into UI kind, title, and locations."""
    raw_path = args.get("Path") or args.get("path")
    path_str = str(raw_path)[:260] if isinstance(raw_path, str) and raw_path else ""
    basename = Path(path_str).name if path_str else name
    if name in ("read_file", "view_file"):
        return ("read", f"Read {basename}"[:160], (path_str,) if path_str else ())
    verb = "Write" if name == "write_file" else "Edit"
    return ("edit", f"{verb} {basename}"[:160], (path_str,) if path_str else ())


def _describe_tool_call(raw_call: object) -> tuple[str, str, tuple[str, ...]]:
    """Convert one ``PLANNER_RESPONSE.tool_calls`` entry into tool UI metadata."""
    if not isinstance(raw_call, Mapping):
        return ("other", "Tool call", ())
    name = str(raw_call.get("name") or "tool")
    raw_args = raw_call.get("args")
    args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}
    result: tuple[str, str, tuple[str, ...]] = ("other", f"Run {name}"[:160], ())
    if name in ("read_file", "view_file", "edit_file", "write_file"):
        result = _describe_file_tool(name, args)
    elif name == "run_command":
        cmd = str(args.get("CommandLine") or args.get("command") or "command")
        result = ("execute", cmd.splitlines()[0][:160], ())
    elif name == "call_mcp_tool":
        tool_name = str(args.get("ToolName") or "mcp")
        kind = "search" if tool_name in _SEARCH_MCP_TOOLS else "fetch"
        result = (kind, f"MCP Tool {tool_name}"[:160], ())
    elif name == "search_web":
        query = str(args.get("query") or "")
        title = f"Search web: {query}" if query else "Search web"
        result = ("search", title[:160], ())
    return result


def _truncate_tool_text(body: str) -> str:
    """Clamp normalized tool text to ``MAX_TOOL_OUTPUT_CHARS`` with truncation marker."""
    if len(body) > MAX_TOOL_OUTPUT_CHARS:
        return f"{body[:MAX_TOOL_OUTPUT_CHARS]}\n...[truncated]"
    return body


def _strip_timestamp_headers(normalized_content: str) -> str:
    """Strip leading ``Created At:`` and ``Completed At:`` header lines."""
    lines = normalized_content.splitlines()
    idx = 0
    while idx < len(lines) and (
        lines[idx].startswith("Created At:") or lines[idx].startswith("Completed At:")
    ):
        idx += 1
    return "\n".join(lines[idx:]).lstrip("\n")


def _extract_background_preview(body: str) -> str:
    """Extract live ``Log output:`` preview while removing internal agent prompt directives."""
    log_match = _BG_LOG_OUTPUT_RE.search(body)
    if log_match is None:
        return ""
    preview = _BG_AGENT_DIRECTIVE_RE.sub("", log_match.group(1)).strip()
    return _truncate_tool_text(preview)


def _extract_background_launch(
    raw_content: str,
    fallback_created_at: object,
) -> tuple[str, str, str | None] | None:
    """Return ``(task_id, preview_body, started_at)`` when ``raw_content`` launches a task."""
    normalized = raw_content.replace("\r\n", "\n")
    body = _strip_timestamp_headers(normalized)
    match = _BG_TASK_ID_RE.match(body)
    if match is None:
        return None
    created_matches = _CREATED_AT_RE.findall(normalized)
    fallback = (
        fallback_created_at.strip()
        if isinstance(fallback_created_at, str) and fallback_created_at.strip()
        else None
    )
    started_at = created_matches[0] if created_matches else fallback
    return match.group(1), _extract_background_preview(body), started_at


def clean_generic_tool_output(raw_content: str) -> tuple[str, float | None, int | None]:
    """Strip tool timestamp headers and extract ``(text, duration_sec, exit_code)``."""
    normalized = raw_content.replace("\r\n", "\n")
    created_matches = _CREATED_AT_RE.findall(normalized)
    completed_matches = _COMPLETED_AT_RE.findall(normalized)
    start_ts = _parse_iso_timestamp(created_matches[0]) if created_matches else None
    end_ts = _parse_iso_timestamp(completed_matches[-1]) if completed_matches else None
    duration_sec = _round_elapsed_seconds(start_ts, end_ts)

    exit_match = _EXIT_CODE_RE.search(normalized)
    exit_code = int(exit_match.group(1)) if exit_match is not None else None

    body = _strip_timestamp_headers(normalized)
    if _BG_TASK_ID_RE.match(body) is not None:
        body = _extract_background_preview(body)
    else:
        body = _truncate_tool_text(body)
    return body, duration_sec, exit_code


def _decode_consumed_jsonl(consumed: bytes) -> list[dict[str, Any]]:
    """Parse decoded newline-terminated JSON object lines while skipping invalid lines."""
    entries: list[dict[str, Any]] = []
    for raw_line in consumed.decode("utf-8", errors="replace").splitlines():
        text_line = raw_line.strip()
        if not text_line:
            continue
        try:
            loaded = json.loads(text_line)
        except json.JSONDecodeError as exc:
            logger.debug("Skipping malformed transcript JSON line: %s", exc)
            continue
        if isinstance(loaded, dict):
            entries.append(loaded)
    return entries


def _extract_complete_lines(
    transcript_path: Path,
    start_offset: int,
) -> tuple[list[dict[str, Any]], int, bool, float | None]:
    """Read complete newline-terminated JSON objects starting at byte ``start_offset``."""
    try:
        file_stat = transcript_path.stat()
    except OSError as exc:
        logger.debug("Transcript stat failed for %s: %s", transcript_path, exc)
        return [], start_offset, False, None

    file_size = file_stat.st_size
    if file_size < start_offset:
        return [], 0, True, file_stat.st_mtime
    if file_size == start_offset:
        return [], start_offset, False, file_stat.st_mtime

    result = ([], start_offset, False, file_stat.st_mtime)
    try:
        with transcript_path.open("rb") as handle:
            handle.seek(start_offset)
            chunk = handle.read(file_size - start_offset)
    except OSError as exc:
        logger.debug("Transcript read failed for %s: %s", transcript_path, exc)
        return result

    last_nl = chunk.rfind(b"\n")
    if last_nl >= 0:
        consumed = chunk[: last_nl + 1]
        result = (
            _decode_consumed_jsonl(consumed),
            start_offset + len(consumed),
            False,
            file_stat.st_mtime,
        )
    return result


def _backfill_planner_duration(
    upserts: list[ChatTimelineItemUpsert],
    batch: TranscriptToolBatchState | None,
    step_created_at: object,
) -> TranscriptToolBatchState | None:
    """Backfill duration onto the preceding planner step once its next step begins."""
    if batch is None or batch.duration_updated or batch.planner_item_id is None:
        return batch
    planner_ts = _parse_iso_timestamp(batch.planner_created_at)
    next_ts = _parse_iso_timestamp(step_created_at)
    dur = _round_elapsed_seconds(planner_ts, next_ts)
    if dur is not None and dur > 0.0:
        role = ChatItemRole.THOUGHT if batch.planner_role == "thought" else ChatItemRole.AGENT
        upserts.append(
            ChatTimelineItemUpsert(
                item_id=batch.planner_item_id,
                role=role,
                text="",
                duration_sec=dur,
                merge_mode="replace",
            )
        )
    return batch.model_copy(update={"duration_updated": True})


def _append_planner_upserts(
    step_index: int,
    record: Mapping[str, Any],
    upserts: list[ChatTimelineItemUpsert],
    duration_sec: float | None,
) -> tuple[TranscriptToolBatchState | None, bool]:
    """Emit thought, reply, and pending tool upserts for one ``PLANNER_RESPONSE`` step."""
    thinking = record.get("thinking")
    planner_item_id: str | None = None
    planner_role: Literal["thought", "agent"] | None = None
    if isinstance(thinking, str) and thinking.strip():
        planner_item_id = f"tf:{step_index}:thought"
        planner_role = "thought"
        upserts.append(
            ChatTimelineItemUpsert(
                item_id=planner_item_id,
                role=ChatItemRole.THOUGHT,
                text=thinking,
                duration_sec=duration_sec,
                merge_mode="replace",
            )
        )

    content = record.get("content")
    if isinstance(content, str) and content.strip():
        reply_id = f"tf:{step_index}:agent"
        if planner_item_id is None:
            planner_item_id = reply_id
            planner_role = "agent"
        upserts.append(
            ChatTimelineItemUpsert(
                item_id=reply_id,
                role=ChatItemRole.AGENT,
                text=content,
                duration_sec=duration_sec if planner_role == "agent" else None,
                merge_mode="replace",
            )
        )

    raw_tools = record.get("tool_calls")
    tool_calls = raw_tools if isinstance(raw_tools, list) else []
    for idx, call in enumerate(tool_calls):
        tool_kind, tool_title, locations = _describe_tool_call(call)
        upserts.append(
            ChatTimelineItemUpsert(
                item_id=f"tf:{step_index}:tool:{idx}",
                role=ChatItemRole.TOOL,
                text="",
                merge_mode="replace",
                tool_kind=tool_kind,
                tool_title=tool_title,
                tool_status="running",
                locations=locations or None,
            )
        )
    if not tool_calls:
        return None, str(record.get("status") or "").upper() == "RUNNING"

    raw_created = record.get("created_at")
    batch = TranscriptToolBatchState(
        planner_step=step_index,
        next_tool_idx=0,
        total_tools=len(tool_calls),
        planner_item_id=planner_item_id,
        planner_role=planner_role,
        planner_created_at=str(raw_created) if isinstance(raw_created, str) else None,
        duration_updated=duration_sec is not None,
    )
    return batch, True


def _resolve_generic_tool_status(raw_status: str, exit_code: int | None) -> str:
    """Map raw ``MODEL/GENERIC`` step status and exit code onto UI tool status."""
    if raw_status == "RUNNING":
        return "running"
    if raw_status == "ERROR" or (exit_code is not None and exit_code != 0):
        return "error"
    return "done"


def _apply_generic_step(
    record: Mapping[str, Any],
    batch: TranscriptToolBatchState,
    tasks_map: dict[str, TranscriptBackgroundTask],
    upserts: list[ChatTimelineItemUpsert],
) -> TranscriptToolBatchState:
    """Pair one ``MODEL/GENERIC`` step onto the next pending tool item in ``batch``."""
    updated_batch = _backfill_planner_duration(upserts, batch, record.get("created_at"))
    assert updated_batch is not None
    tool_idx = updated_batch.next_tool_idx
    item_id = f"tf:{updated_batch.planner_step}:tool:{tool_idx}"
    raw_content = str(record.get("content") or "")
    raw_status = str(record.get("status") or "DONE").upper()
    bg_launch = (
        _extract_background_launch(raw_content, record.get("created_at"))
        if raw_status == "RUNNING"
        else None
    )
    if bg_launch is not None:
        task_id, preview_body, started_at = bg_launch
        tasks_map[task_id] = TranscriptBackgroundTask(
            task_id=task_id,
            item_id=item_id,
            started_at=started_at,
        )
        upserts.append(
            ChatTimelineItemUpsert(
                item_id=item_id,
                role=ChatItemRole.TOOL,
                text=preview_body,
                duration_sec=None,
                merge_mode="replace",
                tool_status="running",
                exit_code=None,
            )
        )
        return updated_batch.model_copy(update={"next_tool_idx": tool_idx + 1})

    body, duration_sec, exit_code = clean_generic_tool_output(raw_content)
    upserts.append(
        ChatTimelineItemUpsert(
            item_id=item_id,
            role=ChatItemRole.TOOL,
            text=body,
            duration_sec=duration_sec,
            merge_mode="replace",
            tool_status=_resolve_generic_tool_status(raw_status, exit_code),
            exit_code=exit_code,
        )
    )
    return updated_batch.model_copy(update={"next_tool_idx": tool_idx + 1})


def _parse_task_completion_message(raw_content: str) -> tuple[str, str, str, str] | None:
    """Extract ``(msg_ts, task_id, outcome, raw_result)`` from background task notices."""
    block_match = _SYSTEM_MSG_BLOCK_RE.search(raw_content)
    inner = block_match.group(1) if block_match is not None else raw_content.strip()
    matched = _TASK_RESULT_RE.match(inner)
    if matched is None:
        return None
    return (matched.group(1), matched.group(2), matched.group(3), matched.group(4))


def _build_task_completion_upsert(
    task: TranscriptBackgroundTask,
    parsed_msg: tuple[str, str, str, str],
    fallback_ts: object,
) -> ChatTimelineItemUpsert:
    """Convert one background task completion message into a settled tool item upsert."""
    msg_ts, _, outcome, raw_result = parsed_msg
    normalized = raw_result.replace("\r\n", "\n")
    exit_match = _EXIT_CODE_RE.search(normalized)
    exit_code = int(exit_match.group(1)) if exit_match is not None else None
    cleaned = _TRAILING_LOG_URI_RE.sub("", normalized).strip()
    if not cleaned:
        cleaned = "Tool execution was canceled." if outcome != "finished" else "Command completed."
    end_ts = _parse_iso_timestamp(msg_ts) or _parse_iso_timestamp(fallback_ts)
    duration_sec = _round_elapsed_seconds(_parse_iso_timestamp(task.started_at), end_ts)
    tool_status = "done" if outcome == "finished" and exit_code in (None, 0) else "error"
    return ChatTimelineItemUpsert(
        item_id=task.item_id,
        role=ChatItemRole.TOOL,
        text=_truncate_tool_text(cleaned),
        duration_sec=duration_sec,
        merge_mode="replace",
        tool_status=tool_status,
        exit_code=exit_code,
    )


def _stop_restarted_tasks(
    tasks_map: dict[str, TranscriptBackgroundTask],
    upserts: list[ChatTimelineItemUpsert],
    stopped_at: object,
) -> None:
    """Mark all pending background tasks as errored when Antigravity reports a server restart."""
    end_ts = _parse_iso_timestamp(stopped_at)
    for task in tasks_map.values():
        duration_sec = _round_elapsed_seconds(_parse_iso_timestamp(task.started_at), end_ts)
        upserts.append(
            ChatTimelineItemUpsert(
                item_id=task.item_id,
                role=ChatItemRole.TOOL,
                text="Background task stopped due to server restart.",
                duration_sec=duration_sec,
                merge_mode="replace",
                tool_status="error",
            )
        )
    tasks_map.clear()


def _apply_system_message_step(
    entry: Mapping[str, Any],
    allow_incremental_reset: bool,
    tasks_map: dict[str, TranscriptBackgroundTask],
    upserts: list[ChatTimelineItemUpsert],
    active: bool,
) -> tuple[bool, bool]:
    """Resolve background task completion or restart notices from ``SYSTEM_MESSAGE`` entries."""
    raw_content = str(entry.get("content") or "")
    parsed_msg = _parse_task_completion_message(raw_content)
    if parsed_msg is not None:
        task = tasks_map.pop(parsed_msg[1], None)
        if task is None:
            return active, allow_incremental_reset
        upserts.append(_build_task_completion_upsert(task, parsed_msg, entry.get("created_at")))
        return True, False

    block_match = _SYSTEM_MSG_BLOCK_RE.search(raw_content)
    inner = block_match.group(1) if block_match is not None else raw_content.strip()
    if (
        inner.startswith("[Message]")
        and "All your subagents and background tasks have been stopped" in inner
        and tasks_map
    ):
        _stop_restarted_tasks(tasks_map, upserts, entry.get("created_at"))
        return False, False
    return active, False


def _planner_duration_hint(
    idx: int,
    entries: Sequence[Mapping[str, Any]],
) -> float | None:
    """Compute exact ``PLANNER_RESPONSE`` duration when its following step is present."""
    if idx + 1 >= len(entries):
        return None
    next_entry = entries[idx + 1]
    if next_entry.get("type") == "USER_INPUT":
        return None
    start_ts = _parse_iso_timestamp(entries[idx].get("created_at"))
    end_ts = _parse_iso_timestamp(next_entry.get("created_at"))
    return _round_elapsed_seconds(start_ts, end_ts)


def _apply_user_step(
    step_index: int,
    entry: Mapping[str, Any],
    upserts: list[ChatTimelineItemUpsert],
    turn_started_at: str | None,
) -> tuple[TranscriptToolBatchState | None, bool, str | None]:
    """Handle one ``USER_EXPLICIT/USER_INPUT`` transcript step."""
    prompt_text = extract_user_request_text(str(entry.get("content") or ""))
    if prompt_text:
        upserts.append(
            ChatTimelineItemUpsert(
                item_id=f"tf:{step_index}:user",
                role=ChatItemRole.USER,
                text=prompt_text,
                merge_mode="replace",
            )
        )
    raw_ca = entry.get("created_at")
    next_started = raw_ca if isinstance(raw_ca, str) else turn_started_at
    return None, True, next_started


def _apply_error_step(
    step_index: int,
    entry: Mapping[str, Any],
    upserts: list[ChatTimelineItemUpsert],
) -> tuple[TranscriptToolBatchState | None, bool]:
    """Handle one ``SYSTEM/ERROR_MESSAGE`` transcript step."""
    err_text = str(entry.get("content") or entry.get("error") or "").strip()
    if err_text:
        upserts.append(
            ChatTimelineItemUpsert(
                item_id=f"tf:{step_index}:error",
                role=ChatItemRole.AGENT,
                text=err_text,
                merge_mode="replace",
            )
        )
    return None, False


def _handle_model_entry(
    idx: int,
    entries: list[dict[str, Any]],
    context: tuple[int, bool],
    state: tuple[
        TranscriptToolBatchState | None,
        bool,
        str | None,
        list[ChatTimelineItemUpsert],
        dict[str, TranscriptBackgroundTask],
    ],
) -> tuple[TranscriptToolBatchState | None, bool, str | None, bool]:
    """Handle one ``MODEL/PLANNER_RESPONSE`` or ``MODEL/GENERIC`` transcript entry."""
    step_index, allow_incremental_reset = context
    batch, active, started_at, upserts, tasks_map = state
    step_type = str(entries[idx].get("type") or "")
    if step_type == "PLANNER_RESPONSE":
        dur = _planner_duration_hint(idx, entries)
        next_batch, next_active = _append_planner_upserts(step_index, entries[idx], upserts, dur)
        effective_active = next_active or bool(tasks_map) if next_batch is None else next_active
        return (next_batch, effective_active, started_at, False)
    if batch is None or batch.next_tool_idx >= batch.total_tools:
        return (batch, active, started_at, allow_incremental_reset)
    return (_apply_generic_step(entries[idx], batch, tasks_map, upserts), True, started_at, False)


def _process_transcript_entry(
    idx: int,
    entries: list[dict[str, Any]],
    context: tuple[int, bool],
    state: tuple[
        TranscriptToolBatchState | None,
        bool,
        str | None,
        list[ChatTimelineItemUpsert],
        dict[str, TranscriptBackgroundTask],
    ],
) -> tuple[TranscriptToolBatchState | None, bool, str | None, bool]:
    """Dispatch one parsed JSONL entry; trailing bool signals whether full reset is needed."""
    batch, active, started_at, upserts, tasks_map = state
    entry = entries[idx]
    step_type = str(entry.get("type") or "")
    source = str(entry.get("source") or "")
    result = (batch, active, started_at, False)

    if source == "USER_EXPLICIT" and step_type == "USER_INPUT":
        user_res = _apply_user_step(context[0], entry, upserts, started_at)
        result = (*user_res, False)
    elif source == "MODEL" and step_type in ("PLANNER_RESPONSE", "GENERIC"):
        result = _handle_model_entry(idx, entries, context, state)
    elif step_type == "ERROR_MESSAGE":
        err_res = _apply_error_step(context[0], entry, upserts)
        result = (*err_res, started_at, False)
    elif step_type == "SYSTEM_MESSAGE":
        sys_res = _apply_system_message_step(entry, context[1], tasks_map, upserts, active)
        result = (batch, sys_res[0], started_at, sys_res[1])
    elif step_type == "CHECKPOINT":
        result = (None, bool(tasks_map), started_at, False)
    return result


def read_transcript_delta(
    transcript_path: Path,
    *,
    start_offset: int,
    last_step_index: int | None,
    pending_batch: TranscriptToolBatchState | None = None,
    pending_tasks: Sequence[TranscriptBackgroundTask] = (),
    turn_started_at: str | None = None,
) -> TranscriptParseResult:
    """Parse newly appended ``transcript_full.jsonl`` lines starting at ``start_offset``."""
    entries, next_offset, truncated, _ = _extract_complete_lines(
        transcript_path,
        start_offset,
    )
    prev_step = last_step_index if last_step_index is not None else -1
    if truncated:
        return TranscriptParseResult(
            last_step_index=prev_step,
            next_offset=0,
            needs_reset=True,
            turn_started_at=turn_started_at,
        )

    upserts: list[ChatTimelineItemUpsert] = []
    tasks_map: dict[str, TranscriptBackgroundTask] = {t.task_id: t for t in pending_tasks}
    active = pending_batch is not None or bool(tasks_map)

    for idx, entry in enumerate(entries):
        raw_step = entry.get("step_index")
        if not isinstance(raw_step, int) or isinstance(raw_step, bool):
            continue
        if raw_step <= prev_step and not (
            raw_step == prev_step and str(entry.get("type") or "") == "GENERIC"
        ):
            return TranscriptParseResult(
                last_step_index=prev_step,
                next_offset=0,
                needs_reset=True,
                turn_started_at=turn_started_at,
            )
        prev_step = raw_step
        pending_batch, active, turn_started_at, needs_reset = _process_transcript_entry(
            idx,
            entries,
            (raw_step, start_offset > 0),
            (pending_batch, active, turn_started_at, upserts, tasks_map),
        )
        if needs_reset:
            return TranscriptParseResult(
                last_step_index=prev_step,
                next_offset=0,
                needs_reset=True,
                turn_started_at=turn_started_at,
            )

    return TranscriptParseResult(
        upserts=tuple(upserts),
        last_step_index=prev_step,
        next_offset=next_offset,
        needs_reset=False,
        pending_batch=pending_batch,
        pending_tasks=tuple(tasks_map.values()),
        turn_active=active,
        turn_started_at=turn_started_at,
    )


def format_chat_sync_notification(params: ChatSyncNotificationParams) -> str:
    """Serialize one ``ChatSyncNotificationParams`` payload into a JSON-RPC frame."""
    envelope = {
        "jsonrpc": "2.0",
        "method": "cbi/chat_sync",
        "params": params.model_dump(by_alias=True, exclude_none=True),
    }
    return json.dumps(envelope, separators=(",", ":"))


__all__ = [
    "MAX_TOOL_OUTPUT_CHARS",
    "classify_profile_watch_path",
    "clean_generic_tool_output",
    "extract_user_request_text",
    "format_chat_sync_notification",
    "read_transcript_delta",
    "resolve_transcript_path",
]
