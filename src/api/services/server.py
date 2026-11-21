"""FastAPI application factory and Uvicorn runner for the ACP bridge and UI API."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from api.models.config import ApiServerConfig, HealthStatus
from api.services.gateway import AcpWebSocketGateway
from api.utils.formatting import configure_api_terminal_logging, format_kv_block
from api.utils.security import build_agy_acp_command, validate_bind_host
from core.models.config import AppConfig
from core.models.enums import APP_VERSION
from core.utils.logging import get_logger
from engine.services.facade import IndexerEngine

logger = get_logger("api.services.server")


class ApiServerRunner:
    """Constructs the FastAPI ACP bridge app and runs Uvicorn cleanly."""

    def __init__(
        self,
        api_config: ApiServerConfig | None = None,
        app_config: AppConfig | None = None,
        *,
        engine: IndexerEngine | None = None,
    ) -> None:
        """Initialize the FastAPI server runner with optional injected ``IndexerEngine``."""
        base_api_cfg = api_config if api_config is not None else ApiServerConfig()
        self._app_config = app_config if app_config is not None else AppConfig()
        self._owns_engine = engine is None
        self._engine = engine if engine is not None else IndexerEngine(self._app_config)
        self._api_config = (
            base_api_cfg
            if base_api_cfg.cbi_db_path is not None
            else base_api_cfg.model_copy(update={"cbi_db_path": self._engine.db_path})
        )
        self._gateway = AcpWebSocketGateway(self._api_config)

    @property
    def engine(self) -> IndexerEngine:
        """Return underlying ``IndexerEngine`` bound to this server runner."""
        return self._engine

    @property
    def gateway(self) -> AcpWebSocketGateway:
        """Return ``AcpWebSocketGateway`` driving ``WS /acp``."""
        return self._gateway

    async def _health_endpoint(self) -> HealthStatus:
        """Emit structured FastAPI server and supported ACP protocol status."""
        default_cwd = str((self._api_config.workspace_root or Path.cwd()).resolve())
        return HealthStatus(
            app=self._app_config.app_name,
            version=APP_VERSION,
            default_cwd=default_cwd,
            cbi_mcp_enabled=self._api_config.enable_cbi_mcp,
        )

    def create_app(self) -> FastAPI:
        """Validate bind host security rules and build the configured ``FastAPI`` app."""
        validate_bind_host(
            self._api_config.host,
            allow_non_loopback=self._api_config.allow_non_loopback,
        )

        @asynccontextmanager
        async def _lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
            try:
                yield
            finally:
                await self._gateway.close_all()

        app = FastAPI(
            title="Codebase Indexer ACP API",
            version=APP_VERSION,
            lifespan=_lifespan,
        )
        app.add_api_route(
            "/healthz",
            self._health_endpoint,
            methods=["GET"],
            response_model=HealthStatus,
        )
        app.add_api_websocket_route("/acp", self._gateway.handle_websocket)
        return app

    def run(self) -> None:
        """Start the Uvicorn ASGI server and close owned ``IndexerEngine`` on exit."""
        configure_api_terminal_logging(debug=self._app_config.debug)
        app = self.create_app()
        logger.info(
            "Starting FastAPI ACP bridge\n%s",
            format_kv_block(
                {
                    "bind": f"{self._api_config.host}:{self._api_config.port}",
                    "agy_command": " ".join(build_agy_acp_command(self._api_config)),
                    "allowed_origins": ", ".join(self._api_config.allowed_origins),
                    "max_connections": self._api_config.max_connections,
                }
            ),
        )
        try:
            uvicorn.run(
                app,
                host=self._api_config.host,
                port=self._api_config.port,
                ws_max_size=self._api_config.max_frame_bytes,
                log_level="debug" if self._app_config.debug else "info",
            )
        finally:
            if self._owns_engine:
                self._engine.close()


__all__ = [
    "ApiServerRunner",
]
