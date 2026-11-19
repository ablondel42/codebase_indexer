"""Repository directory and single-file scanner service."""

import os
from datetime import UTC, datetime
from pathlib import Path

from core.models.config import AppConfig
from core.models.enums import Language
from core.models.exceptions import ParseError, PathSecurityError
from core.models.file import FileRecord
from core.models.repo import RepoRecord
from core.utils.hashing import sha256_file
from core.utils.logging import get_logger
from core.utils.pathing import (
    derive_file_id,
    derive_module_path,
    derive_repo_id,
    ensure_safe_repo_file,
    normalize_repo_root,
    to_file_uri,
    to_relative_path,
)
from core.utils.timestamps import utc_now_iso
from parsing.models.scan import ScanOptions, ScanReport
from parsing.utils.filters import is_supported_source_file, is_test_path, should_ignore_dir

logger = get_logger("parsing.services.scanner")


class RepoScanner:
    """Safely scans a repository root or individual file into typed records."""

    def __init__(self, config: AppConfig | None = None) -> None:
        """Initialize the repository scanner with optional application configuration.

        Args:
            config: Optional ``AppConfig`` controlling ignore globs, test inclusion,
                supported extensions, and ``max_file_size_bytes``.
        """
        resolved_config = config if config is not None else AppConfig()
        self._config = resolved_config
        self._options = ScanOptions.from_config(resolved_config)

    @property
    def options(self) -> ScanOptions:
        """Return the immutable ``ScanOptions`` active on this scanner.

        Returns:
            Configured ``ScanOptions`` instance.
        """
        return self._options

    @staticmethod
    def _count_lines(file_path: Path) -> int:
        """Count lines in ``file_path`` matching ``str.splitlines()`` semantics.

        Args:
            file_path: Verified regular file path on disk.

        Returns:
            Total number of lines in the file.

        Raises:
            OSError: If opening or reading ``file_path`` fails.
        """
        with open(file_path, encoding="utf-8", errors="replace") as handle:
            return len(handle.read().splitlines())

    def build_repo_record(self, repo_root: Path | str) -> RepoRecord:
        """Build a canonical ``RepoRecord`` from a repository root path.

        Args:
            repo_root: Candidate path to the repository root directory.

        Returns:
            Validated ``RepoRecord`` with collision-resistant ``repo_id``.

        Raises:
            FileNotFoundError: If ``repo_root`` does not exist on disk.
            NotADirectoryError: If ``repo_root`` is not a directory.
        """
        resolved = normalize_repo_root(repo_root)
        now = utc_now_iso()
        return RepoRecord(
            id=derive_repo_id(resolved),
            root_path=str(resolved),
            name=resolved.name,
            default_language=Language.PYTHON,
            created_at=now,
            last_indexed_at=now,
        )

    def scan_file(
        self,
        repo_id: str,
        repo_root: Path | str,
        file_path: Path | str,
    ) -> FileRecord:
        """Validate and build a ``FileRecord`` for a single file inside ``repo_root``.

        Args:
            repo_id: Owning repository identifier.
            repo_root: Repository root directory path.
            file_path: Candidate file path (absolute or relative to ``repo_root``).

        Returns:
            Populated ``FileRecord`` with hash, line count, and ``is_test`` flag.

        Raises:
            PathSecurityError: If ``file_path`` is a symlink, escapes ``repo_root``,
                or is not a regular file.
            ParseError: If the file exceeds ``options.max_file_size_bytes``.
            OSError: If stating or reading the file fails.
        """
        root_path = Path(repo_root)
        safe_path = ensure_safe_repo_file(root_path, file_path)
        stat_res = safe_path.stat()
        max_bytes = self._options.max_file_size_bytes

        if stat_res.st_size > max_bytes:
            logger.warning(
                "Skipping oversized file %s (%d bytes > %d max_file_size_bytes)",
                safe_path,
                stat_res.st_size,
                max_bytes,
            )
            raise ParseError(
                f"File exceeds max_file_size_bytes ({max_bytes}): {safe_path}",
                context={"file_path": str(safe_path), "size_bytes": stat_res.st_size},
            )

        rel_path = to_relative_path(root_path, safe_path)
        now = utc_now_iso()
        return FileRecord(
            id=derive_file_id(repo_id, rel_path),
            repo_id=repo_id,
            file_path=rel_path,
            uri=to_file_uri(safe_path),
            module_path=derive_module_path(root_path, safe_path),
            language=Language.PYTHON.value,
            content_hash=sha256_file(safe_path, max_file_size_bytes=max_bytes),
            size_bytes=stat_res.st_size,
            line_count=self._count_lines(safe_path),
            is_test=is_test_path(rel_path, self._options.test_dir_names),
            last_modified_at=datetime.fromtimestamp(stat_res.st_mtime, tz=UTC).isoformat(),
            last_indexed_at=now,
        )

    def _is_allowed_subdir(self, current_dir: Path, dirname: str) -> bool:
        """Check whether ``dirname`` under ``current_dir`` should be traversed.

        Args:
            current_dir: Current directory visited by ``os.walk``.
            dirname: Candidate subdirectory name.

        Returns:
            ``True`` when ``dirname`` is a non-symlink directory permitted by scan rules.
        """
        if (current_dir / dirname).is_symlink():
            logger.debug("Skipping symlinked directory during scan: %s", current_dir / dirname)
            return False
        if should_ignore_dir(dirname, self._options.ignored_dirs):
            return False
        return self._options.include_tests or not should_ignore_dir(
            dirname, self._options.test_dir_names
        )

    def _scan_candidate(
        self,
        repo_id: str,
        root: Path,
        candidate: Path,
    ) -> FileRecord | None:
        """Scan one candidate path, logging a warning and returning ``None`` when skipped.

        Args:
            repo_id: Owning repository identifier.
            root: Normalized repository root path.
            candidate: Candidate file path inside ``root``.

        Returns:
            ``FileRecord`` if valid and supported, otherwise ``None``.
        """
        if not is_supported_source_file(candidate, self._options.supported_extensions):
            return None
        try:
            return self.scan_file(repo_id, root, candidate)
        except (PathSecurityError, ParseError, OSError) as exc:
            logger.warning("Skipping file %s during scan of %s: %s", candidate, repo_id, exc)
            return None

    def scan_repository(self, repo_root: Path | str) -> ScanReport:
        """Walk ``repo_root`` safely and return a ``ScanReport`` sorted by ``file_path``.

        Args:
            repo_root: Path to the repository root directory.

        Returns:
            ``ScanReport`` containing the ``RepoRecord`` and sorted ``FileRecord`` list.

        Raises:
            FileNotFoundError: If ``repo_root`` does not exist.
            NotADirectoryError: If ``repo_root`` is not a directory.
        """
        root = normalize_repo_root(repo_root)
        repo = self.build_repo_record(root)
        files: list[FileRecord] = []

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current_dir = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if self._is_allowed_subdir(current_dir, d))
            for filename in sorted(filenames):
                record = self._scan_candidate(repo.id, root, current_dir / filename)
                if record is not None:
                    files.append(record)

        files.sort(key=lambda item: item.file_path)
        logger.debug("Scanned repository %s (%d files matched)", repo.id, len(files))
        return ScanReport(repo=repo, files=files)


__all__ = [
    "RepoScanner",
]
