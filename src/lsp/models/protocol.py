"""Pydantic v2 models for LSP client configuration, JSON-RPC responses, and locations."""

from typing import Any, Self

from pydantic import Field

from core.models.base import FrozenModel
from core.models.config import AppConfig
from core.models.range import Range


class LspClientOptions(FrozenModel):
    """Immutable configuration options for a ``PyrightLspClient`` session."""

    server_cmd: tuple[str, ...] = ("pyright-langserver", "--stdio")
    request_timeout_seconds: float = Field(default=15.0, gt=0.0)
    shutdown_timeout_seconds: float = Field(default=2.0, gt=0.0)
    terminate_wait_seconds: float = Field(default=2.0, gt=0.0)
    max_payload_bytes: int = Field(default=16 * 1024 * 1024, gt=0)
    max_header_bytes: int = Field(default=8192, gt=0)
    stderr_history_limit: int = Field(default=200, gt=0)

    @classmethod
    def from_config(cls, config: AppConfig, **overrides: Any) -> Self:
        """Build ``LspClientOptions`` from an ``AppConfig`` instance with optional overrides.

        Args:
            config: Application configuration supplying default timeout and payload bounds.
            **overrides: Explicit keyword overrides taking precedence over ``config``.

        Returns:
            Validated immutable ``LspClientOptions`` instance.

        Raises:
            pydantic.ValidationError: If any field override violates schema constraints.
        """
        params: dict[str, Any] = {
            "request_timeout_seconds": config.lsp_timeout_seconds,
            "max_payload_bytes": config.max_lsp_payload_bytes,
        }
        params.update(overrides)
        return cls(**params)


class JsonRpcResponse(FrozenModel):
    """Typed JSON-RPC 2.0 response envelope returned for a client-initiated request."""

    id: int | str | None = None
    result: Any = None
    error: dict[str, Any] | None = None


class NormalizedLocation(FrozenModel):
    """Normalized LSP ``Location`` with canonical ``file://`` URI and UTF-16 ``Range``."""

    uri: str
    range: Range
