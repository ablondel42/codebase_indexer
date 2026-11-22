"""Pure WebSocket ASGI application factory and Uvicorn runner for ACP v1."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from api.models.config import AcpServerConfig, LogFormat
from api.services.gateway import AcpWebSocketGateway
from api.services.logging import AcpLoggingConfigurator
from api.utils.formatting import format_kv_block
from api.utils.security import build_agent_command, format_agy_profile_display, validate_bind_host
from core.models.config import AppConfig
from core.models.enums import APP_VERSION
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine

logger = get_logger("api.services.server")


class ApiServerRunner:
    """Constructs the WebSocket-only ACP FastAPI app and runs Uvicorn cleanly."""

    def __init__(
        self,
        api_config: AcpServerConfig | None = None,
        app_config: AppConfig | None = None,
        *,
        engine: IndexerEngine | None = None,
    ) -> None:
        """Initialize the pure-WebSocket ACP server runner with optional ``IndexerEngine``."""
        base_cfg = api_config if api_config is not None else AcpServerConfig()
        self._app_config = app_config if app_config is not None else AppConfig()
        self._owns_engine = engine is None
        self._engine = engine if engine is not None else IndexerEngine(self._app_config)
        resolved_cbi_db = (
            base_cfg.mcp.cbi_db_path
            if base_cfg.mcp.cbi_db_path is not None
            else self._engine.db_path
        )
        updated_mcp = base_cfg.mcp.model_copy(update={"cbi_db_path": resolved_cbi_db})
        updated_logging = (
            base_cfg.logging.model_copy(update={"level": "DEBUG"})
            if self._app_config.debug and base_cfg.logging.level != "DEBUG"
            else base_cfg.logging
        )
        self._api_config = base_cfg.model_copy(
            update={"mcp": updated_mcp, "logging": updated_logging}
        )
        self._gateway = AcpWebSocketGateway(self._api_config)

    @property
    def engine(self) -> IndexerEngine:
        """Return underlying ``IndexerEngine`` bound to this server runner."""
        return self._engine

    @property
    def gateway(self) -> AcpWebSocketGateway:
        """Return ``AcpWebSocketGateway`` driving the ACP WebSocket route."""
        return self._gateway

    @property
    def api_config(self) -> AcpServerConfig:
        """Return resolved immutable ``AcpServerConfig`` for this server."""
        return self._api_config

    def create_app(self) -> FastAPI:
        """Validate security rules and build the pure WebSocket-only ``FastAPI`` app."""
        validate_bind_host(
            self._api_config.transport.host,
            allow_non_loopback=self._api_config.transport.allow_non_loopback,
        )
        build_agent_command(self._api_config.agent)

        @asynccontextmanager
        async def _lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
            try:
                yield
            finally:
                await self._gateway.close_all()

        app = FastAPI(
            title="Codebase Indexer ACP WebSocket Gateway",
            version=APP_VERSION,
            lifespan=_lifespan,
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        app.add_api_websocket_route(
            self._api_config.transport.ws_path,
            self._gateway.handle_websocket,
        )
        return app

    def run(self) -> None:
        """Configure structured logging, start Uvicorn, and close owned engine on exit."""
        AcpLoggingConfigurator(self._api_config.logging).configure()
        app = self.create_app()
        t_cfg = self._api_config.transport
        c_cfg = self._api_config.capabilities
        logger.info(
            "Starting pure ACP v1 WebSocket gateway\n%s",
            format_kv_block(
                {
                    "ws_url": f"ws://{t_cfg.host}:{t_cfg.port}{t_cfg.ws_path}",
                    "agent_command": " ".join(build_agent_command(self._api_config.agent)),
                    "agy_profile": format_agy_profile_display(self._api_config.agent),
                    "fs_policy": f"read={c_cfg.fs_read.value}, write={c_cfg.fs_write.value}",
                    "terminal_policy": c_cfg.terminal.value,
                    "log_format": self._api_config.logging.format.value,
                    "max_connections": t_cfg.max_connections,
                }
            ),
        )
        uvicorn_level = (
            "debug"
            if self._api_config.logging.level == "DEBUG"
            else ("warning" if self._api_config.logging.format == LogFormat.JSON else "info")
        )
        try:
            uvicorn.run(
                app,
                host=t_cfg.host,
                port=t_cfg.port,
                ws_max_size=t_cfg.max_frame_bytes,
                ws_ping_interval=t_cfg.ws_ping_interval_seconds,
                ws_ping_timeout=t_cfg.ws_ping_timeout_seconds,
                log_level=uvicorn_level,
            )
        finally:
            if self._owns_engine:
                self._engine.close()


__all__ = [
    "ApiServerRunner",
]
