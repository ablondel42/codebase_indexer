"""ACP connection initialization state machine and agent bridge session."""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from api.models.acp import (
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    SUPPORTED_ACP_VERSIONS,
    AcpConnectionState,
    InitializeParams,
    InitializeResult,
    SessionStepOutcome,
)
from api.models.config import ApiServerConfig
from api.services.process import AgyAcpProcess
from api.utils.formatting import (
    describe_jsonrpc_frame,
    format_raw_frame_for_log,
    indent_json_payload,
)
from api.utils.jsonrpc import extract_request_id, format_jsonrpc_error, parse_client_frame
from api.utils.security import clamp_client_capabilities
from core.models.exceptions import AcpError
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
    ) -> None:
        """Create a new uninitialized session bound to ``connection_id``."""
        self._connection_id = connection_id
        self._config = config
        self._process = process if process is not None else AgyAcpProcess(config)
        self._state = AcpConnectionState.UNINITIALIZED
        self._initialize_result: InitializeResult | None = None

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
        return await self._finalize_init_response(req_id, raw_line, agent_msg)

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

    async def handle_ready_frame(self, raw_text: str) -> SessionStepOutcome:
        """Validate and forward a post-initialization JSON-RPC frame to ``agy acp``."""
        msg, parse_err = parse_client_frame(raw_text, max_bytes=self._config.max_frame_bytes)
        if parse_err is not None or msg is None:
            return self._log_rejection_outcome(SessionStepOutcome(frames=(parse_err or "",)))

        frame_label = describe_jsonrpc_frame(msg)
        pretty_msg = indent_json_payload(msg)
        logger.info("[%s] UI -> Bridge (%s)\n%s", self._connection_id, frame_label, pretty_msg)
        if msg.get("method") == "initialize":
            return self._log_rejection_outcome(
                SessionStepOutcome(
                    frames=(
                        format_jsonrpc_error(
                            extract_request_id(msg),
                            JSONRPC_INVALID_REQUEST,
                            "Connection already initialized",
                        ),
                    )
                )
            )
        logger.info("[%s] Bridge -> agy (%s)\n%s", self._connection_id, frame_label, pretty_msg)
        await self._process.send_line(msg)
        return SessionStepOutcome()

    async def read_agent_frame(self) -> str | None:
        """Read the next outbound NDJSON frame emitted by ``agy acp``."""
        return await self._process.read_line()

    async def close(self) -> None:
        """Close the session and terminate its backing ``agy acp`` subprocess."""
        self._state = AcpConnectionState.CLOSED
        await self._process.stop()


__all__ = [
    "AcpConnectionSession",
]
