"""Hardened pure-ACP WebSocket gateway enforcing Host, Origin, Bearer auth, and limits."""

import asyncio
import secrets

from fastapi import WebSocket, WebSocketDisconnect

from api.models.acp import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_REQUEST,
    AcpConnectionState,
    SessionStepOutcome,
)
from api.models.config import AcpServerConfig, LogFormat
from api.services.logging import AcpAuditLogger, AcpLogContext
from api.services.session import AcpConnectionSession
from api.utils.errors import exception_to_jsonrpc_frame, format_jsonrpc_error
from api.utils.formatting import format_kv_block, format_raw_frame_for_log
from api.utils.security import verify_ws_upgrade
from core.models.exceptions import AcpError
from core.utils.logging import get_logger

logger = get_logger("api.services.gateway")


class AcpWebSocketGateway:
    """Pure JSON-RPC 2.0 ACP v1 WebSocket gateway with fail-closed upgrade security."""

    def __init__(self, config: AcpServerConfig) -> None:
        """Initialize the WebSocket gateway with immutable server configuration."""
        self._config = config
        self._audit = AcpAuditLogger(enabled=config.logging.log_audit_events)
        self._active_sessions: set[AcpConnectionSession] = set()
        self._socket_entries: dict[AcpConnectionSession, tuple[WebSocket, asyncio.Lock]] = {}

    @property
    def active_connection_count(self) -> int:
        """Return number of currently open ACP WebSocket connections."""
        return len(self._active_sessions)

    def _is_debug_errors(self) -> bool:
        """Return ``True`` when DEV pretty DEBUG mode permits detailed error messages."""
        return (
            self._config.logging.format == LogFormat.PRETTY
            and self._config.logging.level == "DEBUG"
        )

    def _is_authorized_upgrade(self, websocket: WebSocket) -> bool:
        """Verify HTTP ``Host``, hybrid ``Origin``, and optional Bearer token."""
        headers = {key.lower(): val for key, val in websocket.headers.items()}
        authorized, reason = verify_ws_upgrade(headers, self._config.transport)
        if not authorized:
            self._audit.record(
                "ws_upgrade_rejected",
                outcome="rejected",
                details={
                    "reason": reason,
                    "host": websocket.headers.get("host") or "(none)",
                    "origin": websocket.headers.get("origin") or "(none)",
                },
            )
            logger.warning(
                "Rejected %s WebSocket upgrade (%s)\n%s",
                self._config.transport.ws_path,
                reason,
                format_kv_block(
                    {
                        "reason": reason,
                        "host": websocket.headers.get("host") or "(none)",
                        "origin": websocket.headers.get("origin") or "(none)",
                    }
                ),
            )
            return False
        self._audit.record(
            "ws_upgrade_accepted",
            outcome="allowed",
            details={"reason": reason},
        )
        return True

    @staticmethod
    async def _receive_text_frame(websocket: WebSocket) -> str:
        """Read next ASGI WebSocket event, rejecting binary frames with close ``1003``."""
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(code=int(message.get("code", 1000)))
        raw_text = message.get("text")
        if raw_text is None:
            await websocket.close(code=1003)
            raise WebSocketDisconnect(code=1003)
        return str(raw_text)

    async def _send_text_locked(
        self,
        session: AcpConnectionSession,
        websocket: WebSocket,
        frame: str,
    ) -> None:
        """Send one WebSocket frame under the connection's send lock."""
        entry = self._socket_entries.get(session)
        if entry is None:
            await websocket.send_text(frame)
            return
        async with entry[1]:
            await websocket.send_text(frame)

    async def _emit_outcome(
        self,
        session: AcpConnectionSession,
        websocket: WebSocket,
        outcome: SessionStepOutcome,
    ) -> bool:
        """Send response frames in ``outcome`` and return ``False`` when closing."""
        for frame in outcome.frames:
            await self._send_text_locked(session, websocket, frame)
        if outcome.close_code is not None:
            await websocket.close(code=outcome.close_code)
            return False
        return True

    async def _run_init_phase(
        self,
        websocket: WebSocket,
        session: AcpConnectionSession,
    ) -> bool:
        """Drive client messages until ACP initialization succeeds or closes."""
        while session.state == AcpConnectionState.UNINITIALIZED:
            raw_text = await self._receive_text_frame(websocket)
            outcome = await session.handle_init_frame(raw_text)
            if not await self._emit_outcome(session, websocket, outcome):
                return False
        return session.state == AcpConnectionState.READY

    async def _forward_client_frames(
        self,
        websocket: WebSocket,
        session: AcpConnectionSession,
    ) -> None:
        """Forward authenticated client JSON-RPC frames to the initialized subprocess."""
        while True:
            raw_text = await self._receive_text_frame(websocket)
            outcome = await session.handle_ready_frame(raw_text)
            if not await self._emit_outcome(session, websocket, outcome):
                break

    async def _forward_agent_frames(
        self,
        websocket: WebSocket,
        session: AcpConnectionSession,
    ) -> None:
        """Relay agent stdout NDJSON messages back over the WebSocket."""
        while True:
            frame = await session.read_agent_frame()
            if frame is None:
                logger.info("[%s] Agent stdout closed (EOF)", session.connection_id)
                break
            if frame:
                if self._config.logging.effective_log_payloads:
                    label, pretty = format_raw_frame_for_log(frame)
                    logger.info(
                        "[%s] Agent -> UI (%s)\n%s",
                        session.connection_id,
                        label,
                        pretty,
                    )
                else:
                    label, _ = format_raw_frame_for_log(frame)
                    logger.info("[%s] Agent -> UI (%s)", session.connection_id, label)
                await self._send_text_locked(session, websocket, frame)

    async def _run_ready_relay(
        self,
        websocket: WebSocket,
        session: AcpConnectionSession,
    ) -> None:
        """Run bidirectional client <-> subprocess frame pumps after initialization."""
        client_task = asyncio.create_task(self._forward_client_frames(websocket, session))
        agent_task = asyncio.create_task(self._forward_agent_frames(websocket, session))
        done, pending = await asyncio.wait(
            {client_task, agent_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    @staticmethod
    async def _send_error_and_close(
        websocket: WebSocket,
        err_json: str,
        close_code: int,
    ) -> None:
        """Attempt structured JSON-RPC error delivery and clean WebSocket closure."""
        try:
            await websocket.send_text(err_json)
            await websocket.close(code=close_code)
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            logger.debug("Skipping error send on already-closed WebSocket: %s", exc)

    async def _drive_session(
        self,
        websocket: WebSocket,
        session: AcpConnectionSession,
    ) -> None:
        """Execute timeout-guarded init and post-init relay within error boundary."""
        try:
            async with asyncio.timeout(self._config.agent.initialize_timeout_seconds):
                should_relay = await self._run_init_phase(websocket, session)
            if should_relay:
                await self._run_ready_relay(websocket, session)
        except WebSocketDisconnect as exc:
            logger.info("[%s] WebSocket disconnected (code=%s)", session.connection_id, exc.code)
        except TimeoutError as exc:
            logger.warning("ACP initialization timed out for %s: %s", session.connection_id, exc)
            err = format_jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "ACP initialization timeout")
            await self._send_error_and_close(websocket, err, 1008)
        except AcpError as exc:
            logger.warning("ACP protocol error on %s: %s", session.connection_id, exc)
            err = (
                exception_to_jsonrpc_frame(
                    exc,
                    None,
                    debug=self._is_debug_errors(),
                )
                or format_jsonrpc_error(None, JSONRPC_INTERNAL_ERROR, "Internal error")
            )
            await self._send_error_and_close(websocket, err, 1011)
        except Exception as exc:
            logger.exception("Unhandled ACP gateway error on %s", session.connection_id)
            detail = f"Internal ACP error: {exc}" if self._is_debug_errors() else "Internal error"
            err = format_jsonrpc_error(None, JSONRPC_INTERNAL_ERROR, detail)
            await self._send_error_and_close(websocket, err, 1011)

    async def handle_websocket(self, websocket: WebSocket) -> None:
        """Enforce security checks before accepting and manage connection lifecycle."""
        if not self._is_authorized_upgrade(websocket):
            await websocket.close(code=1008)
            return
        if len(self._active_sessions) >= self._config.transport.max_connections:
            self._audit.record("max_connections_reached", outcome="rejected")
            logger.warning(
                "Rejecting %s connection: max_connections=%d reached",
                self._config.transport.ws_path,
                self._config.transport.max_connections,
            )
            await websocket.close(code=1013)
            return

        connection_id = f"acp_{secrets.token_urlsafe(18)}"
        session = AcpConnectionSession(connection_id, self._config, audit=self._audit)
        self._active_sessions.add(session)
        self._socket_entries[session] = (websocket, asyncio.Lock())
        with AcpLogContext.bind(connection_id=connection_id):
            try:
                await websocket.accept(
                    headers=[(b"acp-connection-id", connection_id.encode("ascii"))]
                )
                logger.info(
                    "[%s] WebSocket connected\n%s",
                    connection_id,
                    format_kv_block(
                        {
                            "host": websocket.headers.get("host") or "(none)",
                            "origin": websocket.headers.get("origin") or "(none)",
                            "active": (
                                f"{len(self._active_sessions)}/"
                                f"{self._config.transport.max_connections}"
                            ),
                        }
                    ),
                )
                await self._drive_session(websocket, session)
            finally:
                self._socket_entries.pop(session, None)
                self._active_sessions.discard(session)
                await session.close()

    async def close_all(self) -> None:
        """Terminate all active ACP sessions during FastAPI shutdown."""
        sessions = list(self._active_sessions)
        self._socket_entries.clear()
        self._active_sessions.clear()
        for session in sessions:
            await session.close()


__all__ = [
    "AcpWebSocketGateway",
]
