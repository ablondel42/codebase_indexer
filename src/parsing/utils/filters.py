"""Pure stateless path and extension filter helpers for repository scanning."""

from pathlib import Path

from core.utils.text_files import is_ignored_text_dir, is_test_rel_path

DEFAULT_TEST_DIR_NAMES: tuple[str, ...] = ("tests", "test")
DEFAULT_SUPPORTED_EXTENSIONS: tuple[str, ...] = (".py",)


def should_ignore_dir(name: str, ignored_dirs: tuple[str, ...]) -> bool:
    """Check whether a directory name matches any ignore name or glob pattern."""
    return is_ignored_text_dir(name, ignored_dirs)


def is_test_path(
    rel_path: str,
    test_dir_names: tuple[str, ...] = DEFAULT_TEST_DIR_NAMES,
) -> bool:
    """Determine whether a repository-relative file path lives inside a test directory."""
    return is_test_rel_path(rel_path, test_dir_names)


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
