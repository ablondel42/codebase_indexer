"""Strict Pydantic v2 input schemas for all 8 token-lean MCP tools."""

from pathlib import Path
from typing import Any

from pydantic import AliasChoices, ConfigDict, Field, field_validator

from core.models.base import FrozenModel
from core.models.enums import SymbolKind
from graph.models.references import ReferenceDirection


def _require_non_empty(value: str, field_name: str) -> str:
    """Validate that a string input is not empty or whitespace-only.

    Args:
        value: Raw string input value.
        field_name: Schema field label for validation error messages.

    Returns:
        Trimmed non-empty string.

    Raises:
        ValueError: If ``value`` is empty or contains only whitespace.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be empty or whitespace")
    return stripped


def _validate_optional_non_empty(value: str | None, field_name: str) -> str | None:
    """Validate optional string inputs, rejecting whitespace-only strings.

    Args:
        value: Optional string value or ``None``.
        field_name: Schema field label for validation error messages.

    Returns:
        Trimmed non-empty string, or ``None`` when ``value`` is ``None``.

    Raises:
        ValueError: If ``value`` is a non-``None`` empty or whitespace-only string.
    """
    if value is None:
        return None
    return _require_non_empty(value, field_name)


def _coerce_db_path_input(value: Any) -> Any:
    """Coerce ``Path`` instances to strings prior to ``db_path`` validation.

    Args:
        value: Raw ``db_path`` value supplied by caller.

    Returns:
        String path if ``value`` is a ``Path``, otherwise ``value`` unchanged.
    """
    if isinstance(value, Path):
        return str(value)
    return value


class GetRepoOverviewInput(FrozenModel):
    """Validated input parameters for the ``get_repo_overview`` MCP tool."""

    repo_id: str | None = None
    db_path: str | None = None

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_db_path(cls, value: Any) -> Any:
        """Convert ``Path`` objects to ``str`` before string validation."""
        return _coerce_db_path_input(value)

    @field_validator("repo_id")
    @classmethod
    def _validate_repo_id(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``repo_id`` values."""
        return _validate_optional_non_empty(value, "repo_id")

    @field_validator("db_path")
    @classmethod
    def _validate_db_path(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``db_path`` values."""
        return _validate_optional_non_empty(value, "db_path")


class ListFilesInput(FrozenModel):
    """Validated input parameters for the ``list_files`` MCP tool."""

    repo_id: str | None = None
    path_prefix: str | None = None
    include_tests: bool = False
    limit: int = Field(default=100, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    db_path: str | None = None

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_db_path(cls, value: Any) -> Any:
        """Convert ``Path`` objects to ``str`` before string validation."""
        return _coerce_db_path_input(value)

    @field_validator("repo_id")
    @classmethod
    def _validate_repo_id(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``repo_id`` values."""
        return _validate_optional_non_empty(value, "repo_id")

    @field_validator("path_prefix")
    @classmethod
    def _validate_path_prefix(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``path_prefix`` values."""
        return _validate_optional_non_empty(value, "path_prefix")

    @field_validator("db_path")
    @classmethod
    def _validate_db_path(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``db_path`` values."""
        return _validate_optional_non_empty(value, "db_path")


class GetFileOutlineInput(FrozenModel):
    """Validated input parameters for the ``get_file_outline`` MCP tool."""

    file_path: str = Field(..., min_length=1)
    repo_id: str | None = None
    include_private: bool = True
    max_depth: int = Field(default=3, ge=1, le=10)
    db_path: str | None = None

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_db_path(cls, value: Any) -> Any:
        """Convert ``Path`` objects to ``str`` before string validation."""
        return _coerce_db_path_input(value)

    @field_validator("file_path")
    @classmethod
    def _validate_file_path(cls, value: str) -> str:
        """Reject whitespace-only ``file_path`` values."""
        return _require_non_empty(value, "file_path")

    @field_validator("repo_id")
    @classmethod
    def _validate_repo_id(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``repo_id`` values."""
        return _validate_optional_non_empty(value, "repo_id")

    @field_validator("db_path")
    @classmethod
    def _validate_db_path(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``db_path`` values."""
        return _validate_optional_non_empty(value, "db_path")


class SearchSymbolsInput(FrozenModel):
    """Validated input parameters for the ``search_symbols`` MCP tool."""

    query: str = Field(..., min_length=1)
    repo_id: str | None = None
    kind: SymbolKind | None = None
    file_path: str | None = None
    limit: int = Field(default=20, ge=1, le=200)
    db_path: str | None = None

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_db_path(cls, value: Any) -> Any:
        """Convert ``Path`` objects to ``str`` before string validation."""
        return _coerce_db_path_input(value)

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        """Reject whitespace-only ``query`` values."""
        return _require_non_empty(value, "query")

    @field_validator("repo_id")
    @classmethod
    def _validate_repo_id(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``repo_id`` values."""
        return _validate_optional_non_empty(value, "repo_id")

    @field_validator("file_path")
    @classmethod
    def _validate_file_path(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``file_path`` values."""
        return _validate_optional_non_empty(value, "file_path")

    @field_validator("db_path")
    @classmethod
    def _validate_db_path(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``db_path`` values."""
        return _validate_optional_non_empty(value, "db_path")


class GetSymbolSourceInput(FrozenModel):
    """Validated input parameters for the ``get_symbol_source`` MCP tool."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    symbol_id_or_name: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("symbol", "symbol_id_or_name"),
    )
    repo_id: str | None = None
    max_lines: int = Field(default=120, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    db_path: str | None = None

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_db_path(cls, value: Any) -> Any:
        """Convert ``Path`` objects to ``str`` before string validation."""
        return _coerce_db_path_input(value)

    @field_validator("symbol_id_or_name")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        """Reject whitespace-only symbol selectors."""
        return _require_non_empty(value, "symbol")

    @field_validator("repo_id")
    @classmethod
    def _validate_repo_id(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``repo_id`` values."""
        return _validate_optional_non_empty(value, "repo_id")

    @field_validator("db_path")
    @classmethod
    def _validate_db_path(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``db_path`` values."""
        return _validate_optional_non_empty(value, "db_path")


class GetSymbolContextInput(FrozenModel):
    """Validated input parameters for the ``get_symbol_context`` MCP tool."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    symbol_id_or_name: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("symbol", "symbol_id_or_name"),
    )
    repo_id: str | None = None
    edge_limit: int = Field(default=20, ge=1, le=200)
    include_source: bool = False
    max_source_lines: int = Field(default=80, ge=1, le=500)
    db_path: str | None = None

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_db_path(cls, value: Any) -> Any:
        """Convert ``Path`` objects to ``str`` before string validation."""
        return _coerce_db_path_input(value)

    @field_validator("symbol_id_or_name")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        """Reject whitespace-only symbol selectors."""
        return _require_non_empty(value, "symbol")

    @field_validator("repo_id")
    @classmethod
    def _validate_repo_id(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``repo_id`` values."""
        return _validate_optional_non_empty(value, "repo_id")

    @field_validator("db_path")
    @classmethod
    def _validate_db_path(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``db_path`` values."""
        return _validate_optional_non_empty(value, "db_path")


class GetSymbolReferencesInput(FrozenModel):
    """Validated input parameters for the ``get_symbol_references`` MCP tool."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    symbol_id_or_name: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("symbol", "symbol_id_or_name"),
    )
    repo_id: str | None = None
    direction: ReferenceDirection = ReferenceDirection.INCOMING
    refresh_lsp: bool = False
    limit: int = Field(default=30, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    db_path: str | None = None

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_db_path(cls, value: Any) -> Any:
        """Convert ``Path`` objects to ``str`` before string validation."""
        return _coerce_db_path_input(value)

    @field_validator("symbol_id_or_name")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        """Reject whitespace-only symbol selectors."""
        return _require_non_empty(value, "symbol")

    @field_validator("repo_id")
    @classmethod
    def _validate_repo_id(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``repo_id`` values."""
        return _validate_optional_non_empty(value, "repo_id")

    @field_validator("db_path")
    @classmethod
    def _validate_db_path(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``db_path`` values."""
        return _validate_optional_non_empty(value, "db_path")


class AnalyzeRiskInput(FrozenModel):
    """Validated input parameters for the ``analyze_risk`` MCP tool."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    symbols: list[str] = Field(
        ...,
        min_length=1,
        max_length=50,
        validation_alias=AliasChoices("symbols", "symbol_ids"),
    )
    repo_id: str | None = None
    db_path: str | None = None

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_db_path(cls, value: Any) -> Any:
        """Convert ``Path`` objects to ``str`` before string validation."""
        return _coerce_db_path_input(value)

    @field_validator("symbols")
    @classmethod
    def _validate_symbols(cls, value: list[str]) -> list[str]:
        """Ensure every symbol entry is non-empty after stripping whitespace."""
        if not value:
            raise ValueError("symbols must not be empty")
        return [_require_non_empty(item, f"symbols[{idx}]") for idx, item in enumerate(value)]

    @field_validator("repo_id")
    @classmethod
    def _validate_repo_id(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``repo_id`` values."""
        return _validate_optional_non_empty(value, "repo_id")

    @field_validator("db_path")
    @classmethod
    def _validate_db_path(cls, value: str | None) -> str | None:
        """Reject whitespace-only ``db_path`` values."""
        return _validate_optional_non_empty(value, "db_path")


__all__ = [
    "AnalyzeRiskInput",
    "GetFileOutlineInput",
    "GetRepoOverviewInput",
    "GetSymbolContextInput",
    "GetSymbolReferencesInput",
    "GetSymbolSourceInput",
    "ListFilesInput",
    "SearchSymbolsInput",
]
