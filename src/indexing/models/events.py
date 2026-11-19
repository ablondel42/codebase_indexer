"""Pydantic v2 file-change and repository-watch lifecycle event models."""

from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from core.models.base import FrozenModel
from core.models.config import AppConfig
from core.utils.pathing import derive_repo_id
from indexing.models.results import IncrementalFileResult, IncrementalStatus


class FileEventType(StrEnum):
    """Normalized filesystem change event kinds."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class NormalizeContext(FrozenModel):
    """Immutable path-confinement and filter context for ``WatchEventNormalizer``."""

    repo_id: str
    repo_root: Path
    resolved_root: Path
    ignored_dirs: tuple[str, ...] = ()
    test_dir_names: tuple[str, ...] = ("tests", "test")
    include_tests: bool = False
    supported_extensions: tuple[str, ...] = (".py",)

    @classmethod
    def from_config(cls, repo_root: Path, config: AppConfig) -> Self:
        """Build a ``NormalizeContext`` from a repository root path and ``AppConfig``.

        Args:
            repo_root: Raw repository root path supplied by the caller.
            config: Application configuration containing ignore and extension filters.

        Returns:
            Populated ``NormalizeContext`` instance.
        """
        raw_root = Path(repo_root)
        resolved_root = raw_root.resolve()
        return cls(
            repo_id=derive_repo_id(resolved_root),
            repo_root=raw_root,
            resolved_root=resolved_root,
            ignored_dirs=config.ignored_dirs,
            test_dir_names=config.test_dir_names,
            include_tests=config.include_tests,
            supported_extensions=config.supported_extensions,
        )


class FileChangeEvent(FrozenModel):
    """Normalized, repository-confined file change event."""

    event_type: FileEventType
    absolute_path: str
    repo_relative_path: str
    is_supported: bool = True
    old_absolute_path: str | None = None
    old_repo_relative_path: str | None = None
    repo_id: str | None = None


class WatchStartedEvent(FrozenModel):
    """Callback event emitted once ``RepoWatcher`` starts observing a repository."""

    event: Literal["watch_started"] = "watch_started"
    repo_root: str
    db_path: str
    debounce_ms: int


class WatchStoppedEvent(FrozenModel):
    """Callback event emitted when ``RepoWatcher`` stops observing a repository."""

    event: Literal["watch_stopped"] = "watch_stopped"
    repo_root: str


class WatchFileProcessedEvent(FrozenModel):
    """Callback event emitted for a file processed during an active watch batch."""

    event: Literal["file_processed"] = "file_processed"
    file_path: str
    status: IncrementalStatus
    node_count: int = 0
    edge_count: int = 0
    deleted_node_count: int = 0
    deleted_edge_count: int = 0
    invalidated_reference_edge_count: int = 0
    invalidated_target_symbol_count: int = 0
    resolved_edge_count: int = 0
    reset_edge_count: int = 0
    error_message: str | None = None

    @classmethod
    def from_file_result(cls, result: IncrementalFileResult) -> Self:
        """Build a ``WatchFileProcessedEvent`` from an ``IncrementalFileResult``.

        Args:
            result: Incremental per-file processing outcome.

        Returns:
            Populated ``WatchFileProcessedEvent`` instance.
        """
        return cls(
            file_path=result.file_path,
            status=result.status,
            node_count=result.node_count,
            edge_count=result.edge_count,
            deleted_node_count=result.deleted_node_count,
            deleted_edge_count=result.deleted_edge_count,
            invalidated_reference_edge_count=result.invalidated_reference_edge_count,
            invalidated_target_symbol_count=result.invalidated_target_symbol_count,
            resolved_edge_count=result.resolved_edge_count,
            reset_edge_count=result.reset_edge_count,
            error_message=result.error_message,
        )


__all__ = [
    "FileChangeEvent",
    "FileEventType",
    "NormalizeContext",
    "WatchFileProcessedEvent",
    "WatchStartedEvent",
    "WatchStoppedEvent",
]
