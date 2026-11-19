"""Immutable Pydantic v2 response and pagination envelopes returned by ``QueryEngine``."""

from pydantic import Field

from core.models.base import FrozenModel
from core.models.diagnostics import RepoGraphStats, RepoStorageSummary
from core.models.file import TrackedFileSummary
from core.models.node import SymbolSummary
from core.models.repo import RepoRecord
from core.models.runs import IndexRunRecord


class DirectoryFileCount(FrozenModel):
    """Non-test tracked file count aggregated under a top-level repository directory."""

    path: str
    file_count: int


class RepoOverview(FrozenModel):
    """High-level repository metadata, graph statistics, top directories, and recent runs."""

    repo: RepoRecord
    stats: RepoGraphStats
    top_directories: list[DirectoryFileCount] = Field(default_factory=list)
    recent_runs: list[IndexRunRecord] = Field(default_factory=list)


class FileListPage(FrozenModel):
    """Paginated page of ``TrackedFileSummary`` items for a repository."""

    repo_id: str
    total_count: int
    returned_count: int
    offset: int
    limit: int
    has_more: bool
    files: list[TrackedFileSummary] = Field(default_factory=list)


class SymbolSearchPage(FrozenModel):
    """Ranked symbol search result page for a repository."""

    repo_id: str
    query: str
    count: int
    symbols: list[SymbolSummary] = Field(default_factory=list)


class SymbolListPage(FrozenModel):
    """Paginated page of ``SymbolSummary`` items for a repository."""

    repo_id: str
    total_count: int
    returned_count: int
    offset: int
    limit: int
    has_more: bool
    symbols: list[SymbolSummary] = Field(default_factory=list)


class RepoSummaryListReport(FrozenModel):
    """Database-wide per-repository storage summary listing."""

    db_path: str
    repos: list[RepoStorageSummary] = Field(default_factory=list)


class IndexRunListReport(FrozenModel):
    """Recent indexing run audit report for a database or specific repository."""

    db_path: str
    repo_id: str | None = None
    runs: list[IndexRunRecord] = Field(default_factory=list)


__all__ = [
    "DirectoryFileCount",
    "FileListPage",
    "IndexRunListReport",
    "RepoOverview",
    "RepoSummaryListReport",
    "SymbolListPage",
    "SymbolSearchPage",
]
