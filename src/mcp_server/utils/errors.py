"""Structured MCP success/error envelope builders and domain exception translation."""

import sys
import traceback
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from core.models.exceptions import (
    DatabaseError,
    DatabaseNotFoundError,
    IndexerError,
    LspError,
    ParseError,
    PathSecurityError,
    SymbolNotFoundError,
    ValidationError,
)
from core.utils.logging import get_logger, is_logging_configured
from mcp_server.models.errors import McpErrorCode, McpErrorDetail

logger = get_logger("mcp_server.utils.errors")

_INDEXER_ERROR_RULES: tuple[
    tuple[type[IndexerError], str | None, str | None, bool],
    ...,
] = (
    (ValidationError, McpErrorCode.INVALID_INPUT, None, True),
    (
        DatabaseNotFoundError,
        McpErrorCode.DATABASE_NOT_FOUND,
        "Indexed database not found; run `rc index` first.",
        False,
    ),
    (DatabaseError, None, None, True),
    (SymbolNotFoundError, None, None, True),
    (
        PathSecurityError,
        McpErrorCode.PATH_SECURITY,
        "Access denied: file path is missing, symlinked, or outside repository root.",
        False,
    ),
    (LspError, McpErrorCode.LSP_FAILURE, None, False),
    (ParseError, McpErrorCode.PARSE_ERROR, None, False),
)


def success_result(data: dict[str, Any]) -> dict[str, Any]:
    """Build a structured MCP success envelope.

    Args:
        data: Tool response payload dictionary.

    Returns:
        Dictionary carrying ``ok=True``, ``data``, and ``error=None``.
    """
    return {
        "ok": True,
        "data": data,
        "error": None,
    }


def error_result(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured MCP error envelope.

    Args:
        code: Machine-readable MCP error code string.
        message: Safe human-readable error message.
        details: Optional structured disambiguation or diagnostic metadata.

    Returns:
        Dictionary carrying ``ok=False``, ``error`` detail dict, and ``data=None``.
    """
    detail = McpErrorDetail(code=code, message=message, details=details)
    return {
        "ok": False,
        "error": detail.model_dump(),
        "data": None,
    }


def _log_internal_exception(operation: str, exc: Exception) -> None:
    """Log an unexpected exception strictly to ``stderr`` without polluting ``stdout``.

    Args:
        operation: MCP tool name where the unhandled error occurred.
        exc: Unhandled exception instance.
    """
    if is_logging_configured():
        logger.exception("Unhandled error in MCP tool %s: %s", operation, type(exc).__name__)
        return
    sys.stderr.write(f"Unhandled error in MCP tool {operation}:\n")
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def _map_indexer_error(exc: IndexerError) -> dict[str, Any]:
    """Translate a typed ``IndexerError`` into a safe MCP error envelope.

    Args:
        exc: Domain ``IndexerError`` instance.

    Returns:
        Structured MCP error envelope dictionary.
    """
    for cls, fixed_code, fixed_msg, include_details in _INDEXER_ERROR_RULES:
        if isinstance(exc, cls):
            code = fixed_code or exc.error_code or McpErrorCode.DATABASE_ERROR
            msg = fixed_msg if fixed_msg is not None else exc.message
            details = exc.context if include_details else None
            return error_result(code, msg, details=details)
    return error_result(exc.error_code, exc.message, details=exc.context)


def map_exception_to_error_result(
    exc: Exception,
    *,
    operation: str,
    log_unexpected: bool = True,
) -> dict[str, Any]:
    """Map Pydantic, domain, or unexpected exceptions into MCP error envelopes.

    Args:
        exc: Exception caught at the MCP tool boundary.
        operation: Name of the MCP tool operation being executed.
        log_unexpected: Whether to log unexpected non-domain exceptions via
            ``_log_internal_exception`` (disable when caller already called
            ``logger.exception``).

    Returns:
        Structured MCP error envelope dictionary safe for wire serialization.
    """
    if isinstance(exc, PydanticValidationError):
        return error_result(McpErrorCode.INVALID_INPUT, str(exc))
    if isinstance(exc, IndexerError):
        return _map_indexer_error(exc)

    if log_unexpected:
        _log_internal_exception(operation, exc)
    return error_result(
        McpErrorCode.INTERNAL_ERROR,
        f"Internal error while executing {operation}.",
    )


__all__ = [
    "error_result",
    "map_exception_to_error_result",
    "success_result",
]
