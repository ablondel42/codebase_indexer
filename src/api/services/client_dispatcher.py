"""Bidirectional coordinator for Agent -> Client requests, notifications, and host ops."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from api.models.acp import ClientCapabilities
from api.models.client_ops import (
    CompleteElicitationParams,
    CreateTerminalParams,
    CreateTerminalResult,
    KillTerminalParams,
    KillTerminalResult,
    PendingElicitationRequest,
    ReadTextFileParams,
    ReadTextFileResult,
    ReleaseTerminalParams,
    ReleaseTerminalResult,
    TerminalOutputParams,
    TerminalOutputResult,
    WaitForTerminalExitParams,
    WaitForTerminalExitResult,
    WriteTextFileParams,
    WriteTextFileResult,
)
from api.models.config import AcpServerConfig, CapabilityPolicy
from api.models.jsonrpc import (
    ACP_REQUEST_CANCELLED,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
)
from api.models.prompt import (
    AgentFrameDecision,
    PendingAgentRequest,
    PendingPermissionRequest,
    RequestPermissionParams,
)
from api.models.session import SessionRootSet
from api.services.filesystem import HostFileSystemService
from api.services.logging import AcpAuditLogger, AcpLogContext
from api.services.terminal import HostTerminalManager
from api.utils.elicitation import (
    build_cancelled_elicitation_response,
    validate_elicitation_request,
    validate_elicitation_response,
)
from api.utils.errors import build_jsonrpc_error_dict, pydantic_error_to_acp_error
from api.utils.jsonrpc import extract_request_id
from api.utils.prompt import (
    build_cancelled_permission_response,
    is_valid_extension_method,
    prepare_cancel_request_params,
    scrub_outbound_message_meta,
    validate_permission_response,
)
from api.utils.security import validate_path_in_root_set
from core.models.base import FrozenModel
from core.models.exceptions import (
    AcpError,
    AcpInvalidParamsError,
    AcpInvalidRequestError,
)
from core.utils.logging import get_logger

logger = get_logger("api.services.client_dispatcher")

_FS_MODELS: dict[str, tuple[type[FrozenModel], type[FrozenModel]]] = {
    "fs/read_text_file": (ReadTextFileParams, ReadTextFileResult),
    "fs/write_text_file": (WriteTextFileParams, WriteTextFileResult),
}

_TERMINAL_MODELS: dict[str, tuple[type[FrozenModel], type[FrozenModel]]] = {
    "terminal/create": (CreateTerminalParams, CreateTerminalResult),
    "terminal/output": (TerminalOutputParams, TerminalOutputResult),
    "terminal/wait_for_exit": (WaitForTerminalExitParams, WaitForTerminalExitResult),
    "terminal/kill": (KillTerminalParams, KillTerminalResult),
    "terminal/release": (ReleaseTerminalParams, ReleaseTerminalResult),
}


class ClientRequestDispatcher:
    """Routes Agent -> Client calls to relay (WS client) or backend host execution."""

    def __init__(
        self,
        connection_id: str,
        config: AcpServerConfig,
        audit: AcpAuditLogger,
        send_to_agent: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        """Initialize dispatcher state, host FS/terminal managers, and reply sender."""
        self._connection_id = connection_id
        self._config = config
        self._audit = audit
        self._send_to_agent = send_to_agent
        self._fs = HostFileSystemService(config.capabilities, audit)
        self._terminals = HostTerminalManager(config.capabilities, audit)
        self._pending_relayed: dict[int | str, PendingAgentRequest] = {}
        self._pending_permissions: dict[int | str, PendingPermissionRequest] = {}
        self._pending_elicitations: dict[int | str, PendingElicitationRequest] = {}
        self._active_url_elicitation_ids: set[str] = set()
        self._host_tasks: dict[int | str, asyncio.Task[None]] = {}
        self._host_task_sessions: dict[int | str, str] = {}

    @property
    def pending_permission_ids(self) -> frozenset[int | str]:
        """Return unresolved ``session/request_permission`` JSON-RPC IDs."""
        return frozenset(self._pending_permissions.keys())

    @property
    def pending_elicitation_ids(self) -> frozenset[int | str]:
        """Return unresolved ``elicitation/create`` JSON-RPC IDs."""
        return frozenset(self._pending_elicitations.keys())

    @property
    def pending_agent_request_count(self) -> int:
        """Return total in-flight relayed and host-executed agent requests."""
        return len(self._pending_relayed) + len(self._host_tasks)

    @staticmethod
    def _error_reply(
        req_id: int | str,
        code: int,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> AgentFrameDecision:
        """Build an immediate error reply sent back to the agent subprocess."""
        return AgentFrameDecision(
            forward_frame="",
            agent_reply=build_jsonrpc_error_dict(req_id, code, message, data=data),
        )

    def _register_relayed(
        self,
        req_id: int | str,
        method: str,
        session_id: str | None,
    ) -> None:
        """Record an agent request forwarded to the WebSocket client."""
        self._pending_relayed[req_id] = PendingAgentRequest(
            request_id=req_id,
            method=method,
            session_id=session_id,
            started_at_mono=time.monotonic(),
            is_host_executed=False,
        )

    def _handle_permission_request(
        self,
        raw_line: str,
        req_id: int | str,
        raw_params: object,
    ) -> AgentFrameDecision:
        """Validate and track ``session/request_permission`` before relaying."""
        if not isinstance(raw_params, Mapping):
            return self._error_reply(
                req_id,
                JSONRPC_INVALID_PARAMS,
                "Invalid session/request_permission params",
            )
        try:
            parsed = RequestPermissionParams.model_validate(raw_params)
        except PydanticValidationError as exc:
            err = pydantic_error_to_acp_error(exc, label="session/request_permission")
            logger.debug(
                "[%s] Invalid session/request_permission params: %s",
                self._connection_id,
                err.message,
            )
            return self._error_reply(req_id, err.jsonrpc_code, err.message, err.wire_data)

        self._pending_permissions[req_id] = PendingPermissionRequest(
            request_id=req_id,
            session_id=parsed.session_id,
            option_ids=frozenset(opt.option_id for opt in parsed.options),
        )
        self._register_relayed(req_id, "session/request_permission", parsed.session_id)
        return AgentFrameDecision(forward_frame=raw_line)

    def _handle_elicitation_request(
        self,
        raw_line: str,
        req_id: int | str,
        raw_params: object,
        ws_caps: ClientCapabilities,
    ) -> AgentFrameDecision:
        """Validate ``elicitation/create`` mode/scope/security and relay to client."""
        try:
            pending = validate_elicitation_request(
                req_id,
                raw_params,
                ws_caps,
                self._config.capabilities,
                self._active_url_elicitation_ids,
            )
        except AcpError as exc:
            logger.debug("[%s] Rejected elicitation/create: %s", self._connection_id, exc.message)
            if exc.audit_event:
                self._audit.record(
                    "elicitation_url_blocked",
                    outcome="blocked",
                    details={"request_id": req_id},
                )
            return self._error_reply(req_id, exc.jsonrpc_code, exc.message, exc.wire_data)

        self._pending_elicitations[req_id] = pending
        if pending.elicitation_id is not None:
            self._active_url_elicitation_ids.add(pending.elicitation_id)
        self._register_relayed(req_id, "elicitation/create", pending.session_id)
        return AgentFrameDecision(forward_frame=raw_line)

    def _spawn_host_task(
        self,
        req_id: int | str,
        method: str,
        session_id: str,
        coro: Coroutine[Any, Any, FrozenModel],
    ) -> AgentFrameDecision:
        """Execute a host FS/terminal operation asynchronously without blocking stdout."""
        task = asyncio.create_task(self._execute_host_coro(req_id, method, session_id, coro))
        self._host_tasks[req_id] = task
        self._host_task_sessions[req_id] = session_id
        return AgentFrameDecision(forward_frame="")

    async def _execute_host_coro(
        self,
        req_id: int | str,
        method: str,
        session_id: str,
        coro: Coroutine[Any, Any, FrozenModel],
    ) -> None:
        """Run host operation coroutine, send JSON-RPC reply to agent, and log latency."""
        started = time.monotonic()
        reply: dict[str, Any] | None = None
        try:
            with AcpLogContext.bind(
                connection_id=self._connection_id,
                session_id=session_id,
                request_id=req_id,
                method=method,
            ):
                result_model = await coro
                reply = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": result_model.model_dump(by_alias=True, exclude_none=True),
                }
        except asyncio.CancelledError:
            reply = build_jsonrpc_error_dict(req_id, ACP_REQUEST_CANCELLED, "Request cancelled")
            logger.info("[%s] Cancelled host task %s (#%s)", self._connection_id, method, req_id)
        except AcpError as exc:
            logger.debug("[%s] Rejected elicitation/create: %s", self._connection_id, exc.message)
            if exc.audit_event:
                self._audit.record(
                    exc.audit_event,
                    outcome="blocked",
                    details={"method": method, "session_id": session_id},
                )
            reply = build_jsonrpc_error_dict(req_id, exc.jsonrpc_code, exc.message, exc.wire_data)
        except OSError as exc:
            logger.warning("[%s] Host %s failed: %s", self._connection_id, method, exc)
            reply = build_jsonrpc_error_dict(req_id, JSONRPC_INTERNAL_ERROR, "Internal error")
        finally:
            self._host_tasks.pop(req_id, None)
            self._host_task_sessions.pop(req_id, None)

        if reply is not None:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            logger.debug(
                "[%s] Host %s (#%s) completed",
                self._connection_id,
                method,
                req_id,
                extra={"duration_ms": elapsed_ms},
            )
            try:
                await self._send_to_agent(reply)
            except AcpError as exc:
                logger.debug(
                    "[%s] Dropping host reply #%s on closed transport: %s",
                    self._connection_id,
                    req_id,
                    exc,
                )

    def _dispatch_parsed_fs(
        self,
        raw_line: str,
        req_id: int | str,
        method: str,
        parsed: ReadTextFileParams | WriteTextFileParams,
        resolve_ws_and_root: tuple[ClientCapabilities, SessionRootSet | None],
    ) -> AgentFrameDecision:
        """Route validated ``fs/*`` params to host execution or WS client relay."""
        ws_caps, root_set = resolve_ws_and_root
        policy = (
            self._config.capabilities.fs_read
            if method == "fs/read_text_file"
            else self._config.capabilities.fs_write
        )
        if policy == CapabilityPolicy.HOST:
            coro: Coroutine[Any, Any, FrozenModel] = (
                self._fs.read_text_file(parsed, root_set)
                if isinstance(parsed, ReadTextFileParams)
                else self._fs.write_text_file(parsed, root_set)
            )
            return self._spawn_host_task(req_id, method, parsed.session_id, coro)
        return self._relay_fs_call(raw_line, req_id, method, parsed, ws_caps, root_set)

    def _handle_fs_request(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        ws_caps: ClientCapabilities,
        resolve_root_set: Callable[[str], SessionRootSet | None],
    ) -> AgentFrameDecision:
        """Dispatch ``fs/read_text_file`` or ``fs/write_text_file`` per capability policy."""
        req_id = extract_request_id(loaded)
        method = str(loaded.get("method"))
        assert req_id is not None
        policy = (
            self._config.capabilities.fs_read
            if method == "fs/read_text_file"
            else self._config.capabilities.fs_write
        )
        if policy == CapabilityPolicy.DISABLED:
            return self._error_reply(req_id, JSONRPC_METHOD_NOT_FOUND, f"'{method}' is disabled")

        raw_params = loaded.get("params")
        if not isinstance(raw_params, Mapping):
            return self._error_reply(req_id, JSONRPC_INVALID_PARAMS, f"Invalid {method} params")
        req_cls, _ = _FS_MODELS[method]
        try:
            parsed = req_cls.model_validate(raw_params)
        except PydanticValidationError as exc:
            err = pydantic_error_to_acp_error(exc, label=method)
            logger.debug("[%s] Invalid %s params: %s", self._connection_id, method, err.message)
            return self._error_reply(req_id, err.jsonrpc_code, err.message, err.wire_data)

        assert isinstance(parsed, (ReadTextFileParams, WriteTextFileParams))
        return self._dispatch_parsed_fs(
            raw_line,
            req_id,
            method,
            parsed,
            (ws_caps, resolve_root_set(parsed.session_id)),
        )

    def _relay_fs_call(
        self,
        raw_line: str,
        req_id: int | str,
        method: str,
        parsed: ReadTextFileParams | WriteTextFileParams,
        ws_caps: ClientCapabilities,
        root_set: SessionRootSet | None,
    ) -> AgentFrameDecision:
        """Verify WS client ``fs`` capability and root-set containment before relaying."""
        allowed = bool(
            ws_caps.fs is not None
            and (
                ws_caps.fs.read_text_file
                if method == "fs/read_text_file"
                else ws_caps.fs.write_text_file
            )
        )
        if not allowed:
            return self._error_reply(
                req_id,
                JSONRPC_METHOD_NOT_FOUND,
                f"Client did not advertise '{method}'",
            )
        try:
            validate_path_in_root_set(
                parsed.path,
                root_set,
                allow_missing_leaf=(method == "fs/write_text_file"),
            )
        except AcpError as exc:
            logger.debug("[%s] Rejected %s path: %s", self._connection_id, method, exc.message)
            return self._error_reply(req_id, exc.jsonrpc_code, exc.message, exc.wire_data)
        self._register_relayed(req_id, method, parsed.session_id)
        return AgentFrameDecision(forward_frame=raw_line)

    def _build_host_terminal_coro(
        self,
        method: str,
        parsed: FrozenModel,
        root_set: SessionRootSet | None,
    ) -> Coroutine[Any, Any, FrozenModel]:
        """Select the matching ``HostTerminalManager`` coroutine for ``method``."""
        if isinstance(parsed, CreateTerminalParams):
            return self._terminals.create_terminal(parsed, root_set)
        if isinstance(parsed, TerminalOutputParams):
            return self._wrap_sync_terminal_output(parsed)
        if isinstance(parsed, WaitForTerminalExitParams):
            return self._terminals.wait_for_exit(parsed)
        if isinstance(parsed, KillTerminalParams):
            return self._terminals.kill_terminal(parsed)
        assert isinstance(parsed, ReleaseTerminalParams)
        return self._terminals.release_terminal(parsed)

    async def _wrap_sync_terminal_output(
        self,
        params: TerminalOutputParams,
    ) -> TerminalOutputResult:
        """Wrap synchronous ``get_output`` into an awaitable coroutine."""
        return self._terminals.get_output(params)

    def _relay_terminal_call(
        self,
        raw_line: str,
        req_id: int | str,
        method: str,
        parsed: Any,
        ws_caps: ClientCapabilities,
        root_set: SessionRootSet | None,
    ) -> AgentFrameDecision:
        """Verify WS client ``terminal`` capability and optional ``cwd`` before relaying."""
        if not ws_caps.terminal:
            return self._error_reply(
                req_id,
                JSONRPC_METHOD_NOT_FOUND,
                f"Client did not advertise '{method}'",
            )
        if isinstance(parsed, CreateTerminalParams) and parsed.cwd is not None:
            try:
                validate_path_in_root_set(parsed.cwd, root_set, allow_missing_leaf=False)
            except AcpError as exc:
                logger.debug("[%s] Rejected %s cwd: %s", self._connection_id, method, exc.message)
                return self._error_reply(req_id, exc.jsonrpc_code, exc.message, exc.wire_data)
        self._register_relayed(req_id, method, str(parsed.session_id))
        return AgentFrameDecision(forward_frame=raw_line)

    def _handle_terminal_request(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        ws_caps: ClientCapabilities,
        resolve_root_set: Callable[[str], SessionRootSet | None],
    ) -> AgentFrameDecision:
        """Dispatch ``terminal/*`` calls according to configured terminal policy."""
        req_id = extract_request_id(loaded)
        method = str(loaded.get("method"))
        assert req_id is not None
        policy = self._config.capabilities.terminal
        if policy == CapabilityPolicy.DISABLED:
            return self._error_reply(req_id, JSONRPC_METHOD_NOT_FOUND, f"'{method}' is disabled")

        raw_params = loaded.get("params")
        if not isinstance(raw_params, Mapping):
            return self._error_reply(req_id, JSONRPC_INVALID_PARAMS, f"Invalid {method} params")
        req_cls, _ = _TERMINAL_MODELS[method]
        try:
            parsed = req_cls.model_validate(raw_params)
        except PydanticValidationError as exc:
            err = pydantic_error_to_acp_error(exc, label=method)
            logger.debug("[%s] Invalid %s params: %s", self._connection_id, method, err.message)
            return self._error_reply(req_id, err.jsonrpc_code, err.message, err.wire_data)

        session_id = str(getattr(parsed, "session_id", ""))
        root_set = resolve_root_set(session_id)
        if policy == CapabilityPolicy.HOST:
            coro = self._build_host_terminal_coro(method, parsed, root_set)
            return self._spawn_host_task(req_id, method, session_id, coro)
        return self._relay_terminal_call(raw_line, req_id, method, parsed, ws_caps, root_set)

    def _handle_agent_extension_request(
        self,
        raw_line: str,
        req_id: int | str,
        method: str,
    ) -> AgentFrameDecision:
        """Gate and relay Agent -> Client ``_*`` extension requests."""
        if not is_valid_extension_method(method, self._config.capabilities):
            self._audit.record("extension_denied", outcome="denied", details={"method": method})
            return self._error_reply(req_id, JSONRPC_METHOD_NOT_FOUND, "Method not found")
        self._register_relayed(req_id, method, None)
        return AgentFrameDecision(forward_frame=raw_line)

    def _route_valid_agent_request(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        ws_caps: ClientCapabilities,
        resolve_root_set: Callable[[str], SessionRootSet | None],
    ) -> AgentFrameDecision:
        """Route a capacity-checked Agent -> Client JSON-RPC request."""
        req_id = extract_request_id(loaded)
        method = str(loaded.get("method"))
        assert req_id is not None
        decision: AgentFrameDecision | None = None
        if method == "session/request_permission":
            decision = self._handle_permission_request(raw_line, req_id, loaded.get("params"))
        elif method == "elicitation/create":
            decision = self._handle_elicitation_request(
                raw_line, req_id, loaded.get("params"), ws_caps
            )
        elif method in _FS_MODELS:
            decision = self._handle_fs_request(raw_line, loaded, ws_caps, resolve_root_set)
        elif method in _TERMINAL_MODELS:
            decision = self._handle_terminal_request(raw_line, loaded, ws_caps, resolve_root_set)
        elif method.startswith("_"):
            decision = self._handle_agent_extension_request(raw_line, req_id, method)
        return decision or self._error_reply(
            req_id,
            JSONRPC_METHOD_NOT_FOUND,
            f"Unsupported method '{method}'",
        )

    def inspect_agent_request(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        ws_caps: ClientCapabilities,
        resolve_root_set: Callable[[str], SessionRootSet | None],
    ) -> AgentFrameDecision:
        """Validate, gate, and route one Agent -> Client JSON-RPC request."""
        req_id = extract_request_id(loaded)
        method = loaded.get("method")
        if req_id is None or not isinstance(method, str) or not method:
            return AgentFrameDecision(forward_frame="")
        if self.pending_agent_request_count >= self._config.limits.max_pending_agent_requests:
            self._audit.record("pending_agent_limit_exceeded", outcome="denied")
            return self._error_reply(req_id, JSONRPC_INTERNAL_ERROR, "Too many pending requests")
        if req_id in self._pending_relayed or req_id in self._host_tasks:
            return self._error_reply(
                req_id,
                JSONRPC_INVALID_PARAMS,
                f"Duplicate request id {req_id!r}",
            )
        return self._route_valid_agent_request(raw_line, loaded, ws_caps, resolve_root_set)

    def inspect_agent_notification(
        self,
        raw_line: str,
        loaded: Mapping[str, Any],
        ws_caps: ClientCapabilities,
    ) -> AgentFrameDecision:
        """Validate and route non-``session/update`` Agent -> Client notifications."""
        method = loaded.get("method")
        if not isinstance(method, str) or not method:
            return AgentFrameDecision(forward_frame="")
        if method == "elicitation/complete":
            return self._handle_elicitation_complete(raw_line, loaded.get("params"), ws_caps)
        if method == "$/cancel_request":
            return self._handle_agent_cancel_request(raw_line, loaded.get("params"))
        if method.startswith("_") and is_valid_extension_method(method, self._config.capabilities):
            return AgentFrameDecision(forward_frame=raw_line)
        logger.debug("[%s] Ignoring unknown agent notification '%s'", self._connection_id, method)
        return AgentFrameDecision(forward_frame="")

    def _handle_elicitation_complete(
        self,
        raw_line: str,
        raw_params: object,
        ws_caps: ClientCapabilities,
    ) -> AgentFrameDecision:
        """Correlate ``elicitation/complete`` against outstanding URL elicitations."""
        if ws_caps.elicitation is None or ws_caps.elicitation.url is None:
            return AgentFrameDecision(forward_frame="")
        if not isinstance(raw_params, Mapping):
            return AgentFrameDecision(forward_frame="")
        try:
            parsed = CompleteElicitationParams.model_validate(raw_params)
        except PydanticValidationError as exc:
            logger.debug(
                "[%s] Dropping malformed elicitation/complete: %s",
                self._connection_id,
                exc,
            )
            return AgentFrameDecision(forward_frame="")
        if parsed.elicitation_id not in self._active_url_elicitation_ids:
            return AgentFrameDecision(forward_frame="")
        self._active_url_elicitation_ids.discard(parsed.elicitation_id)
        return AgentFrameDecision(forward_frame=raw_line)

    def _handle_agent_cancel_request(
        self,
        raw_line: str,
        raw_params: object,
    ) -> AgentFrameDecision:
        """Cancel matching host task or relay ``$/cancel_request`` to WS client."""
        try:
            target_id, _ = prepare_cancel_request_params(raw_params)
        except AcpError as exc:
            logger.debug(
                "[%s] Dropping malformed agent $/cancel_request: %s",
                self._connection_id,
                exc,
            )
            return AgentFrameDecision(forward_frame="")
        host_task = self._host_tasks.get(target_id)
        if host_task is not None:
            host_task.cancel()
            return AgentFrameDecision(forward_frame="")
        if target_id in self._pending_relayed:
            return AgentFrameDecision(forward_frame=raw_line)
        return AgentFrameDecision(forward_frame="")

    def _validate_relayed_op_result(
        self,
        method: str,
        msg: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate client response payload for relayed ``fs/*`` and ``terminal/*`` calls."""
        if "error" in msg:
            return scrub_outbound_message_meta(msg)
        raw_result = msg.get("result")
        if not isinstance(raw_result, Mapping):
            raise AcpInvalidParamsError(f"Invalid {method} response: expected JSON object")
        pair = _FS_MODELS.get(method) or _TERMINAL_MODELS.get(method)
        if pair is not None:
            _, res_cls = pair
            try:
                res_cls.model_validate(raw_result)
            except PydanticValidationError as exc:
                raise pydantic_error_to_acp_error(exc, label=f"{method} result") from exc
        return scrub_outbound_message_meta(msg)

    def prepare_client_response(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        """Correlate and validate a Client -> Agent response to a relayed request."""
        req_id = extract_request_id(msg)
        has_single_body = ("result" in msg) != ("error" in msg)
        if "id" not in msg or req_id is None or not has_single_body:
            raise AcpInvalidRequestError("Invalid JSON-RPC response envelope")
        pending = self._pending_relayed.get(req_id)
        if pending is None:
            raise AcpInvalidRequestError(
                f"Unknown or already-resolved agent request id: {req_id!r}"
            )

        if pending.method == "session/request_permission":
            perm = self._pending_permissions[req_id]
            validated = validate_permission_response(msg, perm)
            self._pending_permissions.pop(req_id, None)
            self._pending_relayed.pop(req_id, None)
            return validated
        if pending.method == "elicitation/create":
            elic = self._pending_elicitations[req_id]
            validated = validate_elicitation_response(msg, elic)
            self._pending_elicitations.pop(req_id, None)
            self._pending_relayed.pop(req_id, None)
            res = validated.get("result")
            if elic.elicitation_id is not None and (
                "error" in validated
                or (isinstance(res, Mapping) and res.get("action") in ("decline", "cancel"))
            ):
                self._active_url_elicitation_ids.discard(elic.elicitation_id)
            return validated
        if pending.method in _FS_MODELS or pending.method in _TERMINAL_MODELS:
            validated = self._validate_relayed_op_result(pending.method, msg)
            self._pending_relayed.pop(req_id, None)
            return validated
        self._pending_relayed.pop(req_id, None)
        return scrub_outbound_message_meta(msg)

    def _drain_pending_relayed(
        self,
        session_ids: set[str] | None,
    ) -> list[dict[str, Any]]:
        """Collect cancellation replies for unresolved permission and elicitation requests."""
        replies: list[dict[str, Any]] = []
        for req_id, perm in list(self._pending_permissions.items()):
            if session_ids is None or perm.session_id in session_ids:
                self._pending_permissions.pop(req_id, None)
                self._pending_relayed.pop(req_id, None)
                replies.append(build_cancelled_permission_response(req_id))
        for req_id, elic in list(self._pending_elicitations.items()):
            if session_ids is None or elic.session_id in session_ids:
                self._pending_elicitations.pop(req_id, None)
                self._pending_relayed.pop(req_id, None)
                if elic.elicitation_id is not None:
                    self._active_url_elicitation_ids.discard(elic.elicitation_id)
                replies.append(build_cancelled_elicitation_response(req_id))
        for req_id, req in list(self._pending_relayed.items()):
            if session_ids is None or req.session_id in session_ids:
                self._pending_relayed.pop(req_id, None)
        return replies

    async def _cancel_tasks_and_release(
        self,
        tasks: list[asyncio.Task[None]],
        session_ids: set[str] | None,
    ) -> None:
        """Cancel and await ``tasks``, then release matching host terminals."""
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if session_ids is None:
            await self._terminals.release_all()
            return
        for sid in session_ids:
            await self._terminals.release_session(sid)

    async def teardown_sessions(
        self,
        session_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Cancel matching host tasks, drain pending replies, and release host terminals."""
        replies = self._drain_pending_relayed(session_ids)
        if session_ids is None:
            tasks = list(self._host_tasks.values())
            self._host_tasks.clear()
            self._host_task_sessions.clear()
            self._active_url_elicitation_ids.clear()
            await self._cancel_tasks_and_release(tasks, None)
            return replies

        matching_tasks = [
            task
            for req_id, task in list(self._host_tasks.items())
            if self._host_task_sessions.get(req_id) in session_ids
            and self._host_tasks.pop(req_id, None) is not None
            and self._host_task_sessions.pop(req_id, None) is not None
        ]
        await self._cancel_tasks_and_release(matching_tasks, session_ids)
        return replies


__all__ = [
    "ClientRequestDispatcher",
]
