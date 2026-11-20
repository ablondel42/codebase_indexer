"""MCP wire error code enumeration and structured error detail model."""

from enum import StrEnum
from typing import Any

from core.models.base import FrozenModel


class McpErrorCode(StrEnum):
    """Machine-readable error codes returned in MCP tool error envelopes."""

    INVALID_INPUT = "invalid_input"
    DATABASE_NOT_FOUND = "database_not_found"
    DATABASE_ERROR = "database_error"
    REPO_NOT_FOUND = "repo_not_found"
    AMBIGUOUS_REPO = "ambiguous_repo"
    FILE_NOT_FOUND = "file_not_found"
    SYMBOL_NOT_FOUND = "symbol_not_found"
    AMBIGUOUS_SYMBOL = "ambiguous_symbol"
    REFERENCES_UNAVAILABLE = "references_unavailable"
    LSP_FAILURE = "lsp_failure"
    PATH_SECURITY = "path_security_error"
    PARSE_ERROR = "parse_error"
    INTERNAL_ERROR = "internal_error"


class McpErrorDetail(FrozenModel):
    """Structured error detail object embedded inside MCP error responses."""

    code: str
    message: str
    details: dict[str, Any] | None = None


__all__ = [
    "McpErrorCode",
    "McpErrorDetail",
]
