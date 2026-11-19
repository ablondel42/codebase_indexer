"""Pure stateless path and extension filter helpers for repository scanning."""

import fnmatch
from pathlib import Path

DEFAULT_TEST_DIR_NAMES: tuple[str, ...] = ("tests", "test")
DEFAULT_SUPPORTED_EXTENSIONS: tuple[str, ...] = (".py",)


def should_ignore_dir(name: str, ignored_dirs: tuple[str, ...]) -> bool:
    """Check whether a directory name matches any ignore name or glob pattern.

    Args:
        name: Directory basename to test.
        ignored_dirs: Tuple of directory names or ``fnmatch`` patterns (e.g. ``*.egg-info``).

    Returns:
        ``True`` if ``name`` matches at least one pattern in ``ignored_dirs``.
    """
    return any(fnmatch.fnmatch(name, pattern) for pattern in ignored_dirs)


def is_test_path(
    rel_path: str,
    test_dir_names: tuple[str, ...] = DEFAULT_TEST_DIR_NAMES,
) -> bool:
    """Determine whether a repository-relative file path lives inside a test directory.

    Args:
        rel_path: Repository-relative POSIX or platform file path.
        test_dir_names: Directory name or glob patterns marking test directories.

    Returns:
        ``True`` if any parent directory component matches ``test_dir_names``.
    """
    parts = Path(rel_path).parts
    return any(should_ignore_dir(part, test_dir_names) for part in parts[:-1])


def is_supported_source_file(
    path: Path,
    supported_extensions: tuple[str, ...] = DEFAULT_SUPPORTED_EXTENSIONS,
) -> bool:
    """Check whether ``path`` carries a supported source file suffix.

    Args:
        path: Candidate file path.
        supported_extensions: Tuple of supported suffixes including leading dot.

    Returns:
        ``True`` when ``path.suffix`` belongs to ``supported_extensions``.
    """
    return path.suffix in supported_extensions


__all__ = [
    "DEFAULT_SUPPORTED_EXTENSIONS",
    "DEFAULT_TEST_DIR_NAMES",
    "is_supported_source_file",
    "is_test_path",
    "should_ignore_dir",
]
