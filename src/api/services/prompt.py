"""Per-connection ACP v1 prompt turn, permission, cancellation, and subagent coordinator."""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from api.models.acp import (
    ACP_AUTH_REQUIRED,
    ACP_RESOURCE_NOT_FOUND,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    ClientCapabilities,
    PromptCapabilities,
    SessionStepOutcome,
)
from api.models.prompt import (
    ActivePromptTurn,
    AgentFrameDecision,
    PendingPermissionRequest,
    RequestPermissionParams,
    SessionPromptResult,
    SessionUpdateKind,
    SubagentSessionState,
)
from api.utils.jsonrpc import extract_request_id, format_jsonrpc_error
from api.utils.prompt import (
    apply_subagent_update,
    build_cancelled_permission_response,
    build_cancelled_prompt_response,
    is_agent_request_allowed,
    is_session_update_allowed,
    prepare_cancel_params,
    prepare_cancel_request_params,
    prepare_prompt_params,
    scrub_outbound_message_meta,
    strip_unadvertised_session_tool_content,
    validate_permission_response,
)
from core.utils.logging import get_logger

logger = get_logger("api.services.prompt")

_ACTIVE_SESSION_REQUIRED_METHODS: frozenset[str] = frozenset(
    {
        "session/set_mode",
        "session/set_config_option",
    }
)


class PromptTurnManager:
    """Tracks in-flight prompt turns, permission requests, and subagent child sessions."""

    def __init__(self, connection_id: str) -> None:
        """Initialize empty turn, permission, and child-session registries."""
        self._connection_id = connection_id
        self._active_turns: dict[str, ActivePromptTurn] = {}
        self._turn_by_req_id: dict[int | str, str] = {}
        self._pending_permissions: dict[int | str, PendingPermissionRequest] = {}
        self._pending_agent_requests: dict[int | str, tuple[str, str | None]] = {}
        self._child_sessions: dict[str, SubagentSessionState] = {}

    @property
    def active_prompt_session_ids(self) -> frozenset[str]:
        """Return session IDs with an active ``session/prompt`` turn in flight."""
        return frozenset(self._active_turns.keys())

    @property
    def pending_permission_ids(self) -> frozenset[int | str]:
        """Return JSON-RPC request IDs of unresolved ``session/request_permission`` calls."""
        return frozenset(self._pending_permissions.keys())

    @property
    def child_session_ids(self) -> frozenset[str]:
        """Return child session IDs announced via ``subagent_update``."""
        return frozenset(self._child_sessions.keys())

    def is_child_session(self, session_id: str) -> bool:
        """Return ``True`` when ``session_id`` is an announced subagent child session."""
        return session_id in self._child_sessions

    def _validate_prompt_session_state(
        self,
        req_id: int | str,
        session_id: str,
        active_session_ids: frozenset[str],
    ) -> SessionStepOutcome | None:
        """Enforce child-session restriction, active-session membership, and turn exclusivity."""
        if session_id in self._child_sessions:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        "Child subagent session does not accept 'session/prompt'",
                    ),
                )
            )
        if session_id not in active_session_ids:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        ACP_RESOURCE_NOT_FOUND,
                        "Session not found or not active on connection",
                        data={"sessionId": session_id},
                    ),
                )
            )
        if session_id in self._active_turns:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_REQUEST,
                        f"A prompt turn is already in progress for session '{session_id}'",
                    ),
                )
            )
        return None

    def prepare_prompt(
        self,
        req_id: int | str | None,
        msg: Mapping[str, Any],
        active_session_ids: frozenset[str],
        prompt_caps: PromptCapabilities,
    ) -> dict[str, Any] | SessionStepOutcome:
        """Validate ``session/prompt``, verify session/capability gates, and register turn."""
        if "id" not in msg or req_id is None:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        None,
                        JSONRPC_INVALID_REQUEST,
                        "'session/prompt' must be a JSON-RPC request with a non-null id",
                    ),
                )
            )
        try:
            session_id, sanitized_params = prepare_prompt_params(msg.get("params"), prompt_caps)
        except (PydanticValidationError, ValueError) as exc:
            logger.debug("Invalid session/prompt params on %s: %s", self._connection_id, exc)
            detail = str(exc) if isinstance(exc, ValueError) else "Invalid session/prompt params"
            return SessionStepOutcome(
                frames=(format_jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, detail),)
            )

        rejection = self._validate_prompt_session_state(req_id, session_id, active_session_ids)
        if rejection is not None:
            return rejection

        self._active_turns[session_id] = ActivePromptTurn(
            request_id=req_id,
            session_id=session_id,
        )
        self._turn_by_req_id[req_id] = session_id
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "session/prompt",
            "params": sanitized_params,
        }

    def _is_cancellable_session(
        self,
        session_id: str,
        active_session_ids: frozenset[str],
    ) -> bool:
        """Return ``True`` when ``session/cancel`` may be forwarded for ``session_id``."""
        child = self._child_sessions.get(session_id)
        if child is not None:
            return child.can_cancel
        return session_id in active_session_ids

    def prepare_cancel(
        self,
        msg: Mapping[str, Any],
        active_session_ids: frozenset[str],
    ) -> dict[str, Any] | SessionStepOutcome | None:
        """Validate ``session/cancel`` notification and mark any active turn cancelled."""
        if "id" in msg:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        extract_request_id(msg),
                        JSONRPC_INVALID_REQUEST,
                        "'session/cancel' must be a JSON-RPC notification without an id",
                    ),
                )
            )
        try:
            session_id, sanitized = prepare_cancel_params(msg.get("params"))
        except (PydanticValidationError, ValueError) as exc:
            logger.warning("Dropping malformed session/cancel on %s: %s", self._connection_id, exc)
            return None

        if not self._is_cancellable_session(session_id, active_session_ids):
            logger.debug(
                "[%s] Ignoring session/cancel for non-cancellable session '%s'",
                self._connection_id,
                session_id,
            )
            return None

        turn = self._active_turns.get(session_id)
        if turn is not None:
            turn.cancel_requested = True
        return {"jsonrpc": "2.0", "method": "session/cancel", "params": sanitized}

    def prepare_cancel_request(
        self,
        msg: Mapping[str, Any],
    ) -> dict[str, Any] | SessionStepOutcome | None:
        """Validate ``$/cancel_request`` notification and flag matching prompt turn."""
        if "id" in msg:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        extract_request_id(msg),
                        JSONRPC_INVALID_REQUEST,
                        "'$/cancel_request' must be a JSON-RPC notification without an id",
                    ),
                )
            )
        try:
            target_req_id, sanitized = prepare_cancel_request_params(msg.get("params"))
        except (PydanticValidationError, ValueError) as exc:
            logger.warning(
                "Dropping malformed $/cancel_request on %s: %s",
                self._connection_id,
                exc,
            )
            return None

        session_id = self._turn_by_req_id.get(target_req_id)
        if session_id is not None and session_id in self._active_turns:
            self._active_turns[session_id].cancel_requested = True
        return {"jsonrpc": "2.0", "method": "$/cancel_request", "params": sanitized}

    def prepare_client_response(
        self,
        msg: Mapping[str, Any],
    ) -> dict[str, Any] | SessionStepOutcome:
        """Correlate and validate a client JSON-RPC response to an agent-initiated request."""
        req_id = extract_request_id(msg)
        has_single_body = ("result" in msg) != ("error" in msg)
        if "id" not in msg or req_id is None or not has_single_body:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_REQUEST,
                        "Invalid JSON-RPC response envelope",
                    ),
                )
            )
        pending_entry = self._pending_agent_requests.get(req_id)
        if pending_entry is None:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        None,
                        JSONRPC_INVALID_REQUEST,
                        f"Unknown or already-resolved agent request id: {req_id!r}",
                    ),
                )
            )
        method_name, _ = pending_entry
        if method_name != "session/request_permission":
            self._pending_agent_requests.pop(req_id, None)
            return scrub_outbound_message_meta(msg)

        perm = self._pending_permissions[req_id]
        try:
            validated = validate_permission_response(msg, perm)
        except (PydanticValidationError, ValueError) as exc:
            logger.debug("Invalid permission response on %s: %s", self._connection_id, exc)
            detail = str(exc) if isinstance(exc, ValueError) else "Invalid permission response"
            return SessionStepOutcome(
                frames=(format_jsonrpc_error(None, JSONRPC_INVALID_PARAMS, detail),)
            )
        self._pending_permissions.pop(req_id, None)
        self._pending_agent_requests.pop(req_id, None)
        return validated

    def check_session_target_allowed(
        self,
        method: str,
        req_id: int | str | None,
        msg: Mapping[str, Any],
        active_session_ids: frozenset[str],
    ) -> SessionStepOutcome | None:
        """Reject prohibited child-session calls and inactive ``set_mode``/``set_config_option``."""
        raw_params = msg.get("params")
        session_id = raw_params.get("sessionId") if isinstance(raw_params, Mapping) else None
        if isinstance(session_id, str) and session_id in self._child_sessions:
            if "id" not in msg:
                logger.warning(
                    "[%s] Dropping forbidden notification '%s' targeting child session '%s'",
                    self._connection_id,
                    method,
                    session_id,
                )
                return SessionStepOutcome()
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        f"Child subagent session does not accept '{method}'",
                    ),
                )
            )
        if method in _ACTIVE_SESSION_REQUIRED_METHODS and "id" in msg and req_id is not None:
            if not isinstance(session_id, str) or not session_id:
                return SessionStepOutcome(
                    frames=(
                        format_jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, f"Invalid {method}"),
                    )
                )
            if session_id not in active_session_ids:
                return SessionStepOutcome(
                    frames=(
                        format_jsonrpc_error(
                            req_id,
                            ACP_RESOURCE_NOT_FOUND,
                            "Session not found or not active on connection",
                            data={"sessionId": session_id},
                        ),
                    )
                )
        return None

    def _handle_agent_session_update(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        client_caps: ClientCapabilities,
    ) -> AgentFrameDecision:
        """Enforce ``clientCapabilities`` gates and track ``subagent_update`` state."""
        params = loaded.get("params")
        update = params.get("update") if isinstance(params, Mapping) else None
        update_kind = update.get("sessionUpdate") if isinstance(update, Mapping) else None
        if (
            not isinstance(params, Mapping)
            or not isinstance(update, Mapping)
            or not isinstance(update_kind, str)
        ):
            return AgentFrameDecision(forward_frame=raw_line)
        if not is_session_update_allowed(update_kind, client_caps):
            logger.warning(
                "[%s] Suppressing unadvertised session/update '%s'",
                self._connection_id,
                update_kind,
            )
            return AgentFrameDecision(forward_frame="")
        if update_kind == SessionUpdateKind.SUBAGENT_UPDATE.value:
            raw_parent = params.get("sessionId")
            parent_id = raw_parent if isinstance(raw_parent, str) else ""
            if not apply_subagent_update(self._child_sessions, parent_id, update):
                logger.warning(
                    "[%s] Suppressing invalid subagent_update on parent '%s'",
                    self._connection_id,
                    parent_id,
                )
                return AgentFrameDecision(forward_frame="")
        cleaned = strip_unadvertised_session_tool_content(raw_line, loaded, client_caps)
        return AgentFrameDecision(forward_frame=cleaned)

    def _register_permission_request(
        self,
        raw_line: str,
        req_id: int | str,
        raw_params: object,
    ) -> AgentFrameDecision:
        """Validate and track an incoming ``session/request_permission`` call from ``agy``."""
        if not isinstance(raw_params, Mapping):
            err = json.loads(
                format_jsonrpc_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    "Invalid session/request_permission params",
                )
            )
            return AgentFrameDecision(forward_frame="", agent_reply=err)
        try:
            parsed = RequestPermissionParams.model_validate(raw_params)
        except PydanticValidationError as exc:
            logger.warning(
                "[%s] Rejecting malformed session/request_permission (#%s): %s",
                self._connection_id,
                req_id,
                exc,
            )
            err = json.loads(
                format_jsonrpc_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    "Invalid session/request_permission params",
                )
            )
            return AgentFrameDecision(forward_frame="", agent_reply=err)

        option_ids = frozenset(opt.option_id for opt in parsed.options)
        self._pending_permissions[req_id] = PendingPermissionRequest(
            request_id=req_id,
            session_id=parsed.session_id,
            option_ids=option_ids,
        )
        self._pending_agent_requests[req_id] = ("session/request_permission", parsed.session_id)
        return AgentFrameDecision(forward_frame=raw_line)

    def _handle_agent_request(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        method: str,
        client_caps: ClientCapabilities,
    ) -> AgentFrameDecision:
        """Gate agent-to-client requests against clamped ``ClientCapabilities``."""
        req_id = extract_request_id(loaded)
        if req_id is None:
            logger.warning(
                "[%s] Dropping agent request '%s' with invalid id",
                self._connection_id,
                method,
            )
            return AgentFrameDecision(forward_frame="")
        if not is_agent_request_allowed(method, client_caps):
            logger.warning(
                "[%s] Rejecting disallowed agent request '%s' (#%s)",
                self._connection_id,
                method,
                req_id,
            )
            err = json.loads(
                format_jsonrpc_error(
                    req_id,
                    JSONRPC_METHOD_NOT_FOUND,
                    f"Client did not advertise support for '{method}'",
                )
            )
            return AgentFrameDecision(forward_frame="", agent_reply=err)
        if method == "session/request_permission":
            return self._register_permission_request(raw_line, req_id, loaded.get("params"))

        raw_params = loaded.get("params")
        sess_id = (
            raw_params.get("sessionId")
            if isinstance(raw_params, Mapping) and isinstance(raw_params.get("sessionId"), str)
            else None
        )
        self._pending_agent_requests[req_id] = (method, sess_id)
        return AgentFrameDecision(forward_frame=raw_line)

    def _handle_prompt_turn_response(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        resp_id: int | str,
    ) -> AgentFrameDecision:
        """Complete a tracked ``session/prompt`` turn and normalize post-cancel abort errors."""
        session_id = self._turn_by_req_id.pop(resp_id)
        turn = self._active_turns.pop(session_id, None)
        if "error" in loaded:
            err_obj = loaded.get("error")
            err_code = err_obj.get("code") if isinstance(err_obj, Mapping) else None
            if turn is not None and turn.cancel_requested and err_code != ACP_AUTH_REQUIRED:
                logger.info(
                    "[%s] Normalizing aborted prompt response #%s (code=%s) to cancelled",
                    self._connection_id,
                    resp_id,
                    err_code,
                )
                return AgentFrameDecision(forward_frame=build_cancelled_prompt_response(resp_id))
            return AgentFrameDecision(forward_frame=raw_line)

        raw_result = loaded.get("result")
        if isinstance(raw_result, Mapping):
            try:
                SessionPromptResult.model_validate(raw_result)
            except PydanticValidationError as exc:
                logger.warning(
                    "[%s] Non-standard session/prompt result on #%s: %s",
                    self._connection_id,
                    resp_id,
                    exc,
                )
        return AgentFrameDecision(forward_frame=raw_line)

    def inspect_agent_frame(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        client_caps: ClientCapabilities,
    ) -> AgentFrameDecision:
        """Inspect one parsed ``agy`` JSON-RPC message and return bridge forwarding decision."""
        method = loaded.get("method")
        if method == "session/update" and "id" not in loaded:
            return self._handle_agent_session_update(raw_line, loaded, client_caps)
        if isinstance(method, str) and "id" in loaded:
            return self._handle_agent_request(raw_line, loaded, method, client_caps)
        resp_id = extract_request_id(loaded)
        if resp_id is not None and resp_id in self._turn_by_req_id:
            return self._handle_prompt_turn_response(raw_line, loaded, resp_id)
        return AgentFrameDecision(forward_frame=raw_line)

    def clear_completed_session(self, session_id: str) -> None:
        """Remove any leftover turn tracking after ``session/close`` or ``session/delete``."""
        turn = self._active_turns.pop(session_id, None)
        if turn is not None:
            self._turn_by_req_id.pop(turn.request_id, None)

    def _drain_pending_requests(self, session_id: str | None) -> list[dict[str, Any]]:
        """Pop matching permission and agent requests, returning ``cancelled`` replies."""
        replies: list[dict[str, Any]] = []
        for req_id, perm in list(self._pending_permissions.items()):
            if session_id is None or perm.session_id == session_id:
                self._pending_permissions.pop(req_id, None)
                self._pending_agent_requests.pop(req_id, None)
                replies.append(build_cancelled_permission_response(req_id))
        for req_id, (_, req_sess) in list(self._pending_agent_requests.items()):
            if session_id is None or req_sess == session_id:
                self._pending_agent_requests.pop(req_id, None)
        return replies

    def _drain_active_turns(self, session_id: str | None) -> list[dict[str, Any]]:
        """Flag (and on full teardown pop) matching active turns, returning ``session/cancel``."""
        cancels: list[dict[str, Any]] = []
        for turn_sess, turn in list(self._active_turns.items()):
            if session_id is not None and turn_sess != session_id:
                continue
            turn.cancel_requested = True
            if session_id is None:
                self._active_turns.pop(turn_sess, None)
                self._turn_by_req_id.pop(turn.request_id, None)
            cancels.append(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": turn_sess},
                }
            )
        return cancels

    def drain_teardown_messages(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Build ``cancelled`` permission replies and ``session/cancel`` frames for teardown."""
        outbound = [
            *self._drain_pending_requests(session_id),
            *self._drain_active_turns(session_id),
        ]
        if session_id is None:
            self._child_sessions.clear()
        return outbound


__all__ = [
    "PromptTurnManager",
]
