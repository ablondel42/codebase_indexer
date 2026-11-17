"""Strict Pydantic input validation schemas for the 8 token-lean MCP tools."""

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

SymbolKindLiteral = Literal[
    "module",
    "class",
    "function",
    "async_function",
    "method",
    "async_method",
    "local_function",
    "local_async_function",
    "constant",
    "attribute",
    "type_alias",
]

DirectionLiteral = Literal["incoming", "outgoing", "both"]


def _require_non_empty(value: str, field_name: str) -> str:
    """Ensure a string input is not empty or whitespace-only."""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be empty or whitespace")
    return stripped


def _validate_optional_non_empty(value: str | None, field_name: str) -> str | None:
    """Validate optional string inputs, rejecting whitespace-only strings."""
    if value is None:
        return None
    return _require_non_empty(value, field_name)


class GetRepoOverviewInput(BaseModel):
    """Input for ``get_repo_overview``."""

    repo_id: str | None = None

    @field_validator("repo_id")
    @classmethod
    def validate_repo_id(cls, v: str | None) -> str | None:
        return _validate_optional_non_empty(v, "repo_id")


class ListFilesInput(BaseModel):
    """Input for ``list_files``."""

    repo_id: str | None = None
    path_prefix: str | None = None
    include_tests: bool = False
    limit: int = Field(default=100, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @field_validator("repo_id")
    @classmethod
    def validate_repo_id(cls, v: str | None) -> str | None:
        return _validate_optional_non_empty(v, "repo_id")

    @field_validator("path_prefix")
    @classmethod
    def validate_path_prefix(cls, v: str | None) -> str | None:
        return _validate_optional_non_empty(v, "path_prefix")


class GetFileOutlineInput(BaseModel):
    """Input for ``get_file_outline``."""

    file_path: str = Field(..., min_length=1)
    repo_id: str | None = None
    include_private: bool = True
    max_depth: int = Field(default=3, ge=1, le=10)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v: str) -> str:
        return _require_non_empty(v, "file_path")

    @field_validator("repo_id")
    @classmethod
    def validate_repo_id(cls, v: str | None) -> str | None:
        return _validate_optional_non_empty(v, "repo_id")


class SearchSymbolsInput(BaseModel):
    """Input for ``search_symbols``."""

    query: str = Field(..., min_length=1)
    repo_id: str | None = None
    kind: SymbolKindLiteral | None = None
    file_path: str | None = None
    limit: int = Field(default=20, ge=1, le=200)

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        return _require_non_empty(v, "query")

    @field_validator("repo_id")
    @classmethod
    def validate_repo_id(cls, v: str | None) -> str | None:
        return _validate_optional_non_empty(v, "repo_id")

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v: str | None) -> str | None:
        return _validate_optional_non_empty(v, "file_path")


class GetSymbolSourceInput(BaseModel):
    """Input for ``get_symbol_source``."""

    model_config = ConfigDict(populate_by_name=True)

    symbol_id_or_name: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("symbol", "symbol_id_or_name"),
    )
    repo_id: str | None = None
    max_lines: int = Field(default=120, ge=1, le=500)
    offset: int = Field(default=0, ge=0)

    @field_validator("symbol_id_or_name")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        return _require_non_empty(v, "symbol")

    @field_validator("repo_id")
    @classmethod
    def validate_repo_id(cls, v: str | None) -> str | None:
        return _validate_optional_non_empty(v, "repo_id")


class GetSymbolContextInput(BaseModel):
    """Input for ``get_symbol_context``."""

    model_config = ConfigDict(populate_by_name=True)

    symbol_id_or_name: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("symbol", "symbol_id_or_name"),
    )
    repo_id: str | None = None
    edge_limit: int = Field(default=20, ge=1, le=200)
    include_source: bool = False
    max_source_lines: int = Field(default=80, ge=1, le=500)

    @field_validator("symbol_id_or_name")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        return _require_non_empty(v, "symbol")

    @field_validator("repo_id")
    @classmethod
    def validate_repo_id(cls, v: str | None) -> str | None:
        return _validate_optional_non_empty(v, "repo_id")


class GetSymbolReferencesInput(BaseModel):
    """Input for ``get_symbol_references``."""

    model_config = ConfigDict(populate_by_name=True)

    symbol_id_or_name: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("symbol", "symbol_id_or_name"),
    )
    repo_id: str | None = None
    direction: DirectionLiteral = "incoming"
    refresh_lsp: bool = False
    limit: int = Field(default=30, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @field_validator("symbol_id_or_name")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        return _require_non_empty(v, "symbol")

    @field_validator("repo_id")
    @classmethod
    def validate_repo_id(cls, v: str | None) -> str | None:
        return _validate_optional_non_empty(v, "repo_id")


class AnalyzeRiskInput(BaseModel):
    """Input for ``analyze_risk``."""

    model_config = ConfigDict(populate_by_name=True)

    symbols: list[str] = Field(
        ...,
        min_length=1,
        max_length=50,
        validation_alias=AliasChoices("symbols", "symbol_ids"),
    )
    repo_id: str | None = None

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("symbols must not be empty")
        normalized: list[str] = []
        for idx, item in enumerate(v):
            normalized.append(_require_non_empty(item, f"symbols[{idx}]"))
        return normalized

    @field_validator("repo_id")
    @classmethod
    def validate_repo_id(cls, v: str | None) -> str | None:
        return _validate_optional_non_empty(v, "repo_id")
