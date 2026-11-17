"""Custom exception hierarchy for repo_context."""

from typing import Any


class RepoContextError(Exception):
    """Base exception for all repo-context errors.

    Attributes:
        message: Human-readable error message.
        error_code: Machine-readable error code for structured handling.
        context: Optional dictionary with additional debugging metadata.
    """

    error_code: str = "repo_context_error"

    def __init__(
        self,
        message: str,
        context: dict[str, Any] | None = None,
        *,
        error_code: str | None = None,
    ) -> None:
        """Initialize the exception."""
        super().__init__(message)
        self.message = message
        self.error_code: str = error_code if error_code is not None else self.__class__.error_code
        self.context: dict[str, Any] | None = context

    def __str__(self) -> str:
        """Return the human-readable error message."""
        return self.message

    def __repr__(self) -> str:
        """Return a developer-friendly representation."""
        return (
            f"{self.__class__.__name__}({self.message!r}, "
            f"error_code={self.error_code!r}, context={self.context!r})"
        )


class ValidationError(RepoContextError):
    """Raised when input or model validation fails."""

    error_code: str = "validation_error"


class DatabaseError(RepoContextError):
    """Raised when a database operation or schema check fails."""

    error_code: str = "database_error"


class DatabaseNotFoundError(DatabaseError):
    """Raised when an expected SQLite database file does not exist."""

    error_code: str = "database_not_found"


class SymbolNotFoundError(RepoContextError):
    """Raised when a requested symbol cannot be found in the graph."""

    error_code: str = "symbol_not_found"


class PathSecurityError(RepoContextError):
    """Raised when a path escapes the repository root or violates symlink rules."""

    error_code: str = "path_security_error"


class LspError(RepoContextError):
    """Raised when an LSP subprocess or JSON-RPC operation fails."""

    error_code: str = "lsp_error"


class ParseError(RepoContextError):
    """Raised when source parsing or AST extraction fails."""

    error_code: str = "parse_error"


__all__ = [
    "RepoContextError",
    "ValidationError",
    "DatabaseError",
    "DatabaseNotFoundError",
    "SymbolNotFoundError",
    "PathSecurityError",
    "LspError",
    "ParseError",
]
