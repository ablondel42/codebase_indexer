"""Repository scanning configuration and report models."""

from typing import Self

from core.models.base import FrozenModel
from core.models.config import AppConfig
from core.models.file import FileRecord
from core.models.repo import RepoRecord


class ScanOptions(FrozenModel):
    """Immutable options controlling repository file discovery and filtering."""

    include_tests: bool = False
    test_dir_names: tuple[str, ...] = ("tests", "test")
    max_file_size_bytes: int = 2 * 1024 * 1024
    ignored_dirs: tuple[str, ...] = ()
    supported_extensions: tuple[str, ...] = (".py",)

    @classmethod
    def from_config(cls, config: AppConfig) -> Self:
        """Construct ``ScanOptions`` from an ``AppConfig`` instance.

        Args:
            config: Application configuration providing scan rules and limits.

        Returns:
            Populated ``ScanOptions`` model.
        """
        return cls(
            include_tests=config.include_tests,
            test_dir_names=config.test_dir_names,
            max_file_size_bytes=config.max_file_size_bytes,
            ignored_dirs=config.ignored_dirs,
            supported_extensions=config.supported_extensions,
        )


class ScanReport(FrozenModel):
    """Result of scanning a repository root directory for supported source files."""

    repo: RepoRecord
    files: list[FileRecord]


__all__ = [
    "ScanOptions",
    "ScanReport",
]
