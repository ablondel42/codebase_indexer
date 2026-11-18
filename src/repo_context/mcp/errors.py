"""Structured error and success payload helpers for MCP tools."""

import sys
import traceback
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from repo_context.exceptions import (
    DatabaseError,
    DatabaseNotFoundError,
    LspError,
    ParseError,
    PathSecurityError,
    RepoContextError,
    SymbolNotFoundError,
    ValidationError,
)
from repo_context.logging_config import get_logger

logger = get_logger("mcp.errors")

# Error codes used by MCP tools
ERROR_INVALID_INPUT = "invalid_input"
ERROR_DATABASE_NOT_FOUND = "database_not_found"
ERROR_DATABASE_ERROR = "database_error"
ERROR_REPO_NOT_FOUND = "repo_not_found"
ERROR_AMBIGUOUS_REPO = "ambiguous_repo"
ERROR_FILE_NOT_FOUND = "file_not_found"
ERROR_SYMBOL_NOT_FOUND = "symbol_not_found"
ERROR_AMBIGUOUS_SYMBOL = "ambiguous_symbol"
ERROR_REFERENCES_UNAVAILABLE = "references_unavailable"
ERROR_LSP_FAILURE = "lsp_failure"
ERROR_STALE_CONTEXT = "stale_context"
ERROR_PATH_SECURITY = "path_security_error"
ERROR_PARSE_ERROR = "parse_error"
ERROR_INTERNAL_ERROR = "internal_error"


def error_result(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured MCP error envelope."""
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "details": details,
        },
        "data": None,
    }


def success_result(data: dict[str, Any]) -> dict[str, Any]:
    """Build a structured MCP success envelope."""
    return {
        "ok": True,
        "data": data,
        "error": None,
    }


def _log_internal_exception(operation: str, exc: Exception) -> None:
    """Log full traceback strictly to stderr without leaking to stdout or tool output."""
    if logger.hasHandlers():
        logger.exception("Unhandled error in MCP tool %s: %s", operation, type(exc).__name__)
    else:
        print(f"Unhandled error in MCP tool {operation}:", file=sys.stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


_REPO_ERROR_RULES: tuple[
    tuple[type[RepoContextError], str | None, str | None, bool],
    ...,
] = (
    (ValidationError, ERROR_INVALID_INPUT, None, True),
    (
        DatabaseNotFoundError,
        ERROR_DATABASE_NOT_FOUND,
        "Indexed database not found; run `rc index` first.",
        False,
    ),
    (DatabaseError, None, None, True),
    (SymbolNotFoundError, ERROR_SYMBOL_NOT_FOUND, None, True),
    (
        PathSecurityError,
        ERROR_PATH_SECURITY,
        "Access denied: file path is missing, symlinked, or outside repository root.",
        False,
    ),
    (LspError, ERROR_LSP_FAILURE, None, False),
    (ParseError, ERROR_PARSE_ERROR, None, False),
)


def _map_repo_context_error(exc: RepoContextError) -> dict[str, Any]:
    """Convert a ``RepoContextError`` subclass into a structured MCP error envelope."""
    for cls, fixed_code, fixed_msg, include_details in _REPO_ERROR_RULES:
        if isinstance(exc, cls):
            code = fixed_code or exc.error_code or ERROR_DATABASE_ERROR
            msg = fixed_msg if fixed_msg is not None else exc.message
            details = exc.context if include_details else None
            return error_result(code, msg, details=details)
    return error_result(exc.error_code, exc.message, details=exc.context)


def map_exception_to_error_result(exc: Exception, *, operation: str) -> dict[str, Any]:
    """Map domain and unexpected exceptions to safe structured MCP error responses."""
    if isinstance(exc, PydanticValidationError):
        return error_result(ERROR_INVALID_INPUT, str(exc))
    if isinstance(exc, RepoContextError):
        return _map_repo_context_error(exc)

    _log_internal_exception(operation, exc)
    return error_result(
        ERROR_INTERNAL_ERROR,
        f"Internal error while executing {operation}.",
    )
