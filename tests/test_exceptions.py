"""Tests for the canonical ``core.models.exceptions`` hierarchy."""

import pytest

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


@pytest.mark.parametrize(
    ("exc_cls", "expected_code"),
    [
        (IndexerError, "indexer_error"),
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
    exc_cls: type[IndexerError],
    expected_code: str,
) -> None:
    """Every domain exception inherits from IndexerError and defines error_code."""
    err = exc_cls("something failed", {"key": "value"})
    assert isinstance(err, IndexerError)
    assert isinstance(err, Exception)
    assert err.message == "something failed"
    assert str(err) == "something failed"
    assert err.error_code == expected_code
    assert err.context == {"key": "value"}
    assert expected_code in repr(err)


def test_database_not_found_is_database_error() -> None:
    """DatabaseNotFoundError subclasses DatabaseError and IndexerError."""
    err = DatabaseNotFoundError("missing db")
    assert isinstance(err, DatabaseError)
    assert isinstance(err, IndexerError)


def test_custom_error_code_override() -> None:
    """Explicit error_code keyword overrides the class default."""
    err = IndexerError(
        "ambiguous",
        {"candidates": ["a", "b"]},
        error_code="ambiguous_symbol",
    )
    assert err.error_code == "ambiguous_symbol"
    assert err.context == {"candidates": ["a", "b"]}
    assert "ambiguous_symbol" in repr(err)
