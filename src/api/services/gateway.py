"""Hardened WebSocket gateway enforcing Origin, Host, limits, and ACP session lifecycle."""

import asyncio
import secrets

from fastapi import WebSocket, WebSocketDisconnect

from api.models.acp import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_REQUEST,
    AcpConnectionState,
    SessionStepOutcome,
)
from api.models.config import ApiServerConfig
from api.models.session import SessionRuntimeState
from api.services.session import AcpConnectionSession
from api.utils.formatting import format_kv_block, format_raw_frame_for_log
from api.utils.jsonrpc import format_jsonrpc_error
from api.utils.security import is_allowed_ws_host, is_allowed_ws_origin
from core.models.exceptions import AcpError
from core.utils.logging import get_logger

logger = get_logger("api.services.gateway")


class AcpWebSocketGateway:
    """FastAPI ``/acp`` WebSocket handler enforcing CSWSH, Host, and connection limits."""

    def __init__(self, config: ApiServerConfig) -> None:
        """Initialize the WebSocket gateway with immutable server security configuration."""
        self._config = config
        self._active_sessions: set[AcpConnectionSession] = set()

    @property
    def active_connection_count(self) -> int:
        """Return number of currently open ``/acp`` WebSocket connections."""
        return len(self._active_sessions)

    def get_session_state(self, session_id: str) -> SessionRuntimeState | None:
        """Look up live ``SessionRuntimeState`` for ``session_id`` across active connections."""
        for conn in tuple(self._active_sessions):
            state = conn.get_session_state(session_id)
            if state is not None:
                return state
        return None

    def _is_authorized_upgrade(self, websocket: WebSocket) -> bool:
        """Verify HTTP ``Host`` (anti-DNS-rebinding) and ``Origin`` (anti-CSWSH)."""
        host_header = websocket.headers.get("host")
        origin_header = websocket.headers.get("origin")
        host_ok = is_allowed_ws_host(
            host_header,
            allow_non_loopback=self._config.allow_non_loopback,
        )
        origin_ok = is_allowed_ws_origin(origin_header, self._config.allowed_origins)
        if not host_ok or not origin_ok:
            logger.warning(
                "Rejected /acp WebSocket upgrade\n%s",
                format_kv_block(
                    {
                        "host": f"{host_header!r} (allowed={host_ok})",
                        "origin": f"{origin_header!r} (allowed={origin_ok})",
                    }
                ),
            )
            return False
        return True

    @staticmethod
    async def _receive_text_frame(websocket: WebSocket) -> str | None:
        """Read next ASGI WebSocket event, returning text payload or ``None`` for binary."""
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(code=int(message.get("code", 1000)))
        raw_text = message.get("text")
        if raw_text is None:
            logger.debug("Ignoring non-text frame on /acp WebSocket")
            return None
        return str(raw_text)

    async def _emit_outcome(self, websocket: WebSocket, outcome: SessionStepOutcome) -> bool:
        """Send response frames in ``outcome`` and return ``False`` when closing."""
        for frame in outcome.frames:
            await websocket.send_text(frame)
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
            if raw_text is None:
                continue
            outcome = await session.handle_init_frame(raw_text)
            if not await self._emit_outcome(websocket, outcome):
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
            if raw_text is None:
                continue
            outcome = await session.handle_ready_frame(raw_text)
            if not await self._emit_outcome(websocket, outcome):
                break

    async def _forward_agent_frames(
        self,
        websocket: WebSocket,
        session: AcpConnectionSession,
    ) -> None:
        """Relay ``agy acp`` stdout NDJSON messages back over the WebSocket."""
        while True:
            frame = await session.read_agent_frame()
            if frame is None:
                logger.info("[%s] agy stdout closed (EOF)", session.connection_id)
                break
            if frame:
                label, pretty = format_raw_frame_for_log(frame)
                logger.info("[%s] agy -> UI (%s)\n%s", session.connection_id, label, pretty)
                await websocket.send_text(frame)

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
            async with asyncio.timeout(self._config.initialize_timeout_seconds):
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
            err = format_jsonrpc_error(None, JSONRPC_INTERNAL_ERROR, str(exc))
            await self._send_error_and_close(websocket, err, 1011)
        except Exception as exc:
            logger.exception("Unhandled ACP gateway error on %s", session.connection_id)
            err = format_jsonrpc_error(None, JSONRPC_INTERNAL_ERROR, f"Internal ACP error: {exc}")
            await self._send_error_and_close(websocket, err, 1011)

    async def handle_websocket(self, websocket: WebSocket) -> None:
        """Enforce security checks before accepting and manage connection lifecycle."""
        if not self._is_authorized_upgrade(websocket):
            await websocket.close(code=1008)
            return
        if len(self._active_sessions) >= self._config.max_connections:
            logger.warning(
                "Rejecting /acp connection: max_connections=%d reached",
                self._config.max_connections,
            )
            await websocket.close(code=1013)
            return

        connection_id = f"acp_{secrets.token_urlsafe(18)}"
        session = AcpConnectionSession(connection_id, self._config)
        self._active_sessions.add(session)
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
                        "active": f"{len(self._active_sessions)}/{self._config.max_connections}",
                    }
                ),
            )
            await self._drive_session(websocket, session)
        finally:
            self._active_sessions.discard(session)
            await session.close()

    async def close_all(self) -> None:
        """Terminate all active ACP sessions during FastAPI shutdown."""
        sessions = list(self._active_sessions)
        self._active_sessions.clear()
        for session in sessions:
            await session.close()


__all__ = [
    "AcpWebSocketGateway",
]
