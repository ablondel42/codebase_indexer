"""Per-connection ACP v1 prompt turn, subagent tree, root-set, and update coordinator."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from api.models.acp import (
    ACP_AUTH_REQUIRED,
    ClientCapabilities,
    PromptCapabilities,
    SessionStepOutcome,
)
from api.models.config import AcpServerConfig
from api.models.prompt import (
    ActivePromptTurn,
    AgentFrameDecision,
    SessionUpdateKind,
    SubagentSessionState,
)
from api.models.session import SessionRootSet
from api.services.client_dispatcher import ClientRequestDispatcher
from api.services.logging import AcpAuditLogger
from api.utils.errors import format_jsonrpc_error
from api.utils.jsonrpc import extract_request_id
from api.utils.prompt import (
    apply_subagent_update,
    build_cancelled_prompt_response,
    collect_descendant_session_ids,
    discard_descendant_live_state,
    is_session_update_allowed,
    prepare_cancel_params,
    prepare_cancel_request_params,
    prepare_prompt_params,
    strip_unadvertised_session_tool_content,
)
from api.utils.security import resolve_effective_root_set
from api.utils.session_state import filter_boolean_config_options
from core.models.exceptions import (
    AcpError,
    AcpInvalidParamsError,
    AcpInvalidRequestError,
    AcpResourceNotFoundError,
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
    """Tracks in-flight prompt turns, root sets, subagent trees, and bidirectional routing."""

    def __init__(
        self,
        connection_id: str,
        config: AcpServerConfig,
        audit: AcpAuditLogger,
        send_to_agent: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        """Initialize turn, root-set, subagent, and client-request dispatcher state."""
        self._connection_id = connection_id
        self._config = config
        self._audit = audit
        self._dispatcher = ClientRequestDispatcher(connection_id, config, audit, send_to_agent)
        self._active_turns: dict[str, ActivePromptTurn] = {}
        self._turn_by_req_id: dict[int | str, str] = {}
        self._root_sets: dict[str, SessionRootSet] = {}
        self._child_sessions: dict[str, SubagentSessionState] = {}
        self._loading_session_ids: set[str] = set()

    @property
    def active_prompt_session_ids(self) -> frozenset[str]:
        """Return session IDs with an active ``session/prompt`` turn in flight."""
        return frozenset(self._active_turns.keys())

    @property
    def pending_permission_ids(self) -> frozenset[int | str]:
        """Return JSON-RPC request IDs of unresolved ``session/request_permission`` calls."""
        return self._dispatcher.pending_permission_ids

    @property
    def pending_elicitation_ids(self) -> frozenset[int | str]:
        """Return JSON-RPC request IDs of unresolved ``elicitation/create`` calls."""
        return self._dispatcher.pending_elicitation_ids

    @property
    def child_session_ids(self) -> frozenset[str]:
        """Return announced subagent child session IDs on this connection."""
        return frozenset(self._child_sessions.keys())

    def is_child_session(self, session_id: str) -> bool:
        """Return ``True`` when ``session_id`` is an announced subagent child session."""
        return session_id in self._child_sessions

    def register_session_root_set(self, session_id: str, root_set: SessionRootSet) -> None:
        """Store or update the effective workspace root set for root ``session_id``."""
        self._root_sets[session_id] = root_set

    def resolve_root_set(self, session_id: str) -> SessionRootSet | None:
        """Resolve effective workspace root set for root or descendant child session."""
        return resolve_effective_root_set(session_id, self._root_sets, self._child_sessions)

    def begin_session_load(self, session_id: str, *, is_replay: bool) -> None:
        """Reset descendant live state on ``load``/``resume`` and mark replay boundary."""
        discard_descendant_live_state(self._child_sessions, session_id)
        if is_replay:
            self._loading_session_ids.add(session_id)

    def finish_session_load(self, session_id: str) -> None:
        """End historical replay boundary after ``session/load`` response."""
        self._loading_session_ids.discard(session_id)

    def session_id_for_prompt_request(self, req_id: int | str) -> str | None:
        """Return active turn ``sessionId`` matching ``req_id``, if any."""
        return self._turn_by_req_id.get(req_id)

    def _validate_prompt_session_state(
        self,
        req_id: int | str,
        session_id: str,
        active_session_ids: frozenset[str],
    ) -> None:
        """Enforce child-session restriction, active membership, and turn exclusivity."""
        if session_id in self._child_sessions:
            raise AcpInvalidParamsError("Child subagent session does not accept 'session/prompt'")
        if session_id not in active_session_ids:
            raise AcpResourceNotFoundError(
                "Session not found or not active on connection",
                wire_data={"sessionId": session_id},
            )
        if session_id in self._active_turns:
            raise AcpInvalidRequestError(
                f"A prompt turn is already in progress for session '{session_id}'"
            )

    def prepare_prompt(
        self,
        req_id: int | str | None,
        msg: Mapping[str, Any],
        active_session_ids: frozenset[str],
        prompt_caps: PromptCapabilities,
    ) -> dict[str, Any]:
        """Validate ``session/prompt``, verify session/capability gates, and register turn."""
        if "id" not in msg or req_id is None:
            raise AcpInvalidRequestError(
                "'session/prompt' must be a JSON-RPC request with a non-null id"
            )
        session_id, sanitized_params = prepare_prompt_params(msg.get("params"), prompt_caps)
        self._validate_prompt_session_state(req_id, session_id, active_session_ids)

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
    ) -> dict[str, Any] | None:
        """Validate ``session/cancel`` notification and mark any active turn cancelled."""
        if "id" in msg:
            raise AcpInvalidRequestError(
                "'session/cancel' must be a JSON-RPC notification without an id"
            )
        try:
            session_id, sanitized = prepare_cancel_params(msg.get("params"))
        except AcpError as exc:
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

    def prepare_cancel_request(self, msg: Mapping[str, Any]) -> dict[str, Any] | None:
        """Validate ``$/cancel_request`` notification and flag matching prompt turn."""
        if "id" in msg:
            raise AcpInvalidRequestError(
                "'$/cancel_request' must be a JSON-RPC notification without an id"
            )
        try:
            target_req_id, sanitized = prepare_cancel_request_params(msg.get("params"))
        except AcpError as exc:
            logger.warning(
                "Dropping malformed $/cancel_request on %s: %s",
                self._connection_id,
                exc,
            )
            return None

        session_id = self._turn_by_req_id.get(target_id := target_req_id)
        if session_id is not None and session_id in self._active_turns:
            self._active_turns[session_id].cancel_requested = True
        del target_id
        return {"jsonrpc": "2.0", "method": "$/cancel_request", "params": sanitized}

    def prepare_client_response(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        """Delegate Client -> Agent response correlation to ``ClientRequestDispatcher``."""
        return self._dispatcher.prepare_client_response(msg)

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
            raise AcpInvalidParamsError(f"Child subagent session does not accept '{method}'")

        if method in _ACTIVE_SESSION_REQUIRED_METHODS and "id" in msg and req_id is not None:
            if not isinstance(session_id, str) or not session_id:
                raise AcpInvalidParamsError(f"Invalid {method}")
            if session_id not in active_session_ids:
                raise AcpResourceNotFoundError(
                    "Session not found or not active on connection",
                    wire_data={"sessionId": session_id},
                )
        return None

    def _is_in_historical_replay(self, session_id: str) -> bool:
        """Return ``True`` when ``session_id`` or an ancestor is replaying ``session/load``."""
        if session_id in self._loading_session_ids:
            return True
        cursor: str | None = session_id
        visited: set[str] = set()
        while cursor is not None and cursor not in visited:
            visited.add(cursor)
            child = self._child_sessions.get(cursor)
            if child is None:
                break
            if child.parent_session_id in self._loading_session_ids:
                return True
            cursor = child.parent_session_id
        return False

    def _handle_agent_session_update(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        ws_caps: ClientCapabilities,
    ) -> AgentFrameDecision:
        """Enforce ``clientCapabilities`` gates, subagent limits, and replay semantics."""
        params = loaded.get("params")
        update = params.get("update") if isinstance(params, Mapping) else None
        update_kind = update.get("sessionUpdate") if isinstance(update, Mapping) else None
        if (
            not isinstance(params, Mapping)
            or not isinstance(update, Mapping)
            or not isinstance(update_kind, str)
        ):
            return AgentFrameDecision(forward_frame=raw_line)
        if not is_session_update_allowed(update_kind, ws_caps):
            logger.warning(
                "[%s] Suppressing unadvertised session/update '%s'",
                self._connection_id,
                update_kind,
            )
            return AgentFrameDecision(forward_frame="")
        if update_kind == SessionUpdateKind.SUBAGENT_UPDATE.value:
            raw_parent = params.get("sessionId")
            parent_id = raw_parent if isinstance(raw_parent, str) else ""
            if len(self._child_sessions) >= (
                self._config.limits.max_subagents_per_session
                * max(1, len(self._root_sets))
            ):
                child_id = update.get("sessionId")
                if isinstance(child_id, str) and child_id not in self._child_sessions:
                    self._audit.record("subagent_limit_exceeded", outcome="denied")
                    return AgentFrameDecision(forward_frame="")
            is_hist = self._is_in_historical_replay(parent_id)
            if not apply_subagent_update(
                self._child_sessions,
                parent_id,
                update,
                is_historical=is_hist,
            ):
                logger.warning(
                    "[%s] Suppressing invalid subagent_update on parent '%s'",
                    self._connection_id,
                    parent_id,
                )
                return AgentFrameDecision(forward_frame="")

        cleaned = strip_unadvertised_session_tool_content(raw_line, loaded, ws_caps)
        if update_kind == SessionUpdateKind.CONFIG_OPTION_UPDATE.value:
            cleaned = filter_boolean_config_options(cleaned, loaded, ws_caps)
        return AgentFrameDecision(forward_frame=cleaned)

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
        return AgentFrameDecision(forward_frame=raw_line)

    def inspect_agent_frame(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        ws_caps: ClientCapabilities,
    ) -> AgentFrameDecision:
        """Inspect one parsed agent JSON-RPC message and return bridge routing decision."""
        method = loaded.get("method")
        if method == "session/update" and "id" not in loaded:
            return self._handle_agent_session_update(raw_line, loaded, ws_caps)
        if isinstance(method, str) and "id" not in loaded:
            return self._dispatcher.inspect_agent_notification(raw_line, loaded, ws_caps)
        if isinstance(method, str) and "id" in loaded:
            return self._dispatcher.inspect_agent_request(
                raw_line,
                loaded,
                ws_caps,
                self.resolve_root_set,
            )
        resp_id = extract_request_id(loaded)
        if resp_id is not None and resp_id in self._turn_by_req_id:
            return self._handle_prompt_turn_response(raw_line, loaded, resp_id)
        return AgentFrameDecision(forward_frame=raw_line)

    def clear_completed_session(self, session_id: str) -> None:
        """Remove turn, root-set, and descendant tracking after ``close`` or ``delete``."""
        descendants = collect_descendant_session_ids(self._child_sessions, session_id)
        for child_id in descendants:
            self._child_sessions.pop(child_id, None)
        self._root_sets.pop(session_id, None)
        self._loading_session_ids.discard(session_id)
        turn = self._active_turns.pop(session_id, None)
        if turn is not None:
            self._turn_by_req_id.pop(turn.request_id, None)

    def _drain_active_turns(self, session_ids: set[str] | None) -> list[dict[str, Any]]:
        """Flag (and on full teardown pop) matching active turns, returning ``session/cancel``."""
        cancels: list[dict[str, Any]] = []
        for turn_sess, turn in list(self._active_turns.items()):
            if session_ids is not None and turn_sess not in session_ids:
                continue
            turn.cancel_requested = True
            if session_ids is None:
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

    async def drain_teardown_messages(
        self,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build cancellations, release host terminals, and return teardown frames."""
        target_ids: set[str] | None = None
        if session_id is not None:
            descendants = collect_descendant_session_ids(self._child_sessions, session_id)
            target_ids = {session_id, *descendants}
        replies = await self._dispatcher.teardown_sessions(target_ids)
        cancels = self._drain_active_turns(target_ids)
        if session_id is None:
            self._child_sessions.clear()
            self._root_sets.clear()
            self._loading_session_ids.clear()
        return [*replies, *cancels]


# Keep unused format_jsonrpc_error import out if unused; check!
del format_jsonrpc_error

__all__ = [
    "PromptTurnManager",
]
