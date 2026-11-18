"""Repository scanner for Python source files."""

import fnmatch
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repo_context.constants import Language
from repo_context.exceptions import ParseError, PathSecurityError
from repo_context.logging_config import get_logger
from repo_context.models.file import FileRecord
from repo_context.models.repo import RepoRecord
from repo_context.parsing.hashing import sha256_file
from repo_context.parsing.pathing import (
    derive_file_id,
    derive_module_path,
    derive_repo_id,
    ensure_safe_repo_file,
    normalize_repo_root,
    to_file_uri,
    to_relative_path,
)

logger = get_logger("parsing.scanner")

DEFAULT_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024
DEFAULT_TEST_DIR_NAMES = ("tests", "test")


@dataclass(frozen=True)
class _ScanOptions:
    """Normalized repository scanning options derived from application config."""

    include_tests: bool
    test_dir_names: tuple[str, ...]
    max_file_size_bytes: int
    ignored_dirs: tuple[str, ...]
    supported_extensions: tuple[str, ...]


def should_ignore_dir(name: str, ignored_dirs: tuple[str, ...]) -> bool:
    """Check if a directory name matches any ignore glob pattern.

    Args:
        name: Directory name to check.
        ignored_dirs: Tuple of directory names or glob patterns (e.g. ``*.egg-info``).

    Returns:
        True if the directory should be ignored.
    """
    return any(fnmatch.fnmatch(name, pattern) for pattern in ignored_dirs)


def is_test_path(rel_path: str, test_dir_names: tuple[str, ...] = DEFAULT_TEST_DIR_NAMES) -> bool:
    """Determine if a repository-relative file path lives inside a test directory."""
    parts = Path(rel_path).parts
    return any(should_ignore_dir(part, test_dir_names) for part in parts[:-1])


def is_supported_source_file(path: Path, supported_extensions: tuple[str, ...]) -> bool:
    """Check if a file is a supported source file.

    Args:
        path: File path to check.
        supported_extensions: Tuple of supported file extensions.

    Returns:
        True if the file is a supported source file.
    """
    return path.suffix in supported_extensions


def _utc_now_iso() -> str:
    """Get current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _count_lines(file_path: Path) -> int:
    """Count lines in a text file matching ``str.splitlines()`` semantics."""
    with open(file_path, encoding="utf-8", errors="replace") as f:
        return len(f.read().splitlines())


def build_repo_record(repo_root: Path) -> RepoRecord:
    """Build a RepoRecord from a repository root path.

    Args:
        repo_root: Repository root path.

    Returns:
        A new RepoRecord instance with collision-resistant ``repo_id``.
    """
    resolved = normalize_repo_root(repo_root)
    now = _utc_now_iso()

    return RepoRecord(
        id=derive_repo_id(resolved),
        root_path=str(resolved),
        name=resolved.name,
        default_language=Language.PYTHON,
        created_at=now,
        last_indexed_at=now,
    )


def build_file_record(
    repo_id: str,
    repo_root: Path,
    file_path: Path,
    *,
    max_file_size_bytes: int | None = None,
    test_dir_names: tuple[str, ...] = DEFAULT_TEST_DIR_NAMES,
) -> FileRecord:
    """Build a validated ``FileRecord`` from a file path inside ``repo_root``.

    Args:
        repo_id: The repository ID.
        repo_root: Repository root path.
        file_path: Candidate file path inside ``repo_root``.
        max_file_size_bytes: Optional maximum file size in bytes before hashing.
        test_dir_names: Directory name/glob patterns marking test files.

    Returns:
        A new ``FileRecord`` instance with ``line_count`` and ``is_test`` populated.

    Raises:
        PathSecurityError: If ``file_path`` is a symlink, escapes ``repo_root``,
            or is not a regular file.
        ParseError: If the file exceeds ``max_file_size_bytes``.
    """
    safe_path = ensure_safe_repo_file(repo_root, file_path)
    stat_res = safe_path.stat()

    if max_file_size_bytes is not None and stat_res.st_size > max_file_size_bytes:
        logger.warning(
            "Skipping oversized file %s (%d bytes > %d max_file_size_bytes)",
            safe_path,
            stat_res.st_size,
            max_file_size_bytes,
        )
        raise ParseError(
            f"File exceeds max_file_size_bytes ({max_file_size_bytes}): {safe_path}",
            context={"file_path": str(safe_path), "size_bytes": stat_res.st_size},
        )

    rel_path = to_relative_path(repo_root, safe_path)
    now = _utc_now_iso()

    return FileRecord(
        id=derive_file_id(repo_id, rel_path),
        repo_id=repo_id,
        file_path=rel_path,
        uri=to_file_uri(safe_path),
        module_path=derive_module_path(repo_root, safe_path),
        language="python",
        content_hash=sha256_file(safe_path, max_file_size_bytes=max_file_size_bytes),
        size_bytes=stat_res.st_size,
        line_count=_count_lines(safe_path),
        is_test=is_test_path(rel_path, test_dir_names),
        last_modified_at=datetime.fromtimestamp(stat_res.st_mtime, tz=UTC).isoformat(),
        last_indexed_at=now,
    )


def _scan_single_file(
    repo_id: str,
    root: Path,
    candidate: Path,
    options: _ScanOptions,
) -> FileRecord | None:
    """Validate and build a ``FileRecord`` for one candidate path, or return None if skipped."""
    if not is_supported_source_file(candidate, options.supported_extensions):
        return None
    try:
        safe_path = ensure_safe_repo_file(root, candidate)
        size_bytes = safe_path.stat().st_size
        if size_bytes > options.max_file_size_bytes:
            logger.warning(
                "Skipping oversized file %s (%d bytes > %d max_file_size_bytes)",
                safe_path,
                size_bytes,
                options.max_file_size_bytes,
            )
            return None
        return build_file_record(
            repo_id,
            root,
            safe_path,
            max_file_size_bytes=options.max_file_size_bytes,
            test_dir_names=options.test_dir_names,
        )
    except (PathSecurityError, ParseError, OSError) as exc:
        logger.warning("Skipping file %s: %s", candidate, exc)
        return None


def _scan_directory_files(
    repo_id: str,
    root: Path,
    current_dir: Path,
    filenames: list[str],
    options: _ScanOptions,
) -> list[FileRecord]:
    """Scan all filenames in ``current_dir`` and return valid ``FileRecord`` entries."""
    records: list[FileRecord] = []
    for filename in sorted(filenames):
        record = _scan_single_file(repo_id, root, current_dir / filename, options)
        if record is not None:
            records.append(record)
    return records


def scan_repository(repo_root: str | Path, config: Any) -> tuple[RepoRecord, list[FileRecord]]:
    """Scan a repository and return ``RepoRecord`` and sorted ``FileRecord`` list.

    Enforces symlink/traversal security checks, skips oversized files with a
    warning, respects ``config.ignored_dirs`` (including globs like ``*.egg-info``),
    and respects ``config.include_tests`` vs ``config.test_dir_names``.

    Args:
        repo_root: Path to the repository root.
        config: Application configuration.

    Returns:
        Tuple of ``(RepoRecord, list[FileRecord])`` sorted by ``file_path``.

    Raises:
        FileNotFoundError: If the repo path does not exist.
        NotADirectoryError: If the repo path is not a directory.
    """
    root = normalize_repo_root(repo_root)
    repo = build_repo_record(root)
    options = _ScanOptions(
        include_tests=getattr(config, "include_tests", False),
        test_dir_names=getattr(config, "test_dir_names", DEFAULT_TEST_DIR_NAMES),
        max_file_size_bytes=getattr(config, "max_file_size_bytes", DEFAULT_MAX_FILE_SIZE_BYTES),
        ignored_dirs=getattr(config, "ignored_dirs", ()),
        supported_extensions=getattr(config, "supported_extensions", (".py",)),
    )
    results: list[FileRecord] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current_dir = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not (current_dir / d).is_symlink()
            and not should_ignore_dir(d, options.ignored_dirs)
            and (options.include_tests or not should_ignore_dir(d, options.test_dir_names))
        )
        results.extend(_scan_directory_files(repo.id, root, current_dir, filenames, options))

    results.sort(key=lambda x: x.file_path)
    return repo, results
