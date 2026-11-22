"""First-class production NDJSON & DEV terminal logging, correlation context, and audit trail."""

import logging
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

from api.models.config import LogFormat, LoggingConfig
from api.utils.formatting import (
    API_CONSOLE_DATEFMT,
    API_CONSOLE_FORMAT,
    format_ndjson_log_line,
)
from core.utils.logging import ROOT_LOGGER_NAME, get_logger

_CTX_CONNECTION_ID: ContextVar[str | None] = ContextVar("acp_connection_id", default=None)
_CTX_SESSION_ID: ContextVar[str | None] = ContextVar("acp_session_id", default=None)
_CTX_REQUEST_ID: ContextVar[int | str | None] = ContextVar("acp_request_id", default=None)
_CTX_TRACEPARENT: ContextVar[str | None] = ContextVar("acp_traceparent", default=None)
_CTX_METHOD: ContextVar[str | None] = ContextVar("acp_method", default=None)
_CTX_DIRECTION: ContextVar[str | None] = ContextVar("acp_direction", default=None)

_STRUCTURED_EXTRA_KEYS: tuple[str, ...] = (
    "event",
    "duration_ms",
    "jsonrpc_code",
    "audit",
    "outcome",
    "details",
)


class AcpLogContext:
    """Async-safe ``contextvars`` correlation scope attached to every ACP log record."""

    @staticmethod
    def snapshot() -> dict[str, Any]:
        """Return active non-null correlation fields from the current async context."""
        fields: dict[str, Any] = {}
        pairs = (
            ("connection_id", _CTX_CONNECTION_ID.get()),
            ("session_id", _CTX_SESSION_ID.get()),
            ("request_id", _CTX_REQUEST_ID.get()),
            ("traceparent", _CTX_TRACEPARENT.get()),
            ("method", _CTX_METHOD.get()),
            ("direction", _CTX_DIRECTION.get()),
        )
        for key, value in pairs:
            if value is not None:
                fields[key] = value
        return fields

    @staticmethod
    @contextmanager
    def bind(
        *,
        connection_id: str | None = None,
        session_id: str | None = None,
        request_id: int | str | None = None,
        traceparent: str | None = None,
        method: str | None = None,
    ) -> Generator[None, None, None]:
        """Bind correlation fields for the duration of the ``with`` block."""
        tokens: list[tuple[ContextVar[Any], Token[Any]]] = []
        bindings: list[tuple[ContextVar[Any], Any]] = [
            (_CTX_CONNECTION_ID, connection_id),
            (_CTX_SESSION_ID, session_id),
            (_CTX_REQUEST_ID, request_id),
            (_CTX_TRACEPARENT, traceparent),
            (_CTX_METHOD, method),
        ]
        for var, val in bindings:
            if val is not None:
                tokens.append((var, var.set(val)))
        try:
            yield
        finally:
            for var, tok in reversed(tokens):
                var.reset(tok)

    @staticmethod
    @contextmanager
    def bind_direction(direction: str) -> Generator[None, None, None]:
        """Bind frame traffic direction (e.g., ``ui_to_bridge``) for the current scope."""
        token = _CTX_DIRECTION.set(direction)
        try:
            yield
        finally:
            _CTX_DIRECTION.reset(token)


class AcpJsonLogFormatter(logging.Formatter):
    """Production single-line NDJSON formatter with correlation fields and secret redaction."""

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as a redacted, single-line UTC JSON object."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(AcpLogContext.snapshot())
        for key in _STRUCTURED_EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return format_ndjson_log_line(payload)


class AcpPrettyLogFormatter(logging.Formatter):
    """Human-readable DEV terminal formatter preserving indented JSON & correlation tags."""

    def __init__(self) -> None:
        """Initialize base console formatter with ``API_CONSOLE_FORMAT``."""
        super().__init__(API_CONSOLE_FORMAT, datefmt=API_CONSOLE_DATEFMT)

    def format(self, record: logging.LogRecord) -> str:
        """Format ``record`` with compact context tags when correlation IDs are bound."""
        base = super().format(record)
        ctx = AcpLogContext.snapshot()
        tags: list[str] = []
        conn = ctx.get("connection_id")
        if isinstance(conn, str) and conn and f"[{conn}]" not in base:
            tags.append(f"conn={conn}")
        sess = ctx.get("session_id")
        if isinstance(sess, str) and sess:
            tags.append(f"session={sess}")
        dur = getattr(record, "duration_ms", None)
        if isinstance(dur, (int, float)):
            tags.append(f"duration_ms={dur:.2f}")
        return f"{base} ({', '.join(tags)})" if tags else base


class AcpAuditLogger:
    """Dedicated ``indexer.api.audit`` security & policy decision logger."""

    def __init__(self, *, enabled: bool = True) -> None:
        """Initialize audit logger gated by ``LoggingConfig.log_audit_events``."""
        self._enabled = enabled
        self._logger = get_logger("api.audit")

    def record(
        self,
        event: str,
        *,
        outcome: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Emit one structured security audit event at ``INFO`` or ``WARNING``."""
        if not self._enabled:
            return
        level = logging.WARNING if outcome in ("denied", "blocked", "rejected") else logging.INFO
        self._logger.log(
            level,
            "ACP audit event=%s outcome=%s",
            event,
            outcome,
            extra={
                "audit": True,
                "event": event,
                "outcome": outcome,
                "details": dict(details) if details else {},
            },
        )


class AcpLoggingConfigurator:
    """Configures ``indexer`` root logger for DEV pretty console or PROD NDJSON output."""

    def __init__(self, config: LoggingConfig) -> None:
        """Initialize logging configurator from immutable ``LoggingConfig``."""
        self._config = config

    def _build_formatter(self) -> logging.Formatter:
        """Select ``AcpJsonLogFormatter`` or ``AcpPrettyLogFormatter`` from config."""
        if self._config.format == LogFormat.JSON:
            return AcpJsonLogFormatter()
        return AcpPrettyLogFormatter()

    def configure(self) -> None:
        """Attach formatted ``stderr`` (and optional file) handlers to ``indexer`` logger."""
        root_logger = logging.getLogger(ROOT_LOGGER_NAME)
        for handler in list(root_logger.handlers):
            if isinstance(handler, logging.NullHandler):
                root_logger.removeHandler(handler)

        formatter = self._build_formatter()
        has_stream = False
        for handler in root_logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setFormatter(formatter)
                has_stream = True

        if not has_stream:
            console = logging.StreamHandler(sys.stderr)
            console.setLevel(logging.DEBUG)
            console.setFormatter(formatter)
            root_logger.addHandler(console)

        if self._config.log_file is not None:
            file_handler = logging.FileHandler(self._config.log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(AcpJsonLogFormatter())
            root_logger.addHandler(file_handler)

        root_logger.setLevel(getattr(logging, self._config.level, logging.INFO))
        root_logger.propagate = False


__all__ = [
    "AcpAuditLogger",
    "AcpJsonLogFormatter",
    "AcpLogContext",
    "AcpLoggingConfigurator",
    "AcpPrettyLogFormatter",
]
