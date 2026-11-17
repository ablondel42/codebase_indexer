"""Tests for the canonical ``repo_context.exceptions`` hierarchy."""

import pytest

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


@pytest.mark.parametrize(
    ("exc_cls", "expected_code"),
    [
        (RepoContextError, "repo_context_error"),
        (ValidationError, "validation_error"),
        (DatabaseError, "database_error"),
        (DatabaseNotFoundError, "database_not_found"),
        (SymbolNotFoundError, "symbol_not_found"),
        (PathSecurityError, "path_security_error"),
        (LspError, "lsp_error"),
        (ParseError, "parse_error"),
    ],
)
def test_exception_defaults_and_inheritance(
    exc_cls: type[RepoContextError],
    expected_code: str,
) -> None:
    """Every domain exception inherits from RepoContextError and defines error_code."""
    err = exc_cls("something failed", {"key": "value"})
    assert isinstance(err, RepoContextError)
    assert isinstance(err, Exception)
    assert err.message == "something failed"
    assert str(err) == "something failed"
    assert err.error_code == expected_code
    assert err.context == {"key": "value"}
    assert expected_code in repr(err)


def test_database_not_found_is_database_error() -> None:
    """DatabaseNotFoundError subclasses DatabaseError."""
    err = DatabaseNotFoundError("missing db")
    assert isinstance(err, DatabaseError)
    assert isinstance(err, RepoContextError)


def test_custom_error_code_override() -> None:
    """Explicit error_code keyword overrides the class default."""
    err = RepoContextError(
        "ambiguous",
        {"candidates": ["a", "b"]},
        error_code="ambiguous_symbol",
    )
    assert err.error_code == "ambiguous_symbol"
    assert err.context == {"candidates": ["a", "b"]}
    assert "ambiguous_symbol" in repr(err)
