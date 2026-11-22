"""ACP connection initialization state machine and agent bridge session."""

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from api.models.acp import (
    ACP_AUTH_REQUIRED,
    ACP_RESOURCE_NOT_FOUND,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
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
from api.models.config import ApiServerConfig
from api.models.session import SessionRuntimeState
from api.services.chat import SessionChatRecorder
from api.services.process import AgyAcpProcess
from api.services.prompt import PromptTurnManager
from api.utils.chat import extract_hydrate_only_flag, extract_metadata_only_flag
from api.utils.formatting import (
    describe_jsonrpc_frame,
    format_raw_frame_for_log,
    indent_json_payload,
)
from api.utils.jsonrpc import extract_request_id, format_jsonrpc_error, parse_client_frame
from api.utils.prompt import scrub_outbound_message_meta
from api.utils.security import (
    build_effective_client_capabilities,
    clamp_client_capabilities,
    find_advertised_auth_method,
    sanitize_authenticate_params,
    sanitize_meta_dict,
)
from api.utils.session import (
    SESSION_METHODS,
    check_session_capability,
    prepare_session_params,
)
from api.utils.session_state import (
    apply_config_options_result,
    apply_mode_change,
    apply_session_setup_result,
    apply_session_update_to_state,
    prepare_set_config_option_params,
    prepare_set_mode_params,
)
from core.models.exceptions import AcpError
from core.ports.chat import ChatStoreProtocol
from core.utils.logging import get_logger

logger = get_logger("api.services.session")


class AcpConnectionSession:
    """Enforces ACP initialization ordering, capability clamping, and agent bridging."""

    def __init__(
        self,
        connection_id: str,
        config: ApiServerConfig,
        *,
        process: AgyAcpProcess | None = None,
        chat_store: ChatStoreProtocol | None = None,
        pre_setup_sync: Callable[[str], Awaitable[bool]] | None = None,
        on_prompt_finished: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Create a new uninitialized session bound to ``connection_id``."""
        self._connection_id = connection_id
        self._config = config
        self._process = process if process is not None else AgyAcpProcess(config)
        self._state = AcpConnectionState.UNINITIALIZED
        self._initialize_result: InitializeResult | None = None
        self._client_capabilities = ClientCapabilities()
        self._auth_state = AcpAuthState.UNKNOWN
        self._active_auth_method_id: str | None = None
        self._active_session_ids: set[str] = set()
        self._session_states: dict[str, SessionRuntimeState] = {}
        self._pending_rpc_methods: dict[int | str, tuple[str, str | None]] = {}
        self._pending_mode_targets: dict[int | str, str] = {}
        self._turns = PromptTurnManager(connection_id)
        self._chat = SessionChatRecorder(connection_id, chat_store)
        self._pre_setup_sync = pre_setup_sync
        self._on_prompt_finished = on_prompt_finished

    @property
    def connection_id(self) -> str:
        """Return server-assigned ``Acp-Connection-Id`` for this WebSocket."""
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
        """Return clamped ``ClientCapabilities`` negotiated during ``initialize``."""
        return self._client_capabilities

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
        """Return set of session IDs currently active on this connection."""
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

    async def flush_session_chat(self, session_id: str) -> None:
        """Persist any pending buffered live chat chunks for ``session_id``."""
        await self._chat.flush_session(session_id)

    def _log_rejection_outcome(self, outcome: SessionStepOutcome) -> SessionStepOutcome:
        """Log any JSON-RPC error frames in ``outcome`` with clean terminal indentation."""
        for frame in outcome.frames:
            label, pretty = format_raw_frame_for_log(frame)
            logger.warning("[%s] Bridge -> UI (%s)\n%s", self._connection_id, label, pretty)
        return outcome

    def _validate_init_params(
        self,
        req_id: int | str,
        raw_params: object,
    ) -> InitializeParams | SessionStepOutcome:
        """Validate ``initialize`` parameters or return a JSON-RPC ``-32602`` error outcome."""
        if not isinstance(raw_params, Mapping):
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        "Invalid initialize params: expected JSON object",
                    ),
                )
            )
        try:
            return InitializeParams.model_validate(raw_params)
        except PydanticValidationError as exc:
            logger.debug("Invalid initialize params on %s: %s", self._connection_id, exc)
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        "Invalid initialize params",
                    ),
                )
            )

    def _check_pre_init_gate(
        self,
        msg: Mapping[str, Any],
        req_id: int | str | None,
    ) -> SessionStepOutcome | None:
        """Return a rejection outcome if state or method is invalid before ``initialize``."""
        if self._state != AcpConnectionState.UNINITIALIZED:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_REQUEST,
                        "Connection initialization already in progress or completed",
                    ),
                )
            )
        if msg.get("method") != "initialize":
            if "id" not in msg:
                return SessionStepOutcome()
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_REQUEST,
                        "Connection not initialized; first request must be 'initialize'",
                    ),
                )
            )
        if "id" not in msg or req_id is None:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        None,
                        JSONRPC_INVALID_REQUEST,
                        "'initialize' must be a JSON-RPC request with a non-null id",
                    ),
                )
            )
        return None

    def _parse_init_request(
        self,
        msg: Mapping[str, Any],
    ) -> tuple[int | str, InitializeParams] | SessionStepOutcome:
        """Enforce pre-initialization request rules prior to launching ``agy``."""
        req_id = extract_request_id(msg)
        rejection = self._check_pre_init_gate(msg, req_id)
        if rejection is not None:
            return rejection
        assert req_id is not None
        validated = self._validate_init_params(req_id, msg.get("params"))
        if isinstance(validated, SessionStepOutcome):
            return validated
        return req_id, validated

    async def _await_agent_init_response(
        self,
        req_id: int | str,
    ) -> tuple[str, dict[str, Any]]:
        """Read NDJSON lines from ``agy`` stdout until matching ``initialize`` response arrives."""
        while True:
            raw_line = await self._process.read_line()
            if raw_line is None:
                raise AcpError("ACP agent closed stdout before responding to initialize.")
            if not raw_line:
                continue
            try:
                loaded = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise AcpError(f"ACP agent returned invalid JSON during initialize: {exc}") from exc
            if isinstance(loaded, dict) and loaded.get("id") == req_id:
                return raw_line, loaded

    async def _finalize_init_response(
        self,
        req_id: int | str,
        raw_line: str,
        agent_msg: Mapping[str, Any],
    ) -> SessionStepOutcome:
        """Validate ``agy`` initialize response, enforce version match, and enter ``READY``."""
        label, pretty = format_raw_frame_for_log(raw_line)
        if "error" in agent_msg or "result" not in agent_msg:
            logger.warning("[%s] agy -> UI (%s)\n%s", self._connection_id, label, pretty)
            await self.close()
            return SessionStepOutcome(frames=(raw_line,), close_code=1002)

        try:
            parsed_result = InitializeResult.model_validate(agent_msg["result"])
        except PydanticValidationError as exc:
            await self.close()
            raise AcpError(f"ACP agent returned malformed initialize result: {exc}") from exc

        if parsed_result.protocol_version not in SUPPORTED_ACP_VERSIONS:
            await self.close()
            err = format_jsonrpc_error(
                req_id,
                JSONRPC_INVALID_PARAMS,
                f"Unsupported ACP protocolVersion: {parsed_result.protocol_version}",
            )
            return self._log_rejection_outcome(
                SessionStepOutcome(frames=(err,), close_code=1002)
            )

        self._initialize_result = parsed_result
        self._state = AcpConnectionState.READY
        logger.info(
            "[%s] agy -> UI (%s · initialize)\n%s",
            self._connection_id,
            label,
            pretty,
        )
        return SessionStepOutcome(frames=(raw_line,))

    async def _execute_agent_initialize(
        self,
        req_id: int | str,
        init_params: InitializeParams,
    ) -> SessionStepOutcome:
        """Lazily spawn ``agy``, forward sanitized ``initialize``, and validate response."""
        self._state = AcpConnectionState.INITIALIZING
        await self._process.start()
        outbound = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "initialize",
            "params": clamp_client_capabilities(init_params),
        }
        logger.info(
            "[%s] Bridge -> agy (request #%s · clamped initialize)\n%s",
            self._connection_id,
            req_id,
            indent_json_payload(outbound),
        )
        await self._process.send_line(outbound)
        raw_line, agent_msg = await self._await_agent_init_response(req_id)
        outcome = await self._finalize_init_response(req_id, raw_line, agent_msg)
        if self._state == AcpConnectionState.READY:
            self._client_capabilities = build_effective_client_capabilities(init_params)
        return outcome

    async def handle_init_frame(self, raw_text: str) -> SessionStepOutcome:
        """Process one incoming WebSocket frame while waiting for ``initialize``."""
        msg, parse_err = parse_client_frame(raw_text, max_bytes=self._config.max_frame_bytes)
        if parse_err is not None or msg is None:
            return self._log_rejection_outcome(SessionStepOutcome(frames=(parse_err or "",)))

        logger.info(
            "[%s] UI -> Bridge (%s)\n%s",
            self._connection_id,
            describe_jsonrpc_frame(msg),
            indent_json_payload(msg),
        )
        parsed = self._parse_init_request(msg)
        if isinstance(parsed, SessionStepOutcome):
            return self._log_rejection_outcome(parsed)
        req_id, init_params = parsed
        return await self._execute_agent_initialize(req_id, init_params)

    def _check_auth_method_allowed(
        self,
        req_id: int | str,
        method_id: str,
    ) -> SessionStepOutcome | None:
        """Verify ``method_id`` is an advertised protocol-driven (non-terminal) method."""
        advertised = self._initialize_result.auth_methods if self._initialize_result else ()
        matched = find_advertised_auth_method(method_id, advertised)
        if matched is None:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        f"Unknown authentication methodId: '{method_id}'",
                    ),
                )
            )
        if matched.type == "terminal":
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        f"Terminal authentication method '{method_id}' cannot use 'authenticate'",
                    ),
                )
            )
        return None

    def _prepare_authenticate_outbound(
        self,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any] | SessionStepOutcome:
        """Validate ``authenticate`` request against advertised ``authMethods``."""
        if "id" not in msg or req_id is None:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        None,
                        JSONRPC_INVALID_REQUEST,
                        "'authenticate' must be a JSON-RPC request with a non-null id",
                    ),
                )
            )
        raw_params = msg.get("params")
        if not isinstance(raw_params, Mapping):
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        "Invalid authenticate params: expected JSON object",
                    ),
                )
            )
        try:
            auth_params = AuthenticateParams.model_validate(raw_params)
        except PydanticValidationError as exc:
            logger.debug("Invalid authenticate params on %s: %s", self._connection_id, exc)
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_PARAMS,
                        "Invalid authenticate params",
                    ),
                )
            )
        rejection = self._check_auth_method_allowed(req_id, auth_params.method_id)
        if rejection is not None:
            return rejection

        self._auth_state = AcpAuthState.AUTHENTICATING
        self._pending_rpc_methods[req_id] = ("authenticate", auth_params.method_id)
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
    ) -> dict[str, Any] | SessionStepOutcome:
        """Enforce ``agentCapabilities.auth.logout`` capability gate before ``logout``."""
        if "id" not in msg or req_id is None:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        None,
                        JSONRPC_INVALID_REQUEST,
                        "'logout' must be a JSON-RPC request with a non-null id",
                    ),
                )
            )
        auth_caps = (
            self._initialize_result.agent_capabilities.auth if self._initialize_result else None
        )
        if auth_caps is None or auth_caps.logout is None:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_METHOD_NOT_FOUND,
                        "Agent did not advertise agentCapabilities.auth.logout",
                    ),
                )
            )
        raw_params = msg.get("params", {})
        if raw_params is not None and not isinstance(raw_params, Mapping):
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, "Invalid logout params"),
                )
            )
        logout_params = LogoutParams.model_validate(raw_params or {})
        scrubbed = sanitize_meta_dict(logout_params.meta)
        params_payload: dict[str, Any] = {"_meta": scrubbed} if scrubbed is not None else {}
        self._pending_rpc_methods[req_id] = ("logout", None)
        return {"jsonrpc": "2.0", "id": req_id, "method": "logout", "params": params_payload}

    def _resolve_session_setup_target(
        self,
        method: str,
        raw_params: object,
        caps: AgentCapabilities,
    ) -> tuple[str, object, tuple[str, bool, bool]]:
        """Route ``metadataOnly``/``hydrateOnly`` setup requests against chat hydration state."""
        if method == "session/load":
            metadata_only, meta_cleaned = extract_metadata_only_flag(raw_params)
            if metadata_only:
                return "session/load", meta_cleaned, ("metadata", True, False)
        hydrate_only, cleaned_params = extract_hydrate_only_flag(raw_params)
        if not hydrate_only or method not in ("session/load", "session/resume"):
            return method, cleaned_params, ("none", False, False)
        raw_sid = (
            cleaned_params.get("sessionId")
            if isinstance(cleaned_params, Mapping)
            else None
        )
        if not isinstance(raw_sid, str) or not raw_sid:
            return method, cleaned_params, ("hydrate", False, False)
        outbound, suppress_replay, hydrate_reset = self._chat.resolve_setup_routing(
            method,
            raw_sid,
            caps,
            hydrate_only=True,
        )
        return outbound, cleaned_params, ("hydrate", suppress_replay, hydrate_reset)

    def _validate_and_sanitize_session_params(
        self,
        req_id: int | str,
        method: str,
        raw_params: object,
        caps: AgentCapabilities,
    ) -> dict[str, Any] | SessionStepOutcome:
        """Validate ``method`` capability and sanitize outbound session parameters."""
        cap_error = check_session_capability(method, caps)
        if cap_error is not None:
            return SessionStepOutcome(
                frames=(format_jsonrpc_error(req_id, JSONRPC_METHOD_NOT_FOUND, cap_error),)
            )
        try:
            return prepare_session_params(method, raw_params, caps, self._config)
        except (PydanticValidationError, ValueError) as exc:
            logger.debug("Invalid %s params on %s: %s", method, self._connection_id, exc)
            detail = str(exc) if isinstance(exc, ValueError) else f"Invalid {method} params"
            return SessionStepOutcome(
                frames=(format_jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, detail),)
            )

    def _prepare_session_outbound(
        self,
        method: str,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any] | SessionStepOutcome:
        """Enforce capability, child-session, and parameter gates for ``session/*`` methods."""
        if "id" not in msg or req_id is None:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        None,
                        JSONRPC_INVALID_REQUEST,
                        f"'{method}' must be a JSON-RPC request with a non-null id",
                    ),
                )
            )
        target_gate = self._turns.check_session_target_allowed(
            method,
            req_id,
            msg,
            self.active_session_ids,
        )
        if target_gate is not None:
            return target_gate

        caps = (
            self._initialize_result.agent_capabilities
            if self._initialize_result is not None
            else AgentCapabilities()
        )
        route = self._resolve_session_setup_target(method, msg.get("params"), caps)
        sanitized = self._validate_and_sanitize_session_params(
            req_id,
            route[0],
            route[1],
            caps,
        )
        if isinstance(sanitized, SessionStepOutcome):
            return sanitized

        target_session = sanitized.get("sessionId")
        tracked_id = target_session if isinstance(target_session, str) else None
        self._pending_rpc_methods[req_id] = (route[0], tracked_id)
        if route[2][0] == "metadata" and tracked_id is not None:
            self._chat.register_metadata_only_load(req_id, tracked_id)
        elif route[2][0] == "hydrate" and tracked_id is not None:
            self._chat.register_pending_setup(
                req_id,
                tracked_id,
                suppress_replay=route[2][1],
                hydrate_reset=route[2][2],
            )
        return {"jsonrpc": "2.0", "id": req_id, "method": route[0], "params": sanitized}

    def _prepare_prompt_or_cancel(
        self,
        method: str,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any] | SessionStepOutcome | None:
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
    ) -> dict[str, Any] | SessionStepOutcome:
        """Validate ``session/set_config_option`` or ``session/set_mode`` against session state."""
        if "id" not in msg or req_id is None:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        None,
                        JSONRPC_INVALID_REQUEST,
                        f"'{method}' must be a JSON-RPC request with a non-null id",
                    ),
                )
            )
        target_gate = self._turns.check_session_target_allowed(
            method,
            req_id,
            msg,
            self.active_session_ids,
        )
        if target_gate is not None:
            return target_gate

        raw_params = msg.get("params")
        raw_sid = raw_params.get("sessionId") if isinstance(raw_params, Mapping) else None
        current_state = self._session_states.get(raw_sid) if isinstance(raw_sid, str) else None
        try:
            session_id, sanitized = (
                prepare_set_config_option_params(raw_params, current_state)
                if method == "session/set_config_option"
                else prepare_set_mode_params(raw_params, current_state)
            )
        except (PydanticValidationError, ValueError) as exc:
            logger.debug("Invalid %s params on %s: %s", method, self._connection_id, exc)
            detail = str(exc) if isinstance(exc, ValueError) else f"Invalid {method} params"
            return SessionStepOutcome(
                frames=(format_jsonrpc_error(req_id, JSONRPC_INVALID_PARAMS, detail),)
            )

        self._pending_rpc_methods[req_id] = (method, session_id)
        if method == "session/set_mode":
            self._pending_mode_targets[req_id] = str(sanitized["modeId"])
        return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": sanitized}

    def _dispatch_ready_method(
        self,
        method: str,
        req_id: int | str | None,
        msg: Mapping[str, Any],
    ) -> dict[str, Any] | SessionStepOutcome | None:
        """Route a validated post-initialization JSON-RPC ``method`` call or notification."""
        if method in ("authenticate", "logout"):
            return (
                self._prepare_authenticate_outbound(req_id, msg)
                if method == "authenticate"
                else self._prepare_logout_outbound(req_id, msg)
            )
        if method in ("session/prompt", "session/cancel", "$/cancel_request"):
            return self._prepare_prompt_or_cancel(method, req_id, msg)
        if method in ("session/set_config_option", "session/set_mode"):
            return self._prepare_config_or_mode_outbound(method, req_id, msg)
        if method in SESSION_METHODS:
            return self._prepare_session_outbound(method, req_id, msg)

        target_gate = self._turns.check_session_target_allowed(
            method,
            req_id,
            msg,
            self.active_session_ids,
        )
        return target_gate if target_gate is not None else scrub_outbound_message_meta(msg)

    def _prepare_ready_outbound(
        self,
        msg: Mapping[str, Any],
    ) -> dict[str, Any] | SessionStepOutcome | None:
        """Dispatch post-init validation for requests, notifications, and client responses."""
        if "method" not in msg:
            return self._turns.prepare_client_response(msg)

        req_id = extract_request_id(msg)
        method = msg.get("method")
        if not isinstance(method, str) or not method:
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_REQUEST,
                        "Invalid JSON-RPC method",
                    ),
                )
            )
        if method == "initialize":
            return SessionStepOutcome(
                frames=(
                    format_jsonrpc_error(
                        req_id,
                        JSONRPC_INVALID_REQUEST,
                        "Connection already initialized",
                    ),
                )
            )
        return self._dispatch_ready_method(method, req_id, msg)

    async def _send_bridge_payload_to_agent(self, payload: Mapping[str, Any]) -> None:
        """Log and write one bridge-generated or sanitized JSON-RPC payload to ``agy``."""
        out_label = describe_jsonrpc_frame(payload)
        logger.info(
            "[%s] Bridge -> agy (%s)\n%s",
            self._connection_id,
            out_label,
            indent_json_payload(payload),
        )
        await self._process.send_line(payload)

    async def _flush_turn_teardown(self, session_id: str | None = None) -> None:
        """Best-effort send cancelled permission outcomes and turn cancellations to ``agy``."""
        messages = self._turns.drain_teardown_messages(session_id)
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

    async def _maybe_pre_sync_hydrate(self, msg: Mapping[str, Any]) -> None:
        """Pre-sync external transcript logs before ``hydrateOnly`` setup routing."""
        method = msg.get("method")
        if self._pre_setup_sync is None or method not in ("session/load", "session/resume"):
            return
        if method == "session/load" and extract_metadata_only_flag(msg.get("params"))[0]:
            return
        hydrate_only, cleaned = extract_hydrate_only_flag(msg.get("params"))
        raw_sid = cleaned.get("sessionId") if isinstance(cleaned, Mapping) else None
        if hydrate_only and isinstance(raw_sid, str) and raw_sid:
            await self._pre_setup_sync(raw_sid)

    async def handle_ready_frame(self, raw_text: str) -> SessionStepOutcome:
        """Validate and forward a post-initialization JSON-RPC frame to ``agy acp``."""
        msg, parse_err = parse_client_frame(raw_text, max_bytes=self._config.max_frame_bytes)
        if parse_err is not None or msg is None:
            return self._log_rejection_outcome(SessionStepOutcome(frames=(parse_err or "",)))

        frame_label = describe_jsonrpc_frame(msg)
        logger.info(
            "[%s] UI -> Bridge (%s)\n%s",
            self._connection_id,
            frame_label,
            indent_json_payload(msg),
        )
        await self._maybe_pre_sync_hydrate(msg)
        prepared = self._prepare_ready_outbound(msg)
        if isinstance(prepared, SessionStepOutcome):
            return self._log_rejection_outcome(prepared)
        if prepared is None:
            return SessionStepOutcome()

        method = prepared.get("method")
        if method in ("session/close", "session/delete"):
            raw_params = prepared.get("params")
            sess_id = raw_params.get("sessionId") if isinstance(raw_params, Mapping) else None
            if isinstance(sess_id, str):
                await self._flush_turn_teardown(sess_id)
                await self._chat.flush_session(sess_id)
        elif method == "session/prompt":
            await self._record_outbound_prompt(prepared)

        await self._send_bridge_payload_to_agent(prepared)
        return SessionStepOutcome()

    async def _record_outbound_prompt(self, prepared: Mapping[str, Any]) -> None:
        """Persist outbound ``session/prompt`` user turn before waiting for streamed reply."""
        req_id = extract_request_id(prepared)
        raw_params = prepared.get("params")
        if req_id is None or not isinstance(raw_params, Mapping):
            return
        sess_id = raw_params.get("sessionId")
        if isinstance(sess_id, str) and sess_id:
            await self._chat.record_prompt_sent(sess_id, req_id, raw_params)

    def _record_setup_state(self, session_id: str, res_obj: object) -> None:
        """Activate ``session_id`` and merge setup metadata into ``_session_states``."""
        self._active_session_ids.add(session_id)
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
        pending_mode_id: str | None = None,
    ) -> None:
        """Update ``active_session_ids`` and ``_session_states`` from ``session/*`` responses."""
        succeeded = "result" in loaded and "error" not in loaded
        res_obj = loaded.get("result")
        err_obj = loaded.get("error")
        err_code = err_obj.get("code") if isinstance(err_obj, Mapping) else None

        if method_name == "session/new" and succeeded and isinstance(res_obj, Mapping):
            new_id = res_obj.get("sessionId")
            if isinstance(new_id, str) and new_id:
                self._record_setup_state(new_id, res_obj)
        elif method_name in ("session/load", "session/resume") and succeeded and tracked_session_id:
            self._record_setup_state(tracked_session_id, res_obj)
        elif method_name == "session/set_config_option" and succeeded and tracked_session_id:
            if isinstance(res_obj, Mapping):
                self._session_states[tracked_session_id] = apply_config_options_result(
                    self._session_states.get(tracked_session_id),
                    tracked_session_id,
                    res_obj,
                )
        elif (
            method_name == "session/set_mode"
            and succeeded
            and tracked_session_id
            and pending_mode_id
        ):
            self._session_states[tracked_session_id] = apply_mode_change(
                self._session_states.get(tracked_session_id),
                tracked_session_id,
                pending_mode_id,
            )
        elif (
            method_name in ("session/close", "session/delete")
            and tracked_session_id
            and (succeeded or err_code == ACP_RESOURCE_NOT_FOUND)
        ):
            self._active_session_ids.discard(tracked_session_id)
            self._session_states.pop(tracked_session_id, None)
            self._turns.clear_completed_session(tracked_session_id)

    def _apply_auth_rpc_result(
        self,
        method_name: str,
        target_id: str | None,
        loaded: Mapping[str, Any],
        pending_mode_id: str | None = None,
    ) -> None:
        """Transition ``auth_state`` or ``active_session_ids`` when tracked RPCs finish."""
        if method_name in SESSION_METHODS or method_name in (
            "session/set_config_option",
            "session/set_mode",
        ):
            self._apply_session_rpc_result(method_name, target_id, loaded, pending_mode_id)
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

    def _observe_agent_frame(
        self,
        raw_line: str,
    ) -> tuple[Mapping[str, Any] | None, tuple[int | str, str, str | None, bool] | None]:
        """Update tracked auth/session state and return parsed frame plus completed RPC."""
        try:
            loaded = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            logger.debug("Skipping non-JSON agent frame in auth observer: %s", exc)
            return None, None
        if not isinstance(loaded, Mapping):
            return None, None

        err_obj = loaded.get("error")
        if isinstance(err_obj, Mapping) and err_obj.get("code") == ACP_AUTH_REQUIRED:
            self._auth_state = AcpAuthState.UNAUTHENTICATED
            self._active_auth_method_id = None

        completed_rpc: tuple[int | str, str, str | None, bool] | None = None
        resp_id = extract_request_id(loaded)
        if resp_id is not None and resp_id in self._pending_rpc_methods:
            method_name, target_id = self._pending_rpc_methods.pop(resp_id)
            mode_id = self._pending_mode_targets.pop(resp_id, None)
            self._apply_auth_rpc_result(method_name, target_id, loaded, mode_id)
            succeeded = "result" in loaded and "error" not in loaded
            completed_rpc = (resp_id, method_name, target_id, succeeded)
        return loaded, completed_rpc

    def _observe_forwarded_session_update(self, loaded: Mapping[str, Any]) -> bool:
        """Record real-time timeline updates, patch runtime state, and check suppression."""
        if loaded.get("method") != "session/update" or "id" in loaded:
            return True
        params = loaded.get("params")
        update = params.get("update") if isinstance(params, Mapping) else None
        raw_sid = params.get("sessionId") if isinstance(params, Mapping) else None
        if not isinstance(raw_sid, str) or not raw_sid or not isinstance(update, Mapping):
            return True
        should_forward = self._chat.observe_session_update(raw_sid, update)
        next_state = apply_session_update_to_state(
            self._session_states.get(raw_sid),
            raw_sid,
            update,
        )
        if next_state is not None:
            self._session_states[raw_sid] = next_state
        return should_forward

    async def _finalize_chat_rpc_effect(
        self,
        completed_rpc: tuple[int | str, str, str | None, bool],
        forward_frame: str,
        loaded: Mapping[str, Any],
    ) -> str:
        """Apply chat persistence side effects and ``_meta.cbiChat`` hydration enrichment."""
        resp_id, method_name, target_id, succeeded = completed_rpc
        if method_name == "session/new" and succeeded:
            res_obj = loaded.get("result")
            new_id = res_obj.get("sessionId") if isinstance(res_obj, Mapping) else None
            if isinstance(new_id, str) and new_id:
                await self._chat.mark_new_session(new_id)
        elif method_name in ("session/load", "session/resume"):
            return await self._chat.complete_setup_response(
                resp_id,
                forward_frame,
                loaded,
                succeeded=succeeded,
            )
        elif method_name == "session/close" and target_id:
            await self._chat.flush_session(target_id)
        elif method_name == "session/delete" and target_id:
            err_obj = loaded.get("error")
            err_code = err_obj.get("code") if isinstance(err_obj, Mapping) else None
            if succeeded or err_code == ACP_RESOURCE_NOT_FOUND:
                await self._chat.delete_session(target_id)
        return forward_frame

    async def read_agent_frame(self) -> str | None:
        """Read, validate, persist, and filter the next outbound NDJSON frame from ``agy``."""
        raw_line = await self._process.read_line()
        if not raw_line:
            return raw_line
        loaded, completed_rpc = self._observe_agent_frame(raw_line)
        if loaded is None:
            return raw_line
        resp_id = extract_request_id(loaded)
        prompt_session_id = (
            self._turns.session_id_for_prompt_request(resp_id)
            if resp_id is not None
            else None
        )
        decision = self._turns.inspect_agent_frame(
            raw_line,
            loaded,
            self._client_capabilities,
        )
        if decision.agent_reply is not None:
            await self._send_bridge_payload_to_agent(decision.agent_reply)
        forward_frame = decision.forward_frame
        if forward_frame and not self._observe_forwarded_session_update(loaded):
            forward_frame = ""
        if prompt_session_id is not None:
            await self._chat.flush_session(prompt_session_id, finalize_active=True)
            if self._on_prompt_finished is not None:
                await self._on_prompt_finished(prompt_session_id)
        if completed_rpc is not None:
            forward_frame = await self._finalize_chat_rpc_effect(
                completed_rpc,
                forward_frame,
                loaded,
            )
        return forward_frame

    async def close(self) -> None:
        """Cancel pending permissions/turns, flush chat history, and stop ``agy acp``."""
        self._state = AcpConnectionState.CLOSED
        await self._flush_turn_teardown()
        await self._chat.flush_all()
        self._session_states.clear()
        self._pending_mode_targets.clear()
        await self._process.stop()


__all__ = [
    "AcpConnectionSession",
]
