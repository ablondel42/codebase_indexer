"""Stateless validation, capability-gating, and sanitization helpers for ACP prompt turns."""

import json
from collections.abc import Mapping
from typing import Any

from api.models.acp import ClientCapabilities, PromptCapabilities
from api.models.prompt import (
    CancelRequestParams,
    PendingPermissionRequest,
    PromptContentBlock,
    RequestPermissionResult,
    SelectedPermissionOutcome,
    SessionCancelParams,
    SessionPromptParams,
    SessionUpdateKind,
    StopReason,
    SubagentSessionState,
)
from api.utils.security import sanitize_meta_dict

_SUBAGENT_GATED_UPDATES: frozenset[str] = frozenset(
    {
        SessionUpdateKind.SUBAGENT_UPDATE.value,
        SessionUpdateKind.SESSION_MESSAGE.value,
        SessionUpdateKind.SESSION_MESSAGE_CHUNK.value,
    }
)
_PLAN_GATED_UPDATES: frozenset[str] = frozenset(
    {
        SessionUpdateKind.PLAN_UPDATE.value,
        SessionUpdateKind.PLAN_REMOVED.value,
    }
)
_COMPACTION_GATED_UPDATES: frozenset[str] = frozenset(
    {
        SessionUpdateKind.COMPACTION_UPDATE.value,
        SessionUpdateKind.COMPACTION_SUMMARY_CHUNK.value,
    }
)
_TOOL_UPDATE_KINDS: frozenset[str] = frozenset(
    {
        SessionUpdateKind.TOOL_CALL.value,
        SessionUpdateKind.TOOL_CALL_UPDATE.value,
    }
)


def check_prompt_block_capability(
    block: PromptContentBlock,
    caps: PromptCapabilities,
) -> str | None:
    """Return ``None`` when ``block`` is permitted by ``caps``, or a denial reason."""
    if block.type == "image" and not caps.image:
        return "Agent did not advertise promptCapabilities.image"
    if block.type == "audio" and not caps.audio:
        return "Agent did not advertise promptCapabilities.audio"
    if block.type == "resource" and not caps.embedded_context:
        return "Agent did not advertise promptCapabilities.embeddedContext"
    return None


def _scrub_nested_meta_field(target: dict[str, Any]) -> None:
    """Scrub ``_meta`` in-place on ``target``, dropping empty or forbidden metadata."""
    raw_meta = target.get("_meta")
    if not isinstance(raw_meta, Mapping):
        target.pop("_meta", None)
        return
    scrubbed = sanitize_meta_dict(raw_meta)
    if scrubbed is None:
        target.pop("_meta", None)
    else:
        target["_meta"] = scrubbed


def sanitize_content_block(block: PromptContentBlock) -> dict[str, Any]:
    """Serialize a validated ``PromptContentBlock`` with scrubbed nested ``_meta`` dicts."""
    dumped = block.model_dump(by_alias=True, exclude_none=True)
    _scrub_nested_meta_field(dumped)
    annotations = dumped.get("annotations")
    if isinstance(annotations, dict):
        _scrub_nested_meta_field(annotations)
    resource = dumped.get("resource")
    if isinstance(resource, dict):
        _scrub_nested_meta_field(resource)
    return dumped


def prepare_prompt_params(
    raw_params: object,
    caps: PromptCapabilities,
) -> tuple[str, dict[str, Any]]:
    """Validate ``session/prompt`` parameters, content capabilities, and ``_meta``."""
    if not isinstance(raw_params, Mapping):
        raise ValueError("Invalid session/prompt params: expected JSON object")
    parsed = SessionPromptParams.model_validate(raw_params)
    sanitized_blocks: list[dict[str, Any]] = []
    for block in parsed.prompt:
        cap_err = check_prompt_block_capability(block, caps)
        if cap_err is not None:
            raise ValueError(cap_err)
        sanitized_blocks.append(sanitize_content_block(block))

    payload: dict[str, Any] = {
        "sessionId": parsed.session_id,
        "prompt": sanitized_blocks,
    }
    scrubbed_meta = sanitize_meta_dict(parsed.meta)
    if scrubbed_meta is not None:
        payload["_meta"] = scrubbed_meta
    return parsed.session_id, payload


def prepare_cancel_params(raw_params: object) -> tuple[str, dict[str, Any]]:
    """Validate and scrub ``session/cancel`` notification parameters."""
    if not isinstance(raw_params, Mapping):
        raise ValueError("Invalid session/cancel params: expected JSON object")
    parsed = SessionCancelParams.model_validate(raw_params)
    payload: dict[str, Any] = {"sessionId": parsed.session_id}
    scrubbed_meta = sanitize_meta_dict(parsed.meta)
    if scrubbed_meta is not None:
        payload["_meta"] = scrubbed_meta
    return parsed.session_id, payload


def prepare_cancel_request_params(raw_params: object) -> tuple[int | str, dict[str, Any]]:
    """Validate and scrub ``$/cancel_request`` notification parameters."""
    if not isinstance(raw_params, Mapping):
        raise ValueError("Invalid $/cancel_request params: expected JSON object")
    parsed = CancelRequestParams.model_validate(raw_params)
    if isinstance(parsed.request_id, str) and not parsed.request_id:
        raise ValueError("requestId cannot be empty")
    payload: dict[str, Any] = {"requestId": parsed.request_id}
    scrubbed_meta = sanitize_meta_dict(parsed.meta)
    if scrubbed_meta is not None:
        payload["_meta"] = scrubbed_meta
    return parsed.request_id, payload


def scrub_outbound_message_meta(msg: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a JSON-RPC message dict while scrubbing ``params._meta`` and ``result._meta``."""
    copied = dict(msg)
    for section_key in ("params", "result"):
        section = copied.get(section_key)
        if isinstance(section, Mapping):
            cleaned_section = dict(section)
            _scrub_nested_meta_field(cleaned_section)
            copied[section_key] = cleaned_section
    return copied


def _validate_client_error_object(raw_err: object) -> dict[str, Any]:
    """Validate a JSON-RPC 2.0 ``error`` object returned by the client."""
    if not isinstance(raw_err, Mapping):
        raise ValueError("Invalid JSON-RPC error payload: expected object")
    code = raw_err.get("code")
    message = raw_err.get("message")
    if isinstance(code, bool) or not isinstance(code, int) or not isinstance(message, str):
        raise ValueError("Invalid JSON-RPC error code or message")
    out: dict[str, Any] = {"code": code, "message": message}
    if "data" in raw_err and raw_err["data"] is not None:
        out["data"] = raw_err["data"]
    return out


def validate_permission_response(
    msg: Mapping[str, Any],
    pending: PendingPermissionRequest,
) -> dict[str, Any]:
    """Validate and scrub a client response to ``session/request_permission``."""
    if "error" in msg:
        return {
            "jsonrpc": "2.0",
            "id": pending.request_id,
            "error": _validate_client_error_object(msg["error"]),
        }
    raw_result = msg.get("result")
    if not isinstance(raw_result, Mapping):
        raise ValueError("Invalid permission result: expected JSON object")
    parsed = RequestPermissionResult.model_validate(raw_result)
    if (
        isinstance(parsed.outcome, SelectedPermissionOutcome)
        and parsed.outcome.option_id not in pending.option_ids
    ):
        raise ValueError(
            f"Unknown permission optionId '{parsed.outcome.option_id}' "
            f"for request #{pending.request_id}"
        )
    result_payload = parsed.model_dump(by_alias=True, exclude_none=True)
    _scrub_nested_meta_field(result_payload)
    return {
        "jsonrpc": "2.0",
        "id": pending.request_id,
        "result": result_payload,
    }


def build_cancelled_permission_response(request_id: int | str) -> dict[str, Any]:
    """Build a JSON-RPC ``session/request_permission`` response with ``cancelled`` outcome."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"outcome": {"outcome": "cancelled"}},
    }


def build_cancelled_prompt_response(request_id: int | str) -> str:
    """Serialize a ``session/prompt`` response frame with ``stopReason='cancelled'``."""
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"stopReason": StopReason.CANCELLED.value},
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def is_session_update_allowed(update_kind: str, client_caps: ClientCapabilities) -> bool:
    """Return ``True`` when ``client_caps`` permits forwarding ``update_kind``."""
    if update_kind in _SUBAGENT_GATED_UPDATES:
        return client_caps.subagents is not None
    if update_kind in _PLAN_GATED_UPDATES:
        return client_caps.plan is not None
    if update_kind == SessionUpdateKind.NOTICE.value:
        return client_caps.session is not None and client_caps.session.notices is not None
    if update_kind in _COMPACTION_GATED_UPDATES:
        return client_caps.session is not None and client_caps.session.compaction is not None
    return True


def strip_unadvertised_session_tool_content(
    raw_line: str,
    loaded: Mapping[str, Any],
    client_caps: ClientCapabilities,
) -> str:
    """Strip ``type='session'`` tool-call content items when ``subagents`` is unadvertised."""
    if client_caps.subagents is not None:
        return raw_line
    params = loaded.get("params")
    update = params.get("update") if isinstance(params, Mapping) else None
    if not isinstance(update, Mapping) or update.get("sessionUpdate") not in _TOOL_UPDATE_KINDS:
        return raw_line
    content = update.get("content")
    if not isinstance(content, list):
        return raw_line

    filtered = [
        item
        for item in content
        if not (isinstance(item, Mapping) and item.get("type") == "session")
    ]
    if len(filtered) == len(content):
        return raw_line

    rewritten_update = {**update, "content": filtered}
    base_params = params if isinstance(params, Mapping) else {}
    rewritten_params = {**base_params, "update": rewritten_update}
    rewritten_msg = {**loaded, "params": rewritten_params}
    return json.dumps(rewritten_msg, separators=(",", ":"), ensure_ascii=False)


def is_agent_request_allowed(method: str, client_caps: ClientCapabilities) -> bool:
    """Return ``True`` when an agent-to-client JSON-RPC request method is allowed."""
    if method == "session/request_permission":
        return True
    if method == "elicitation/create":
        return client_caps.elicitation is not None
    return False


def _creates_parent_cycle(
    children: Mapping[str, SubagentSessionState],
    parent_id: str,
    child_id: str,
) -> bool:
    """Return ``True`` if linking ``parent_id -> child_id`` forms a session cycle."""
    cursor: str | None = parent_id
    visited: set[str] = set()
    while cursor is not None and cursor not in visited:
        if cursor == child_id:
            return True
        visited.add(cursor)
        parent_state = children.get(cursor)
        cursor = parent_state.parent_session_id if parent_state is not None else None
    return False


def apply_subagent_update(
    children: dict[str, SubagentSessionState],
    parent_id: str,
    update: Mapping[str, Any],
) -> bool:
    """Record or patch a child subagent session, returning ``False`` if invalid."""
    child_id = update.get("sessionId")
    if not isinstance(child_id, str) or not child_id or not parent_id:
        return False
    if _creates_parent_cycle(children, parent_id, child_id):
        return False
    existing = children.get(child_id)
    if existing is not None and existing.parent_session_id != parent_id:
        return False

    state = existing or SubagentSessionState(
        session_id=child_id,
        parent_session_id=parent_id,
    )
    if "capabilities" in update:
        raw_caps = update["capabilities"]
        state.can_cancel = isinstance(raw_caps, Mapping) and isinstance(
            raw_caps.get("cancel"),
            Mapping,
        )
    children[child_id] = state
    return True


__all__ = [
    "apply_subagent_update",
    "build_cancelled_permission_response",
    "build_cancelled_prompt_response",
    "check_prompt_block_capability",
    "is_agent_request_allowed",
    "is_session_update_allowed",
    "prepare_cancel_params",
    "prepare_cancel_request_params",
    "prepare_prompt_params",
    "sanitize_content_block",
    "scrub_outbound_message_meta",
    "strip_unadvertised_session_tool_content",
    "validate_permission_response",
]
