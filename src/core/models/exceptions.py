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


__all__ = [
    "DatabaseError",
    "DatabaseNotFoundError",
    "IndexerError",
    "LspError",
    "ParseError",
    "PathSecurityError",
    "SymbolNotFoundError",
    "ValidationError",
]
