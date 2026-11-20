"""Full-codebase trigram text index, code search query, and match report models."""

from pathlib import Path

from pydantic import Field

from core.models.base import FrozenModel


class TextFileStat(FrozenModel):
    """Lightweight on-disk file stat entry for fast repository freshness reconciliation."""

    file_path: str = Field(..., min_length=1)
    absolute_path: Path
    size_bytes: int = Field(..., ge=0)
    mtime_ns: int = Field(..., ge=0)
    is_test: bool = False


class TextDocumentMeta(FrozenModel):
    """Persisted metadata row from ``text_files`` excluding raw ``content`` text."""

    id: str = Field(..., min_length=1, pattern="^file:.+")
    repo_id: str = Field(..., min_length=1, pattern="^project:.+")
    file_path: str = Field(..., min_length=1)
    language: str = "text"
    content_hash: str = Field(..., pattern="^sha256:.+")
    size_bytes: int = Field(..., ge=0)
    line_count: int = Field(default=0, ge=0)
    mtime_ns: int = Field(default=0, ge=0)
    is_test: bool = False


class TextDocumentRecord(FrozenModel):
    """Canonical UTF-8 repository text document stored in ``text_files`` and ``text_fts``."""

    id: str = Field(..., min_length=1, pattern="^file:.+")
    repo_id: str = Field(..., min_length=1, pattern="^project:.+")
    file_path: str = Field(..., min_length=1)
    language: str = "text"
    content_hash: str = Field(..., pattern="^sha256:.+")
    size_bytes: int = Field(..., ge=0)
    line_count: int = Field(default=0, ge=0)
    mtime_ns: int = Field(default=0, ge=0)
    is_test: bool = False
    content: str


class TextCandidateQuery(FrozenModel):
    """Storage filter and required ASCII trigram literals for candidate document retrieval."""

    repo_id: str
    required_trigrams: tuple[str, ...] = ()
    path_prefix: str | None = None
    language: str | None = None
    include_tests: bool = True


class CodeSearchQuery(FrozenModel):
    """Parameters for hybrid trigram + live-disk full-codebase text/regex search."""

    query: str = Field(..., min_length=1)
    repo: str | None = None
    regex: bool = False
    case_sensitive: bool = False
    path_prefix: str | None = None
    language: str | None = None
    include_tests: bool = True
    verify_disk: bool = True
    context_lines: int = Field(default=0, ge=0, le=5)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class FileMatchCount(FrozenModel):
    """Per-file match count summary returned across all matching repository files."""

    file_path: str
    language: str
    count: int = Field(..., ge=1)


class CodeMatchItem(FrozenModel):
    """Single line match enriched with optional context lines and enclosing Python symbol."""

    file_path: str
    language: str
    line: int = Field(..., ge=1)
    column: int = Field(..., ge=1)
    preview: str
    location: str | None = None
    context_before: tuple[str, ...] = ()
    context_after: tuple[str, ...] = ()
    symbol_id: str | None = None
    qualified_name: str | None = None
    symbol_kind: str | None = None
    symbol_lines: str | None = None
    symbol_location: str | None = None


class CodeSearchStats(FrozenModel):
    """Execution telemetry confirming trigram pruning and live disk reconciliation."""

    indexed_files: int = 0
    candidate_files_scanned: int = 0
    trigram_pruned: bool = False
    live_overlay_files: int = 0
    deleted_overlay_files: int = 0


class CodeSearchReport(FrozenModel):
    """Complete full-codebase search response with anti-truncation ``file_counts`` manifest."""

    repo_id: str
    query: str
    regex: bool
    case_sensitive: bool
    total_matches: int = Field(default=0, ge=0)
    matched_file_count: int = Field(default=0, ge=0)
    returned_count: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1)
    has_more: bool = False
    file_counts: list[FileMatchCount] = Field(default_factory=list)
    matches: list[CodeMatchItem] = Field(default_factory=list)
    stats: CodeSearchStats = Field(default_factory=CodeSearchStats)


__all__ = [
    "CodeMatchItem",
    "CodeSearchQuery",
    "CodeSearchReport",
    "CodeSearchStats",
    "FileMatchCount",
    "TextCandidateQuery",
    "TextDocumentMeta",
    "TextDocumentRecord",
    "TextFileStat",
]
