"""Tracked file entity, summary, disk-verification, and pagination models."""

from pathlib import Path

from pydantic import Field, field_validator

from core.models.base import FrozenModel
from core.models.enums import FreshnessStatus
from core.utils.timestamps import validate_iso_timestamp


class FileRecord(FrozenModel):
    """Canonical record representing an indexed source file."""

    id: str = Field(..., min_length=1, pattern=r"^file:.+")
    repo_id: str = Field(..., min_length=1, pattern=r"^repo:.+")
    file_path: str = Field(..., min_length=1)
    uri: str = Field(..., pattern=r"^file://")
    module_path: str = Field(..., min_length=1)
    language: str = "python"
    content_hash: str = Field(..., pattern=r"^sha256:.+")
    size_bytes: int = Field(..., ge=0)
    line_count: int = Field(default=0, ge=0)
    is_test: bool = False
    last_modified_at: str
    last_indexed_at: str | None = None

    @field_validator("last_modified_at", "last_indexed_at")
    @classmethod
    def _validate_timestamps(cls, value: str | None) -> str | None:
        """Validate file timestamps are ISO 8601 strings.

        Args:
            value: Candidate timestamp string or ``None``.

        Returns:
            Validated timestamp string or ``None``.

        Raises:
            ValueError: If ``value`` is not a valid ISO 8601 string.
        """
        return validate_iso_timestamp(value)


class FileSymbolCountRow(FrozenModel):
    """Pair combining a ``FileRecord`` with its extracted symbol count."""

    file: FileRecord
    symbol_count: int = 0


class TrackedFileSummary(FrozenModel):
    """UI-ready and CLI/MCP-ready file summary enriched with symbol count and freshness."""

    id: str
    file_path: str
    module_path: str
    language: str = "python"
    line_count: int = 0
    symbol_count: int = 0
    size_bytes: int = 0
    is_test: bool = False
    content_hash: str = ""
    last_indexed_at: str | None = None
    freshness: str = "fresh"
    is_fresh: bool = True
    stale_reason: str | None = None


class FilePageQuery(FrozenModel):
    """Pagination and filter parameters for listing tracked repository files."""

    repo_id: str
    path_prefix: str | None = None
    include_tests: bool = False
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)


class FilePageResult(FrozenModel):
    """Paginated page of ``FileRecord`` items with total matching count."""

    total_count: int
    items: list[FileRecord]


class FreshnessCheckResult(FrozenModel):
    """Structured outcome of verifying an indexed ``FileRecord`` against disk."""

    is_fresh: bool
    stale_reason: str | None = None
    freshness: FreshnessStatus


class VerifiedFileText(FrozenModel):
    """Verified UTF-8 file contents, split lines, raw-byte SHA-256 hash, and freshness."""

    path: Path
    text: str
    lines: list[str]
    content_hash: str
    is_fresh: bool
    stale_reason: str | None = None
