"""Canonical domain exception hierarchy for the codebase indexer."""

from typing import Any


class IndexerError(Exception):
    """Base exception for all structured indexer errors.

    Attributes:
        message: Human-readable error message.
        error_code: Machine-readable error code for structured handling.
        context: Optional dictionary with structured debugging metadata.
    """

    error_code: str = "indexer_error"

    def __init__(
        self,
        message: str,
        context: dict[str, Any] | None = None,
        *,
        error_code: str | None = None,
    ) -> None:
        """Initialize an ``IndexerError``.

        Args:
            message: Human-readable description of the failure.
            context: Optional structured metadata describing failure context.
            error_code: Optional explicit override for ``self.__class__.error_code``.
        """
        super().__init__(message)
        self.message = message
        self.error_code: str = error_code if error_code is not None else self.__class__.error_code
        self.context: dict[str, Any] | None = context

    def __str__(self) -> str:
        """Return the human-readable error message.

        Returns:
            The exception message string.
        """
        return self.message

    def __repr__(self) -> str:
        """Return a developer-friendly representation including code and context.

        Returns:
            Formatted representation string.
        """
        return (
            f"{self.__class__.__name__}({self.message!r}, "
            f"error_code={self.error_code!r}, context={self.context!r})"
        )


class ValidationError(IndexerError):
    """Raised when input arguments, JSON payloads, or model constraints fail validation."""

    error_code: str = "validation_error"


class DatabaseError(IndexerError):
    """Raised when a SQLite database query, migration, or schema check fails."""

    error_code: str = "database_error"


class DatabaseNotFoundError(DatabaseError):
    """Raised when an expected SQLite database file does not exist on disk."""

    error_code: str = "database_not_found"


class SymbolNotFoundError(IndexerError):
    """Raised when a requested symbol cannot be uniquely resolved in the graph."""

    error_code: str = "symbol_not_found"


class PathSecurityError(IndexerError):
    """Raised when a path escapes the repository root or violates symlink rules."""

    error_code: str = "path_security_error"


class LspError(IndexerError):
    """Raised when a language server subprocess or JSON-RPC operation fails."""

    error_code: str = "lsp_error"


class ParseError(IndexerError):
    """Raised when source reading, decoding, size checking, or AST parsing fails."""

    error_code: str = "parse_error"


class AcpError(IndexerError):
    """Base exception for all ACP protocol, transport, config, and security failures."""

    error_code: str = "acp_error"
    jsonrpc_code: int = -32603
    wire_message: str = "Internal error"
    expose_message: bool = False
    audit_event: str | None = None

    def __init__(
        self,
        message: str,
        context: dict[str, Any] | None = None,
        *,
        error_code: str | None = None,
        jsonrpc_code: int | None = None,
        wire_data: dict[str, Any] | None = None,
    ) -> None:
        """Initialize an ``AcpError`` with optional JSON-RPC code and wire data."""
        super().__init__(message, context=context, error_code=error_code)
        self.jsonrpc_code: int = (
            jsonrpc_code if jsonrpc_code is not None else self.__class__.jsonrpc_code
        )
        self.wire_data: dict[str, Any] | None = wire_data


class AcpParseError(AcpError):
    """Raised when an inbound JSON-RPC frame cannot be parsed as valid JSON (``-32700``)."""

    error_code: str = "acp_parse_error"
    jsonrpc_code: int = -32700
    wire_message: str = "Parse error"
    expose_message: bool = True


class AcpInvalidRequestError(AcpError):
    """Raised when a JSON-RPC envelope or lifecycle state is invalid (``-32600``)."""

    error_code: str = "acp_invalid_request"
    jsonrpc_code: int = -32600
    wire_message: str = "Invalid request"
    expose_message: bool = True


class AcpMethodNotFoundError(AcpError):
    """Raised when an unimplemented or unadvertised JSON-RPC method is called (``-32601``)."""

    error_code: str = "acp_method_not_found"
    jsonrpc_code: int = -32601
    wire_message: str = "Method not found"
    expose_message: bool = True


class AcpInvalidParamsError(AcpError):
    """Raised when method parameters fail schema or capability validation (``-32602``)."""

    error_code: str = "acp_invalid_params"
    jsonrpc_code: int = -32602
    wire_message: str = "Invalid params"
    expose_message: bool = True


class AcpInternalError(AcpError):
    """Raised on unexpected internal ACP gateway failures (``-32603``)."""

    error_code: str = "acp_internal_error"
    jsonrpc_code: int = -32603
    wire_message: str = "Internal error"
    expose_message: bool = False


class AcpRequestCancelledError(AcpError):
    """Raised when an in-flight ACP request is cancelled via ``$/cancel_request`` (``-32800``)."""

    error_code: str = "acp_request_cancelled"
    jsonrpc_code: int = -32800
    wire_message: str = "Request cancelled"
    expose_message: bool = True


class AcpAuthRequiredError(AcpError):
    """Raised when an operation requires ACP authentication first (``-32000``)."""

    error_code: str = "acp_auth_required"
    jsonrpc_code: int = -32000
    wire_message: str = "Authentication required"
    expose_message: bool = True


class AcpResourceNotFoundError(AcpError):
    """Raised when a referenced session, file, or terminal does not exist (``-32002``)."""

    error_code: str = "acp_resource_not_found"
    jsonrpc_code: int = -32002
    wire_message: str = "Resource not found"
    expose_message: bool = True


class AcpSecurityError(AcpError):
    """Raised when a request violates root-set, MCP, origin, auth, or policy boundaries."""

    error_code: str = "acp_security_error"
    jsonrpc_code: int = -32602
    wire_message: str = "Invalid params"
    expose_message: bool = True
    audit_event: str | None = "security_violation"


class AcpConfigError(AcpError):
    """Raised when ACP server TOML/env/CLI configuration fails startup validation."""

    error_code: str = "acp_config_error"
    jsonrpc_code: int = -32603
    wire_message: str = "Configuration error"
    expose_message: bool = False


class AcpTransportError(AcpError):
    """Raised when WebSocket or stdio subprocess transport I/O fails."""

    error_code: str = "acp_transport_error"
    jsonrpc_code: int = -32603
    wire_message: str = "Transport error"
    expose_message: bool = False


__all__ = [
    "AcpAuthRequiredError",
    "AcpConfigError",
    "AcpError",
    "AcpInternalError",
    "AcpInvalidParamsError",
    "AcpInvalidRequestError",
    "AcpMethodNotFoundError",
    "AcpParseError",
    "AcpRequestCancelledError",
    "AcpResourceNotFoundError",
    "AcpSecurityError",
    "AcpTransportError",
    "DatabaseError",
    "DatabaseNotFoundError",
    "IndexerError",
    "LspError",
    "ParseError",
    "PathSecurityError",
    "SymbolNotFoundError",
    "ValidationError",
]
