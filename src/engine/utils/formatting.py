"""Pure stateless path-filter, pattern-matching, freshness, and directory helpers."""

import fnmatch
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from core.models.file import FileRecord
from core.utils.logging import get_logger
from engine.models.responses import DirectoryFileCount

logger = get_logger("engine.utils.formatting")


def normalize_path_prefix(path_prefix: str) -> str:
    """Normalize a repository path prefix without stripping leading dot-directories.

    Strips surrounding whitespace, repeated leading ``./`` segments, and trailing ``/``
    separators while preserving directory names like ``.github``.

    Args:
        path_prefix: Raw user-supplied path prefix string.

    Returns:
        Normalized POSIX path prefix string.
    """
    cleaned = path_prefix.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned.removeprefix("./")
    return cleaned.rstrip("/")


def normalize_repo_file_filter(file_filter: str) -> str:
    """Normalize a symbol file or directory filter string.

    Args:
        file_filter: Raw user-supplied relative file or directory filter.

    Returns:
        Normalized POSIX file or directory filter string.
    """
    return normalize_path_prefix(file_filter)


def relativize_if_under_root(file_path: str, repo_root: str | None) -> str:
    """Convert an absolute ``file_path`` inside ``repo_root`` to a relative POSIX path.

    Leaves relative paths and paths outside ``repo_root`` unchanged, logging a ``DEBUG``
    diagnostic when resolution or ``relative_to`` fails.

    Args:
        file_path: Candidate absolute or repository-relative file path.
        repo_root: Optional repository root directory path string.

    Returns:
        Repository-relative POSIX path when confined under ``repo_root``, else ``file_path``.
    """
    candidate = Path(file_path)
    if not candidate.is_absolute() or not repo_root:
        return file_path
    try:
        return candidate.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except (ValueError, OSError) as exc:
        logger.debug(
            "Path %s is not relative to repository root %s; keeping as-is: %s",
            file_path,
            repo_root,
            exc,
        )
        return file_path


def matches_file_pattern(file_path: str, module_path: str, pattern: str | None) -> bool:
    """Match a file or module path against an optional glob or case-insensitive substring.

    Uses ``fnmatch.fnmatch`` when ``pattern`` contains wildcard metacharacters (``*?["``);
    otherwise performs a case-insensitive substring check against ``file_path`` and
    ``module_path``.

    Args:
        file_path: Repository-relative POSIX file path.
        module_path: Dotted Python module path.
        pattern: Optional glob or substring filter pattern.

    Returns:
        ``True`` when ``pattern`` is blank/``None`` or matches either path.
    """
    if not pattern:
        return True
    cleaned = pattern.strip()
    if not cleaned:
        return True
    if any(ch in cleaned for ch in "*?["):
        return fnmatch.fnmatch(file_path, cleaned) or fnmatch.fnmatch(module_path, cleaned)
    lowered = cleaned.lower()
    return lowered in file_path.lower() or lowered in module_path.lower()


def format_cli_freshness_label(is_fresh: bool, stale_reason: str | None) -> str:
    """Translate disk freshness check state into a human-readable CLI status label.

    Args:
        is_fresh: Whether the indexed file matches live disk state.
        stale_reason: Optional machine-readable staleness reason code.

    Returns:
        ``"fresh"``, ``"deleted"``, ``"modified"``, or ``stale_reason``/``"stale"``.
    """
    if is_fresh:
        return "fresh"
    if stale_reason == "file_missing":
        return "deleted"
    if stale_reason == "content_hash_mismatch":
        return "modified"
    return stale_reason or "stale"


def aggregate_top_directories(
    files: Sequence[FileRecord],
    limit: int = 15,
) -> list[DirectoryFileCount]:
    """Count non-test tracked files grouped by top-level repository directory.

    Root-level files without a ``/`` separator are counted under ``"."``.

    Args:
        files: Sequence of tracked ``FileRecord`` items for a repository.
        limit: Maximum number of top directories to return (default ``15``).

    Returns:
        Descending-count list of ``DirectoryFileCount`` entries.
    """
    if limit <= 0:
        return []
    counts: Counter[str] = Counter()
    for record in files:
        if record.is_test:
            continue
        rel = record.file_path
        top_dir = rel.split("/", 1)[0] if "/" in rel else "."
        counts[top_dir] += 1
    return [
        DirectoryFileCount(path=path, file_count=count)
        for path, count in counts.most_common(limit)
    ]


__all__ = [
    "aggregate_top_directories",
    "format_cli_freshness_label",
    "matches_file_pattern",
    "normalize_path_prefix",
    "normalize_repo_file_filter",
    "relativize_if_under_root",
]
