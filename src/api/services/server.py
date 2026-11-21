"""FastAPI application factory and Uvicorn runner for the ACP bridge and UI API."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from api.models.config import ApiServerConfig, HealthStatus
from api.models.session import SessionArtifactContent, SessionArtifactList
from api.services.gateway import AcpWebSocketGateway
from api.utils.artifacts import list_session_artifacts, read_session_artifact
from api.utils.formatting import configure_api_terminal_logging, format_kv_block
from api.utils.security import (
    build_agy_acp_command,
    format_agy_profile_display,
    is_allowed_ws_host,
    is_allowed_ws_origin,
    resolve_agy_profile_root,
    validate_bind_host,
)
from core.models.config import AppConfig
from core.models.enums import APP_VERSION
from core.models.exceptions import AcpError
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

    def _verify_http_request(self, request: Request) -> None:
        """Enforce Host anti-DNS-rebinding and Origin allowlist checks on API routes."""
        if not is_allowed_ws_host(
            request.headers.get("host"),
            allow_non_loopback=self._api_config.allow_non_loopback,
        ):
            raise HTTPException(status_code=403, detail="Disallowed Host header.")
        origin = request.headers.get("origin")
        if origin is not None and not is_allowed_ws_origin(
            origin,
            self._api_config.allowed_origins,
        ):
            raise HTTPException(status_code=403, detail="Disallowed Origin header.")

    async def _health_endpoint(self) -> HealthStatus:
        """Emit structured FastAPI server and supported ACP protocol status."""
        default_cwd = str((self._api_config.workspace_root or Path.cwd()).resolve())
        return HealthStatus(
            app=self._app_config.app_name,
            version=APP_VERSION,
            default_cwd=default_cwd,
            cbi_mcp_enabled=self._api_config.enable_cbi_mcp,
            agy_app_data_dir=self._api_config.agy_app_data_dir or "",
            agy_profile_dir=format_agy_profile_display(self._api_config),
        )

    async def _list_session_artifacts_endpoint(
        self,
        session_id: str,
        request: Request,
    ) -> SessionArtifactList:
        """Return read-only ``brain/<session_id>/*.md`` artifact summaries."""
        self._verify_http_request(request)
        profile_root = resolve_agy_profile_root(self._api_config)
        try:
            return list_session_artifacts(profile_root, session_id)
        except AcpError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc

    async def _read_session_artifact_endpoint(
        self,
        session_id: str,
        artifact_name: str,
        request: Request,
    ) -> SessionArtifactContent:
        """Return read-only markdown content for ``brain/<session_id>/<artifact_name>``."""
        self._verify_http_request(request)
        profile_root = resolve_agy_profile_root(self._api_config)
        try:
            artifact = read_session_artifact(profile_root, session_id, artifact_name)
        except AcpError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        if artifact is None:
            raise HTTPException(
                status_code=404,
                detail=f"Artifact '{artifact_name}' not found for session '{session_id}'.",
            )
        return artifact

    def create_app(self) -> FastAPI:
        """Validate bind host security rules and build the configured ``FastAPI`` app."""
        validate_bind_host(
            self._api_config.host,
            allow_non_loopback=self._api_config.allow_non_loopback,
        )
        build_agy_acp_command(self._api_config)

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
        app.add_api_route(
            "/api/sessions/{session_id}/artifacts",
            self._list_session_artifacts_endpoint,
            methods=["GET"],
            response_model=SessionArtifactList,
        )
        app.add_api_route(
            "/api/sessions/{session_id}/artifacts/{artifact_name}",
            self._read_session_artifact_endpoint,
            methods=["GET"],
            response_model=SessionArtifactContent,
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
                    "agy_profile": format_agy_profile_display(self._api_config),
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
