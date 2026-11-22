"""Pure ACP v1 connection state machine, capability negotiation, and agent bridge."""

import json
import time
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from api.models.acp import (
    ACP_AUTH_REQUIRED,
    ACP_RESOURCE_NOT_FOUND,
    SUPPORTED_ACP_VERSIONS,
    AcpAuthState,
    AcpConnectionState,
    AgentCapabilities,
    AuthenticateParams,
    ClientCapabilities,
    InitializeParams,
    InitializeResult,
    LogoutParams,
    PromptCapabilities,
    SessionStepOutcome,
)
from api.models.config import AcpServerConfig, LogFormat
from api.models.session import SessionRootSet, SessionRuntimeState
from api.services.logging import AcpAuditLogger, AcpLogContext
from api.services.process import AcpSubprocessTransport
from api.services.prompt import PromptTurnManager
from api.utils.errors import exception_to_jsonrpc_frame, pydantic_error_to_acp_error
from api.utils.formatting import (
    describe_jsonrpc_frame,
    format_raw_frame_for_log,
    indent_json_payload,
)
from api.utils.jsonrpc import extract_request_id, parse_client_frame
from api.utils.prompt import is_valid_extension_method, scrub_outbound_message_meta
from api.utils.security import (
    extract_traceparent,
    find_advertised_auth_method,
    negotiate_client_capabilities,
    sanitize_authenticate_params,
    sanitize_meta_dict,
)
from api.utils.session import SESSION_METHODS, prepare_session_params
from api.utils.session_state import (
    apply_config_options_result,
    apply_mode_change,
    apply_session_setup_result,
    apply_session_update_to_state,
    filter_boolean_config_options,
    prepare_set_config_option_params,
    prepare_set_mode_params,
)
from core.models.exceptions import (
    AcpError,
    AcpInvalidParamsError,
    AcpInvalidRequestError,
    AcpMethodNotFoundError,
    AcpTransportError,
)
from core.utils.logging import get_logger

logger = get_logger("api.services.session")


class AcpConnectionSession:
    """Enforces ACP v1 initialization, capability gates, security policy, and bridging."""

    def __init__(
        self,
        connection_id: str,
        config: AcpServerConfig,
        *,
        process: AcpSubprocessTransport | None = None,
        audit: AcpAuditLogger | None = None,
    ) -> None:
        """Create a new uninitialized session bound to ``connection_id``."""
        self._connection_id = connection_id
        self._config = config
        self._audit = (
            audit
            if audit is not None
            else AcpAuditLogger(enabled=config.logging.log_audit_events)
        )
        self._process = process if process is not None else AcpSubprocessTransport(config)
        self._state = AcpConnectionState.UNINITIALIZED
        self._initialize_result: InitializeResult | None = None
        self._agent_capabilities = ClientCapabilities()
        self._ws_client_capabilities = ClientCapabilities()
        self._auth_state = AcpAuthState.UNKNOWN
        self._active_auth_method_id: str | None = None
        self._active_session_ids: set[str] = set()
        self._session_states: dict[str, SessionRuntimeState] = {}
        self._pending_rpc_methods: dict[int | str, tuple[str, str | None, float]] = {}
        self._pending_root_sets: dict[int | str, SessionRootSet] = {}
        self._pending_mode_targets: dict[int | str, str] = {}
        self._turns = PromptTurnManager(
            connection_id,
            config,
            self._audit,
            self._send_bridge_payload_to_agent,
        )

    @property
    def connection_id(self) -> str:
        """Return server-assigned connection identifier for this WebSocket."""
        return self._connection_id

    @property
    def state(self) -> AcpConnectionState:
        """Return current connection lifecycle state."""
        return self._state

    @property
    def initialize_result(self) -> InitializeResult | None:
        """Return validated ``InitializeResult`` once handshake reaches ``READY``."""
        return self._initialize_result

    @property
    def client_capabilities(self) -> ClientCapabilities:
        """Return effective agent-facing ``ClientCapabilities`` negotiated during ``initialize``."""
        return self._agent_capabilities

    @property
    def ws_client_capabilities(self) -> ClientCapabilities:
        """Return effective capabilities advertised by the connected WebSocket client."""
        return self._ws_client_capabilities

    @property
    def auth_state(self) -> AcpAuthState:
        """Return current ACP authentication state observed on this connection."""
        return self._auth_state

    @property
    def active_auth_method_id(self) -> str | None:
        """Return ``methodId`` used for the active authenticated state, if known."""
        return self._active_auth_method_id

    @property
    def active_session_ids(self) -> frozenset[str]:
        """Return set of root session IDs currently active on this connection."""
        return frozenset(self._active_session_ids)

    @property
    def active_prompt_session_ids(self) -> frozenset[str]:
        """Return session IDs currently executing a ``session/prompt`` turn."""
        return self._turns.active_prompt_session_ids

    @property
    def pending_permission_ids(self) -> frozenset[int | str]:
        """Return pending ``session/request_permission`` JSON-RPC request IDs."""
        return self._turns.pending_permission_ids

    @property
    def pending_elicitation_ids(self) -> frozenset[int | str]:
        """Return pending ``elicitation/create`` JSON-RPC request IDs."""
        return self._turns.pending_elicitation_ids

    @property
    def child_session_ids(self) -> frozenset[str]:
        """Return announced subagent child session IDs on this connection."""
        return self._turns.child_session_ids

    @property
    def session_states(self) -> Mapping[str, SessionRuntimeState]:
        """Return read-only mapping of collected ``SessionRuntimeState`` by ``sessionId``."""
        return dict(self._session_states)

    def get_session_state(self, session_id: str) -> SessionRuntimeState | None:
        """Return collected ``SessionRuntimeState`` for ``session_id``, or ``None``."""
        return self._session_states.get(session_id)

    def _is_debug_errors(self) -> bool:
        """Expose detailed internal error text only in DEV pretty-log DEBUG mode."""
        return (
            self._config.logging.format == LogFormat.PRETTY
            and self._config.logging.level == "DEBUG"
        )

    def _error_outcome(
        self,
        exc: AcpError,
        req_id: int | str | None,
        *,
        is_notification: bool = False,
        close_code: int | None = None,
    ) -> SessionStepOutcome:
        """Map an ``AcpError`` to a logged ``SessionStepOutcome`` respecting silence rules."""
        if exc.audit_event:
            self._audit.record(
                exc.audit_event,
                outcome="blocked",
                details={"error_code": exc.error_code, "message": exc.message},
            )
        frame = exception_to_jsonrpc_frame(
            exc,
            req_id,
            is_notification=is_notification,
            debug=self._is_debug_errors(),
        )
        if frame is None:
            return SessionStepOutcome(close_code=close_code)
        label, pretty = format_raw_frame_for_log(frame)
        logger.warning(
            "[%s] Bridge -> UI (%s)\n%s",
            self._connection_id,
            label,
            pretty,
            extra={"jsonrpc_code": exc.jsonrpc_code},
        )
        return SessionStepOutcome(frames=(frame,), close_code=close_code)

    def _log_inbound_frame(self, msg: Mapping[str, Any]) -> None:
        """Log inbound client JSON-RPC frame respecting ``effective_log_payloads``."""
        label = describe_jsonrpc_frame(msg)
        if self._config.logging.effective_log_payloads:
            logger.info(
                "[%s] UI -> Bridge (%s)\n%s",
                self._connection_id,
                label,
                indent_json_payload(msg),
            )
        else:
            logger.info("[%s] UI -> Bridge (%s)", self._connection_id, label)

    def _log_outbound_agent_frame(self, payload: Mapping[str, Any]) -> None:
        """Log outbound bridge->agent JSON-RPC frame respecting ``effective_log_payloads``."""
        label = describe_jsonrpc_frame(payload)
        if self._config.logging.effective_log_payloads:
            logger.info(
                "[%s] Bridge -> Agent (%s)\n%s",
                self._connection_id,
                label,
                indent_json_payload(payload),
            )
        else:
            logger.info("[%s] Bridge -> Agent (%s)", self._connection_id, label)

    def _parse_init_request(self, msg: Mapping[str, Any]) -> tuple[int | str, InitializeParams]:
        """Validate pre-initialization request ordering and ``InitializeParams``."""
        req_id = extract_request_id(msg)
        if self._state != AcpConnectionState.UNINITIALIZED:
            raise AcpInvalidRequestError(
                "Connection initialization already in progress or completed"
            )
        if msg.get("method") != "initialize":
            if "id" not in msg:
                raise AcpInvalidRequestError("Notification rejected before initialize")
            raise AcpInvalidRequestError(
                "Connection not initialized; first request must be 'initialize'"
            )
        if "id" not in msg or req_id is None:
            raise AcpInvalidRequestError(
                "'initialize' must be a JSON-RPC request with a non-null id"
            )
        raw_params = msg.get("params")
        if not isinstance(raw_params, Mapping):
            raise AcpInvalidParamsError("Invalid initialize params: expected JSON object")
        try:
            return req_id, InitializeParams.model_validate(raw_params)
        except PydanticValidationError as exc:
            raise pydantic_error_to_acp_error(exc, label="initialize") from exc

    async def _await_agent_init_response(
        self,
        req_id: int | str,
    ) -> tuple[str, dict[str, Any]]:
        """Read NDJSON lines from subprocess stdout until matching ``initialize`` response."""
        while True:
            raw_line = await self._process.read_line()
            if raw_line is None:
                raise AcpTransportError("ACP agent closed stdout before responding to initialize.")
            if not raw_line:
                continue
            try:
                loaded = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise AcpTransportError(
                    f"ACP agent returned invalid JSON during initialize: {exc}"
                ) from exc
            if isinstance(loaded, dict) and loaded.get("id") == req_id:
                return raw_line, loaded

    async def _finalize_init_response(
        self,
        req_id: int | str,
        raw_line: str,
        agent_msg: Mapping[str, Any],
    ) -> SessionStepOutcome:
        """Validate agent initialize response, enforce version match, and enter ``READY``."""
        label, pretty = format_raw_frame_for_log(raw_line)
        if "error" in agent_msg or "result" not in agent_msg:
            logger.warning("[%s] Agent -> UI (%s)\n%s", self._connection_id, label, pretty)
            await self.close()
            return SessionStepOutcome(frames=(raw_line,), close_code=1002)
        try:
            parsed_result = InitializeResult.model_validate(agent_msg["result"])
        except PydanticValidationError as exc:
            await self.close()
            raise AcpTransportError(
                f"ACP agent returned malformed initialize result: {exc}"
            ) from exc

        if parsed_result.protocol_version not in SUPPORTED_ACP_VERSIONS:
            await self.close()
            return self._error_outcome(
                AcpInvalidParamsError(
                    f"Unsupported ACP protocolVersion: {parsed_result.protocol_version}"
                ),
                req_id,
                close_code=1002,
            )

        self._initialize_result = parsed_result
        self._state = AcpConnectionState.READY
        logger.info("[%s] Agent -> UI (%s · initialize)", self._connection_id, label)
        return SessionStepOutcome(frames=(raw_line,))

    async def _execute_agent_initialize(
        self,
        req_id: int | str,
        init_params: InitializeParams,
    ) -> SessionStepOutcome:
        """Lazily spawn subprocess, forward negotiated ``initialize``, and validate reply."""
        self._state = AcpConnectionState.INITIALIZING
        await self._process.start()
        sanitized_params, agent_caps, ws_caps = negotiate_client_capabilities(
            init_params,
            self._config.capabilities,
        )
        outbound = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "initialize",
            "params": sanitized_params,
        }
        self._log_outbound_agent_frame(outbound)
        await self._process.send_line(outbound)
        raw_line, agent_msg = await self._await_agent_init_response(req_id)
        outcome = await self._finalize_init_response(req_id, raw_line, agent_msg)
        if self._state == AcpConnectionState.READY:
            self._agent_capabilities = agent_caps
            self._ws_client_capabilities = ws_caps
        return outcome

    async def handle_init_frame(self, raw_text: str) -> SessionStepOutcome:
        """Process one incoming WebSocket frame while waiting for ``initialize``."""
        msg, parse_err = parse_client_frame(
            raw_text,
            max_bytes=self._config.transport.max_frame_bytes,
        )
        if parse_err is not None or msg is None:
            label, pretty = format_raw_frame_for_log(parse_err or "")
            logger.warning("[%s] Bridge -> UI (%s)\n%s", self._connection_id, label, pretty)
            return SessionStepOutcome(frames=(parse_err or "",))

        req_id = extract_request_id(msg)
        traceparent = extract_traceparent(
            msg.get("params", {}).get("_meta") if isinstance(msg.get("params"), Mapping) else None
        )
        with AcpLogContext.bind(
            connection_id=self._connection_id,
            request_id=req_id,
            method=msg.get("method") if isinstance(msg.get("method"), str) else None,
            traceparent=traceparent,
        ):
            self._log_inbound_frame(msg)
            try:
                req_id_val, init_params = self._parse_init_request(msg)
            except AcpError as exc:
                logger.debug("[%s] Invalid initialize frame: %s", self._connection_id, exc.message)
                is_notif = "method" in msg and "id" not in msg
                return self._error_outcome(exc, req_id, is_notification=is_notif)
            return await self._execute_agent_initialize(req_id_val, init_params)

    def _register_pending_rpc(
        self,
        req_id: int | str,
        method: str,
        target_id: str | None,
    ) -> None:
        """Track an in-flight Client -> Agent RPC up to ``max_pending_client_requests``."""
        if req_id in self._pending_rpc_methods:
            raise AcpInvalidRequestError(f"Duplicate in-flight request id: {req_id!r}")
        if len(self._pending_rpc_methods) >= self._config.limits.max_pending_client_requests:
            self._audit.record("pending_client_limit_exceeded", outcome="denied")
            raise AcpInvalidRequestError("Maximum concurrent in-flight requests exceeded")
        self._pending_rpc_methods[req_id] = (method, target_id, time.monotonic())

    def _prepare_authenticate_outbound(
        self,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate ``authenticate`` request against advertised ``authMethods``."""
        if "id" not in msg or req_id is None:
            raise AcpInvalidRequestError("'authenticate' must be a JSON-RPC request with an id")
        raw_params = msg.get("params")
        if not isinstance(raw_params, Mapping):
            raise AcpInvalidParamsError("Invalid authenticate params: expected JSON object")
        try:
            auth_params = AuthenticateParams.model_validate(raw_params)
        except PydanticValidationError as exc:
            raise pydantic_error_to_acp_error(exc, label="authenticate") from exc

        advertised = self._initialize_result.auth_methods if self._initialize_result else ()
        matched = find_advertised_auth_method(auth_params.method_id, advertised)
        if matched is None:
            raise AcpInvalidParamsError(
                f"Unknown authentication methodId: '{auth_params.method_id}'"
            )
        if matched.type == "terminal":
            raise AcpInvalidParamsError(
                f"Terminal auth method '{auth_params.method_id}' cannot use 'authenticate'"
            )

        self._register_pending_rpc(req_id, "authenticate", auth_params.method_id)
        self._auth_state = AcpAuthState.AUTHENTICATING
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "authenticate",
            "params": sanitize_authenticate_params(auth_params),
        }

    def _prepare_logout_outbound(
        self,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Enforce ``agentCapabilities.auth.logout`` capability gate before ``logout``."""
        if "id" not in msg or req_id is None:
            raise AcpInvalidRequestError("'logout' must be a JSON-RPC request with an id")
        auth_caps = (
            self._initialize_result.agent_capabilities.auth if self._initialize_result else None
        )
        if auth_caps is None or auth_caps.logout is None:
            raise AcpMethodNotFoundError("Agent did not advertise agentCapabilities.auth.logout")
        raw_params = msg.get("params", {})
        if raw_params is not None and not isinstance(raw_params, Mapping):
            raise AcpInvalidParamsError("Invalid logout params: expected JSON object")
        try:
            logout_params = LogoutParams.model_validate(raw_params or {})
        except PydanticValidationError as exc:
            raise pydantic_error_to_acp_error(exc, label="logout") from exc
        scrubbed = sanitize_meta_dict(logout_params.meta)
        params_payload: dict[str, Any] = {"_meta": scrubbed} if scrubbed is not None else {}
        self._register_pending_rpc(req_id, "logout", None)
        return {"jsonrpc": "2.0", "id": req_id, "method": "logout", "params": params_payload}

    def _prepare_session_outbound(
        self,
        method: str,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Enforce capability, session limits, MCP policy, and params for ``session/*``."""
        if "id" not in msg or req_id is None:
            raise AcpInvalidRequestError(f"'{method}' must be a JSON-RPC request with an id")
        self._turns.check_session_target_allowed(method, req_id, msg, self.active_session_ids)
        caps = (
            self._initialize_result.agent_capabilities
            if self._initialize_result is not None
            else AgentCapabilities()
        )
        max_sessions = self._config.limits.max_active_sessions_per_connection
        if (
            method in ("session/new", "session/load", "session/resume")
            and len(self._active_session_ids) >= max_sessions
        ):
            raw_p = msg.get("params")
            cand_id = raw_p.get("sessionId") if isinstance(raw_p, Mapping) else None
            if not (isinstance(cand_id, str) and cand_id in self._active_session_ids):
                self._audit.record("session_limit_exceeded", outcome="denied")
                raise AcpInvalidRequestError("Maximum active sessions per connection exceeded")

        sanitized, root_set = prepare_session_params(method, msg.get("params"), caps, self._config)
        target_session = sanitized.get("sessionId")
        tracked_id = target_session if isinstance(target_session, str) else None
        self._register_pending_rpc(req_id, method, tracked_id)
        if root_set is not None:
            self._pending_root_sets[req_id] = root_set
        if method in ("session/load", "session/resume") and tracked_id is not None:
            self._turns.begin_session_load(tracked_id, is_replay=(method == "session/load"))
        return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": sanitized}

    def _prepare_prompt_or_cancel(
        self,
        method: str,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Dispatch ``session/prompt``, ``session/cancel``, and ``$/cancel_request`` frames."""
        if method == "session/prompt":
            prompt_caps = (
                self._initialize_result.agent_capabilities.prompt_capabilities
                if self._initialize_result is not None
                else PromptCapabilities()
            )
            return self._turns.prepare_prompt(
                req_id,
                msg,
                self.active_session_ids,
                prompt_caps,
            )
        if method == "session/cancel":
            return self._turns.prepare_cancel(msg, self.active_session_ids)
        return self._turns.prepare_cancel_request(msg)

    def _prepare_config_or_mode_outbound(
        self,
        method: str,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate ``session/set_config_option`` or ``session/set_mode`` against state."""
        if "id" not in msg or req_id is None:
            raise AcpInvalidRequestError(f"'{method}' must be a JSON-RPC request with an id")
        self._turns.check_session_target_allowed(method, req_id, msg, self.active_session_ids)
        raw_params = msg.get("params")
        raw_sid = raw_params.get("sessionId") if isinstance(raw_params, Mapping) else None
        current_state = self._session_states.get(raw_sid) if isinstance(raw_sid, str) else None
        allow_bool = bool(
            self._ws_client_capabilities.session is not None
            and self._ws_client_capabilities.session.config_options is not None
            and self._ws_client_capabilities.session.config_options.boolean is not None
        )
        session_id, sanitized = (
            prepare_set_config_option_params(raw_params, current_state, allow_boolean=allow_bool)
            if method == "session/set_config_option"
            else prepare_set_mode_params(raw_params, current_state)
        )
        self._register_pending_rpc(req_id, method, session_id)
        if method == "session/set_mode":
            self._pending_mode_targets[req_id] = str(sanitized["modeId"])
        return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": sanitized}

    def _dispatch_extension_method(
        self,
        method: str,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Gate and scrub ``_*`` extension requests and notifications."""
        if not is_valid_extension_method(method, self._config.capabilities):
            self._audit.record("extension_denied", outcome="denied", details={"method": method})
            if "id" in msg and req_id is not None:
                raise AcpMethodNotFoundError("Method not found")
            return None
        if "id" in msg and req_id is not None:
            self._register_pending_rpc(req_id, method, None)
        return scrub_outbound_message_meta(msg)

    def _dispatch_ready_method(
        self,
        method: str,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Route a validated post-initialization JSON-RPC method or notification."""
        handlers: dict[str, Any] = {
            "authenticate": lambda: self._prepare_authenticate_outbound(req_id, msg),
            "logout": lambda: self._prepare_logout_outbound(req_id, msg),
            "session/prompt": lambda: self._prepare_prompt_or_cancel(method, req_id, msg),
            "session/cancel": lambda: self._prepare_prompt_or_cancel(method, req_id, msg),
            "$/cancel_request": lambda: self._prepare_prompt_or_cancel(method, req_id, msg),
            "session/set_config_option": lambda: self._prepare_config_or_mode_outbound(
                method, req_id, msg
            ),
            "session/set_mode": lambda: self._prepare_config_or_mode_outbound(
                method, req_id, msg
            ),
        }
        handler = handlers.get(method)
        if handler is not None:
            return handler()
        if method in SESSION_METHODS:
            return self._prepare_session_outbound(method, req_id, msg)
        if method.startswith("_"):
            return self._dispatch_extension_method(method, req_id, msg)
        if "id" in msg and req_id is not None:
            raise AcpMethodNotFoundError(f"Unsupported ACP method: '{method}'")
        logger.debug("[%s] Ignoring unknown client notification '%s'", self._connection_id, method)
        return None

    def _prepare_ready_outbound(self, msg: Mapping[str, Any]) -> dict[str, Any] | None:
        """Dispatch post-init validation for requests, notifications, and client responses."""
        if "method" not in msg:
            return self._turns.prepare_client_response(msg)
        req_id = extract_request_id(msg)
        method = str(msg.get("method"))
        if method == "initialize":
            raise AcpInvalidRequestError("Connection already initialized")
        return self._dispatch_ready_method(method, req_id, msg)

    async def _send_bridge_payload_to_agent(self, payload: Mapping[str, Any]) -> None:
        """Log and write one validated JSON-RPC payload to the agent subprocess."""
        self._log_outbound_agent_frame(payload)
        await self._process.send_line(payload)

    async def _flush_turn_teardown(self, session_id: str | None = None) -> None:
        """Cancel permissions, elicitations, turns, and host terminals for ``session_id``."""
        messages = await self._turns.drain_teardown_messages(session_id)
        if not messages or not self._process.is_alive:
            return
        for item in messages:
            try:
                await self._send_bridge_payload_to_agent(item)
            except AcpError as exc:
                logger.debug(
                    "[%s] Ignoring teardown write error to ACP subprocess: %s",
                    self._connection_id,
                    exc,
                )
                break

    async def _process_validated_ready_msg(
        self,
        msg: Mapping[str, Any],
        req_id: int | str | None,
    ) -> SessionStepOutcome:
        """Validate, log, and forward a parsed post-initialization frame."""
        is_notif = "method" in msg and "id" not in msg
        self._log_inbound_frame(msg)
        try:
            prepared = self._prepare_ready_outbound(msg)
        except AcpError as exc:
            logger.debug("[%s] Rejected inbound frame: %s", self._connection_id, exc.message)
            return self._error_outcome(exc, req_id, is_notification=is_notif)
        if prepared is None:
            return SessionStepOutcome()
        if prepared.get("method") in ("session/close", "session/delete"):
            p_map = prepared.get("params")
            sess_id = p_map.get("sessionId") if isinstance(p_map, Mapping) else None
            if isinstance(sess_id, str):
                await self._flush_turn_teardown(sess_id)
        await self._send_bridge_payload_to_agent(prepared)
        return SessionStepOutcome()

    async def handle_ready_frame(self, raw_text: str) -> SessionStepOutcome:
        """Validate and forward a post-initialization JSON-RPC frame to the ACP agent."""
        msg, parse_err = parse_client_frame(
            raw_text,
            max_bytes=self._config.transport.max_frame_bytes,
        )
        if parse_err is not None or msg is None:
            label, pretty = format_raw_frame_for_log(parse_err or "")
            logger.warning("[%s] Bridge -> UI (%s)\n%s", self._connection_id, label, pretty)
            return SessionStepOutcome(frames=(parse_err or "",))

        req_id = extract_request_id(msg)
        raw_params = msg.get("params")
        raw_sid = raw_params.get("sessionId") if isinstance(raw_params, Mapping) else None
        raw_meta = raw_params.get("_meta") if isinstance(raw_params, Mapping) else None
        with AcpLogContext.bind(
            connection_id=self._connection_id,
            session_id=raw_sid if isinstance(raw_sid, str) else None,
            request_id=req_id,
            method=msg.get("method") if isinstance(msg.get("method"), str) else None,
            traceparent=extract_traceparent(raw_meta),
        ):
            return await self._process_validated_ready_msg(msg, req_id)

    def _record_setup_state(
        self,
        session_id: str,
        res_obj: object,
        root_set: SessionRootSet | None,
    ) -> None:
        """Activate ``session_id``, merge setup metadata, and bind effective root set."""
        self._active_session_ids.add(session_id)
        if root_set is not None:
            self._turns.register_session_root_set(session_id, root_set)
        payload = res_obj if isinstance(res_obj, Mapping) else {}
        self._session_states[session_id] = apply_session_setup_result(
            self._session_states.get(session_id),
            session_id,
            payload,
        )

    def _apply_session_rpc_result(
        self,
        method_name: str,
        tracked_session_id: str | None,
        loaded: Mapping[str, Any],
        extra: tuple[str | None, SessionRootSet | None],
    ) -> None:
        """Update active sessions, root sets, and runtime metadata from ``session/*`` replies."""
        pending_mode_id, pending_root_set = extra
        succeeded = "result" in loaded and "error" not in loaded
        res_obj = loaded.get("result")
        err_obj = loaded.get("error")
        err_code = err_obj.get("code") if isinstance(err_obj, Mapping) else None

        if method_name == "session/load" and tracked_session_id is not None:
            self._turns.finish_session_load(tracked_session_id)
        if method_name in ("session/close", "session/delete"):
            if tracked_session_id and (succeeded or err_code == ACP_RESOURCE_NOT_FOUND):
                self._active_session_ids.discard(tracked_session_id)
                self._session_states.pop(tracked_session_id, None)
                self._turns.clear_completed_session(tracked_session_id)
            return
        if not succeeded:
            return
        self._apply_successful_session_rpc(
            method_name,
            tracked_session_id,
            res_obj,
            (pending_mode_id, pending_root_set),
        )

    def _apply_successful_session_rpc(
        self,
        method_name: str,
        tracked_session_id: str | None,
        res_obj: object,
        extra: tuple[str | None, SessionRootSet | None],
    ) -> None:
        """Apply succeeded ``new``/``load``/``resume``/``set_config_option``/``set_mode``."""
        pending_mode_id, pending_root_set = extra
        if method_name == "session/new" and isinstance(res_obj, Mapping):
            new_id = res_obj.get("sessionId")
            if isinstance(new_id, str) and new_id:
                self._record_setup_state(new_id, res_obj, pending_root_set)
        elif method_name in ("session/load", "session/resume") and tracked_session_id:
            self._record_setup_state(tracked_session_id, res_obj, pending_root_set)
        elif (
            method_name == "session/set_config_option"
            and tracked_session_id
            and isinstance(res_obj, Mapping)
        ):
            self._session_states[tracked_session_id] = apply_config_options_result(
                self._session_states.get(tracked_session_id),
                tracked_session_id,
                res_obj,
            )
        elif method_name == "session/set_mode" and tracked_session_id and pending_mode_id:
            self._session_states[tracked_session_id] = apply_mode_change(
                self._session_states.get(tracked_session_id),
                tracked_session_id,
                pending_mode_id,
            )

    def _apply_auth_rpc_result(
        self,
        method_name: str,
        target_id: str | None,
        loaded: Mapping[str, Any],
        extra: tuple[str | None, SessionRootSet | None],
    ) -> None:
        """Transition ``auth_state`` or delegate ``session/*`` state updates."""
        if method_name in SESSION_METHODS or method_name in (
            "session/set_config_option",
            "session/set_mode",
        ):
            self._apply_session_rpc_result(method_name, target_id, loaded, extra)
            return
        succeeded = "result" in loaded and "error" not in loaded
        if method_name == "authenticate":
            if succeeded:
                self._auth_state = AcpAuthState.AUTHENTICATED
                self._active_auth_method_id = target_id
            else:
                self._auth_state = AcpAuthState.UNAUTHENTICATED
        elif method_name == "logout" and succeeded:
            self._auth_state = AcpAuthState.UNAUTHENTICATED
            self._active_auth_method_id = None

    def _observe_agent_frame(self, raw_line: str) -> Mapping[str, Any] | None:
        """Update tracked RPC, auth, and latency metrics from an agent stdout frame."""
        try:
            loaded = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            logger.debug("Skipping non-JSON agent frame in observer: %s", exc)
            return None
        if not isinstance(loaded, Mapping):
            return None

        err_obj = loaded.get("error")
        if isinstance(err_obj, Mapping) and err_obj.get("code") == ACP_AUTH_REQUIRED:
            self._auth_state = AcpAuthState.UNAUTHENTICATED
            self._active_auth_method_id = None

        resp_id = extract_request_id(loaded)
        if resp_id is not None and resp_id in self._pending_rpc_methods:
            method_name, target_id, started = self._pending_rpc_methods.pop(resp_id)
            mode_id = self._pending_mode_targets.pop(resp_id, None)
            root_set = self._pending_root_sets.pop(resp_id, None)
            self._apply_auth_rpc_result(method_name, target_id, loaded, (mode_id, root_set))
            duration_ms = (time.monotonic() - started) * 1000.0
            logger.info(
                "[%s] Completed RPC %s (#%s)",
                self._connection_id,
                method_name,
                resp_id,
                extra={"duration_ms": duration_ms},
            )
        return loaded

    def _observe_forwarded_session_update(self, loaded: Mapping[str, Any]) -> None:
        """Patch local ``SessionRuntimeState`` from forwarded ``session/update`` frames."""
        if loaded.get("method") != "session/update" or "id" in loaded:
            return
        params = loaded.get("params")
        update = params.get("update") if isinstance(params, Mapping) else None
        raw_sid = params.get("sessionId") if isinstance(params, Mapping) else None
        if not isinstance(raw_sid, str) or not raw_sid or not isinstance(update, Mapping):
            return
        next_state = apply_session_update_to_state(
            self._session_states.get(raw_sid),
            raw_sid,
            update,
        )
        if next_state is not None:
            self._session_states[raw_sid] = next_state

    async def read_agent_frame(self) -> str | None:
        """Read, inspect, filter, and return the next outbound NDJSON frame from agent."""
        raw_line = await self._process.read_line()
        if not raw_line:
            return raw_line
        loaded = self._observe_agent_frame(raw_line)
        if loaded is None:
            return raw_line

        decision = self._turns.inspect_agent_frame(
            raw_line,
            loaded,
            self._ws_client_capabilities,
        )
        if decision.agent_reply is not None:
            await self._send_bridge_payload_to_agent(decision.agent_reply)
        forward_frame = decision.forward_frame
        if forward_frame:
            forward_frame = filter_boolean_config_options(
                forward_frame,
                loaded,
                self._ws_client_capabilities,
            )
            self._observe_forwarded_session_update(loaded)
        return forward_frame

    async def close(self) -> None:
        """Cancel pending permissions/elicitations/turns, release terminals, and stop agent."""
        self._state = AcpConnectionState.CLOSED
        await self._flush_turn_teardown()
        self._session_states.clear()
        self._pending_mode_targets.clear()
        self._pending_root_sets.clear()
        await self._process.stop()


__all__ = [
    "AcpConnectionSession",
]
