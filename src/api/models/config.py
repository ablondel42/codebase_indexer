"""Composed immutable ACP v1 WebSocket server configuration models."""

import ipaddress
from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr, model_validator

from core.models.base import FrozenModel
from core.utils.logging import get_logger

logger = get_logger("api.models.config")

DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)
DEFAULT_AGY_APP_DATA_DIR: str = "antigravity"
VALID_LOG_LEVELS: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"}
)


class CapabilityPolicy(StrEnum):
    """Enforcement policy for bidirectional ``fs/*`` and ``terminal/*`` capabilities."""

    DISABLED = "disabled"
    RELAY = "relay"
    HOST = "host"


class RelayOnlyPolicy(StrEnum):
    """Enforcement policy for client-side capabilities without backend host execution."""

    DISABLED = "disabled"
    RELAY = "relay"


class LogFormat(StrEnum):
    """Output format for ACP server logs written to ``stderr`` and optional log files."""

    PRETTY = "pretty"
    JSON = "json"


def _is_loopback_literal(host: str) -> bool:
    """Return ``True`` when ``host`` is ``localhost`` or a loopback IP address."""
    cleaned = host.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if not cleaned:
        return False
    if cleaned.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError as exc:
        logger.debug("Invalid IP address literal %r: %s", cleaned, exc)
        return False


def _validate_agy_dir_fields(
    app_data_dir: str | None,
    gemini_dir: str | None,
) -> None:
    """Validate optional Antigravity profile directory settings at config build time."""
    if app_data_dir is not None:
        cleaned_app = app_data_dir.strip()
        if not cleaned_app:
            raise ValueError("agy_app_data_dir cannot be empty")
        app_path = Path(cleaned_app)
        if app_path.is_absolute() or ".." in app_path.parts:
            raise ValueError("agy_app_data_dir must be a safe relative directory name")
    if gemini_dir is not None:
        cleaned_gemini = gemini_dir.strip()
        if not cleaned_gemini:
            raise ValueError("agy_gemini_dir cannot be empty")
        gem_path = Path(cleaned_gemini)
        if ".." in gem_path.parts or not gem_path.expanduser().is_absolute():
            raise ValueError("agy_gemini_dir must be a safe absolute path")


class TransportConfig(FrozenModel):
    """WebSocket transport, origin/host anti-CSWSH, Bearer auth, and frame limits."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    ws_path: str = "/acp"
    allow_non_loopback: bool = False
    allow_unauthenticated_non_loopback: bool = False
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    allow_missing_origin: bool | None = None
    auth_token: SecretStr | None = None
    max_connections: int = Field(default=4, ge=1)
    max_frame_bytes: int = Field(default=1_048_576, ge=4096)
    ws_ping_interval_seconds: float | None = Field(default=30.0, gt=0.0)
    ws_ping_timeout_seconds: float | None = Field(default=10.0, gt=0.0)

    @property
    def effective_allow_missing_origin(self) -> bool:
        """Allow missing ``Origin`` on loopback by default, or reject on non-loopback."""
        if self.allow_missing_origin is not None:
            return self.allow_missing_origin
        return not self.allow_non_loopback

    @model_validator(mode="after")
    def _validate_transport_security(self) -> "TransportConfig":
        """Fail fast on unsafe bind hosts, missing auth tokens, or malformed paths."""
        if not self.host.strip():
            raise ValueError("transport.host cannot be empty")
        if not self.ws_path.startswith("/") or len(self.ws_path) < 2:
            raise ValueError("transport.ws_path must start with '/' and be non-empty")
        if not self.allow_non_loopback and not _is_loopback_literal(self.host):
            raise ValueError(
                f"Refusing non-loopback bind host '{self.host}' without allow_non_loopback=True"
            )
        token_val = self.auth_token.get_secret_value().strip() if self.auth_token else ""
        if (
            self.allow_non_loopback
            and not token_val
            and not self.allow_unauthenticated_non_loopback
        ):
            raise ValueError(
                "Non-loopback binding requires transport.auth_token "
                "(or explicit allow_unauthenticated_non_loopback=True)"
            )
        return self


class AgentConfig(FrozenModel):
    """ACP stdio subprocess command, sandbox, workspace, and lifecycle settings."""

    command: tuple[str, ...] = ("agy", "acp", "--stdio")
    agy_sandbox: bool = True
    agy_app_data_dir: str | None = DEFAULT_AGY_APP_DATA_DIR
    agy_gemini_dir: str | None = None
    workspace_root: Path | None = None
    initialize_timeout_seconds: float = Field(default=15.0, gt=0.0)
    terminate_wait_seconds: float = Field(default=2.0, gt=0.0)
    stderr_history_limit: int = Field(default=200, ge=1)
    extra_env_allowlist: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_agent_config(self) -> "AgentConfig":
        """Validate command vector and Antigravity directory flags."""
        if not self.command or not all(part.strip() for part in self.command):
            raise ValueError("agent.command must contain at least one non-empty argument")
        _validate_agy_dir_fields(self.agy_app_data_dir, self.agy_gemini_dir)
        return self


class CapabilityPolicyConfig(FrozenModel):
    """Fail-closed capability policies for FS, terminal, auth, elicitation, and extensions."""

    fs_read: CapabilityPolicy = CapabilityPolicy.DISABLED
    fs_write: CapabilityPolicy = CapabilityPolicy.DISABLED
    fs_max_read_bytes: int = Field(default=2_097_152, ge=1024)
    fs_max_write_bytes: int = Field(default=2_097_152, ge=1024)
    fs_allow_sensitive_writes: bool = False

    terminal: CapabilityPolicy = CapabilityPolicy.DISABLED
    terminal_max_per_session: int = Field(default=8, ge=1)
    terminal_default_output_byte_limit: int = Field(default=1_048_576, ge=1024)
    terminal_max_output_byte_limit: int = Field(default=4_194_304, ge=1024)
    terminal_terminate_wait_seconds: float = Field(default=2.0, gt=0.0)
    terminal_allow_dangerous_env: bool = False
    terminal_allowed_commands: tuple[str, ...] = ()
    terminal_allow_unrestricted_commands: bool = False

    auth_terminal: RelayOnlyPolicy = RelayOnlyPolicy.DISABLED

    allow_elicitation_form: bool = True
    allow_elicitation_url: bool = True
    elicitation_allow_http_url: bool = False

    allow_plan_operations: bool = True
    allow_session_notices: bool = True
    allow_session_compaction: bool = True
    allow_boolean_config_options: bool = True
    allow_subagents: bool = True

    allow_extension_methods: bool = True
    allowed_extension_prefixes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_capability_policy(self) -> "CapabilityPolicyConfig":
        """Ensure dangerous host terminal execution requires an allowlist or explicit opt-in."""
        if self.terminal_default_output_byte_limit > self.terminal_max_output_byte_limit:
            raise ValueError(
                "capabilities.terminal_default_output_byte_limit cannot exceed "
                "terminal_max_output_byte_limit"
            )
        if (
            self.terminal == CapabilityPolicy.HOST
            and not self.terminal_allowed_commands
            and not self.terminal_allow_unrestricted_commands
        ):
            raise ValueError(
                "Host terminal mode requires capabilities.terminal_allowed_commands "
                "or explicit capabilities.terminal_allow_unrestricted_commands=True"
            )
        return self


class McpPolicyConfig(FrozenModel):
    """Fail-closed policy governing client-supplied and server-curated MCP servers."""

    allow_client_stdio: bool = False
    allowed_stdio_commands: tuple[str, ...] = ()
    allow_http: bool = True
    allow_sse: bool = True
    allow_insecure_http_urls: bool = False
    enable_cbi_mcp: bool = True
    cbi_db_path: Path | None = None


class LimitsConfig(FrozenModel):
    """Per-connection resource and concurrency bounds."""

    max_pending_client_requests: int = Field(default=64, ge=1)
    max_pending_agent_requests: int = Field(default=64, ge=1)
    max_active_sessions_per_connection: int = Field(default=16, ge=1)
    max_subagents_per_session: int = Field(default=32, ge=1)


class LoggingConfig(FrozenModel):
    """Dual-mode production NDJSON and DEV pretty terminal logging configuration."""

    level: str = "INFO"
    format: LogFormat = LogFormat.PRETTY
    log_file: Path | None = None
    log_payloads: bool | None = None
    log_audit_events: bool = True

    @property
    def effective_log_payloads(self) -> bool:
        """Log indented redacted payloads by default in ``pretty`` DEV mode only."""
        if self.log_payloads is not None:
            return self.log_payloads
        return self.format == LogFormat.PRETTY

    @model_validator(mode="after")
    def _validate_logging_level(self) -> "LoggingConfig":
        """Normalize and validate log level name."""
        upper = self.level.strip().upper()
        if upper not in VALID_LOG_LEVELS:
            raise ValueError(f"Invalid logging.level {self.level!r}")
        if upper != self.level:
            object.__setattr__(self, "level", upper)
        return self


class AcpServerConfig(FrozenModel):
    """Root composed configuration for the pure ACP v1 WebSocket gateway."""

    transport: TransportConfig = Field(default_factory=TransportConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    capabilities: CapabilityPolicyConfig = Field(default_factory=CapabilityPolicyConfig)
    mcp: McpPolicyConfig = Field(default_factory=McpPolicyConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


ApiServerConfig = AcpServerConfig

__all__ = [
    "AcpServerConfig",
    "AgentConfig",
    "ApiServerConfig",
    "CapabilityPolicy",
    "CapabilityPolicyConfig",
    "DEFAULT_AGY_APP_DATA_DIR",
    "DEFAULT_ALLOWED_ORIGINS",
    "LimitsConfig",
    "LogFormat",
    "LoggingConfig",
    "McpPolicyConfig",
    "RelayOnlyPolicy",
    "TransportConfig",
    "VALID_LOG_LEVELS",
]
