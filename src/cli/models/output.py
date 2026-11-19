"""Structured CLI error and ``rc mcp-call`` payload envelope models."""

from typing import Any

from core.models.base import FrozenModel


class CliErrorDetail(FrozenModel):
    """Structured error detail object emitted inside CLI ``stderr`` JSON errors."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class CliErrorEnvelope(FrozenModel):
    """Top-level JSON error envelope written to ``sys.stderr`` by CLI commands."""

    ok: bool = False
    error: CliErrorDetail


class McpCallEnvelope(FrozenModel):
    """Byte/token telemetry and decoded result envelope emitted by ``rc mcp-call``."""

    tool: str
    bytes: int
    estimated_tokens: int
    result: Any


__all__ = [
    "CliErrorDetail",
    "CliErrorEnvelope",
    "McpCallEnvelope",
]
