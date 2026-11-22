"""FastAPI ACP server configuration and health status models."""

from pathlib import Path

from pydantic import Field

from core.models.base import FrozenModel

DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)
DEFAULT_AGY_APP_DATA_DIR: str = "antigravity"


class ApiServerConfig(FrozenModel):
    """Immutable runtime and security configuration for the FastAPI ACP bridge."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    allow_non_loopback: bool = False
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    agy_command: tuple[str, ...] = ("agy", "acp", "--stdio")
    agy_sandbox: bool = True
    agy_app_data_dir: str | None = DEFAULT_AGY_APP_DATA_DIR
    agy_gemini_dir: str | None = None
    workspace_root: Path | None = None
    enable_cbi_mcp: bool = True
    cbi_db_path: Path | None = None
    chat_db_path: Path | None = None
    watch_agy_profile: bool | None = None
    max_connections: int = Field(default=4, ge=1)
    max_frame_bytes: int = Field(default=1_048_576, ge=4096)
    initialize_timeout_seconds: float = Field(default=15.0, gt=0.0)
    terminate_wait_seconds: float = Field(default=2.0, gt=0.0)
    stderr_history_limit: int = Field(default=200, ge=1)


class HealthStatus(FrozenModel):
    """Structured liveness payload emitted by ``GET /healthz``."""

    status: str = "ok"
    app: str
    version: str
    supported_acp_versions: tuple[int, ...] = (1,)
    default_cwd: str = ""
    cbi_mcp_enabled: bool = True
    agy_app_data_dir: str = DEFAULT_AGY_APP_DATA_DIR
    agy_profile_dir: str = f"~/.gemini/{DEFAULT_AGY_APP_DATA_DIR}"


__all__ = [
    "ApiServerConfig",
    "DEFAULT_AGY_APP_DATA_DIR",
    "DEFAULT_ALLOWED_ORIGINS",
    "HealthStatus",
]
