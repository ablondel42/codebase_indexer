"""File change event normalization.

Converts watcher-specific event objects into a stable internal event shape.
"""

import contextlib
import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repo_context.config import AppConfig
from repo_context.exceptions import PathSecurityError
from repo_context.logging_config import get_logger
from repo_context.parsing.pathing import derive_repo_id, ensure_safe_repo_file

logger = get_logger("indexing.events")

_IGNORED_TEMP_SUFFIXES = ("~", ".swp", ".tmp")
_EVENT_BOOL_ATTRS: tuple[tuple[str, str], ...] = (
    ("is_created", "created"),
    ("is_modified", "modified"),
    ("is_deleted", "deleted"),
)
_EVENT_NAME_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("moved", "moved"),
    ("move", "moved"),
    ("created", "created"),
    ("modified", "modified"),
    ("deleted", "deleted"),
)


@dataclass(frozen=True)
class _NormalizeContext:
    """Shared repository root and filter configuration for normalizing paths."""

    repo_id: str
    repo_root: Path
    resolved_root: Path
    ignored_dirs: tuple[str, ...]
    test_dir_names: tuple[str, ...]
    include_tests: bool
    supported_extensions: tuple[str, ...]


@dataclass
class FileChangeEvent:
    """Normalized file change event.

    Attributes:
        event_type: One of 'created', 'modified', 'deleted'.
        absolute_path: Absolute filesystem path to the file.
        repo_relative_path: Repository-relative path using POSIX-style separators.
        is_supported: True if the file has a supported extension.
        old_absolute_path: Original path for rename events (optional).
        old_repo_relative_path: Original repo-relative path for renames (optional).
        repo_id: Collision-resistant repository identifier (optional).
    """

    event_type: str
    absolute_path: str
    repo_relative_path: str
    is_supported: bool
    old_absolute_path: str | None = None
    old_repo_relative_path: str | None = None
    repo_id: str | None = None


def normalize_event(
    raw_event: Any,
    repo_root: Path,
    config: AppConfig,
) -> list[FileChangeEvent]:
    """Normalize a raw watcher event into zero or more ``FileChangeEvent`` items.

    Move/rename events emit up to two events: a ``'deleted'`` event for ``src_path``
    (when inside the repository, supported, and non-ignored) and a ``'modified'``
    event for ``dest_path`` (when inside the repository, supported, non-ignored, and
    passing path/symlink safety checks).

    Args:
        raw_event: Watchdog event object (or compatible synthetic event).
        repo_root: Path to repository root.
        config: Application configuration with ignore and extension rules.

    Returns:
        List of normalized ``FileChangeEvent`` objects (empty if ignored/filtered).
    """
    raw_type = _get_raw_event_type(raw_event)
    if raw_type is None:
        return []

    raw_root = Path(repo_root)
    resolved_root = raw_root.resolve()
    ctx = _NormalizeContext(
        repo_id=derive_repo_id(resolved_root),
        repo_root=raw_root,
        resolved_root=resolved_root,
        ignored_dirs=config.ignored_dirs,
        test_dir_names=config.test_dir_names,
        include_tests=config.include_tests,
        supported_extensions=config.supported_extensions,
    )

    if raw_type == "moved":
        return _normalize_move_event(raw_event, ctx)

    raw_path = _get_event_path(raw_event)
    if raw_path is None:
        return []

    normalized = _normalize_single_path(
        ctx,
        raw_path=raw_path,
        event_type=raw_type,
        require_safe_existing_file=raw_type in ("created", "modified"),
    )
    return [normalized] if normalized is not None else []


def _normalize_move_event(
    raw_event: Any,
    ctx: _NormalizeContext,
) -> list[FileChangeEvent]:
    """Normalize a move/rename event into delete(src) and/or modify(dest) events."""
    events: list[FileChangeEvent] = []
    src_path = _get_event_path(raw_event)
    dest_path = _get_event_dest_path(raw_event)

    src_event: FileChangeEvent | None = None
    if src_path is not None:
        src_event = _normalize_single_path(
            ctx,
            raw_path=src_path,
            event_type="deleted",
            require_safe_existing_file=False,
        )
        if src_event is not None:
            events.append(src_event)

    if dest_path is not None:
        dest_event = _normalize_single_path(
            ctx,
            raw_path=dest_path,
            event_type="modified",
            require_safe_existing_file=True,
            old_absolute_path=src_event.absolute_path if src_event is not None else None,
            old_repo_relative_path=src_event.repo_relative_path if src_event is not None else None,
        )
        if dest_event is not None:
            events.append(dest_event)

    return events


def _normalize_single_path(
    ctx: _NormalizeContext,
    *,
    raw_path: str,
    event_type: str,
    require_safe_existing_file: bool,
    old_absolute_path: str | None = None,
    old_repo_relative_path: str | None = None,
) -> FileChangeEvent | None:
    """Resolve, validate, filter, and wrap one path into a ``FileChangeEvent``."""
    resolved = _resolve_candidate_in_repo(
        raw_root=ctx.repo_root,
        resolved_root=ctx.resolved_root,
        raw_path=raw_path,
        require_safe_existing_file=require_safe_existing_file,
    )
    if resolved is None:
        return None

    resolved_path, repo_relative_path = resolved
    if _is_ignored_path(
        repo_relative_path,
        ctx.ignored_dirs,
        include_tests=ctx.include_tests,
        test_dir_names=ctx.test_dir_names,
    ):
        return None

    if not _is_supported_file(repo_relative_path, ctx.supported_extensions):
        return None

    return FileChangeEvent(
        event_type=event_type,
        absolute_path=str(resolved_path),
        repo_relative_path=repo_relative_path,
        is_supported=True,
        old_absolute_path=old_absolute_path,
        old_repo_relative_path=old_repo_relative_path,
        repo_id=ctx.repo_id,
    )


def _resolve_confined_rel_path(
    candidate: Path,
    resolved_root: Path,
) -> tuple[Path, str] | None:
    """Resolve candidate path and compute non-empty POSIX relative path inside root."""
    try:
        resolved_candidate = candidate.resolve()
    except OSError:
        return None

    if not resolved_candidate.is_relative_to(resolved_root):
        return None

    rel_path = resolved_candidate.relative_to(resolved_root).as_posix()
    if not rel_path or rel_path == ".":
        return None

    return resolved_candidate, rel_path


def _resolve_candidate_in_repo(
    *,
    raw_root: Path,
    resolved_root: Path,
    raw_path: str,
    require_safe_existing_file: bool,
) -> tuple[Path, str] | None:
    """Resolve a candidate path inside ``resolved_root`` while blocking symlinks and escapes."""
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate

    # Check leaf symlink and intermediate symlink components below repo_root without
    # flagging system-level symlinks above repo_root (e.g., /tmp -> /private/tmp).
    if candidate.is_symlink() or _has_symlink_component_below_root(
        candidate, raw_root, resolved_root
    ):
        logger.debug("Ignoring symlink event path: %s", candidate)
        return None

    confined = _resolve_confined_rel_path(candidate, resolved_root)
    if confined is None:
        return None
    resolved_candidate, rel_path = confined

    if require_safe_existing_file and os.path.lexists(candidate):
        try:
            resolved_candidate = ensure_safe_repo_file(raw_root, candidate)
        except PathSecurityError as exc:
            logger.warning("Rejecting unsafe event target %s: %s", candidate, exc)
            return None

    return resolved_candidate, rel_path


def _find_root_base_for_candidate(
    candidate: Path,
    raw_root: Path,
    resolved_root: Path,
) -> Path | None:
    """Find matching root prefix for candidate (handling system symlinks above root)."""
    if candidate.is_relative_to(resolved_root):
        return resolved_root
    if candidate.is_relative_to(raw_root):
        return raw_root
    for parent in candidate.parents:
        with contextlib.suppress(OSError):
            if parent.resolve() == resolved_root:
                return parent
    return None


def _has_symlink_component_below_root(
    candidate: Path,
    raw_root: Path,
    resolved_root: Path,
) -> bool:
    """Return True if any path segment below ``repo_root`` is a symlink."""
    base_for_parts = _find_root_base_for_candidate(candidate, raw_root, resolved_root)
    if base_for_parts is None:
        return False

    current = base_for_parts
    for part in candidate.relative_to(base_for_parts).parts:
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def _infer_event_type_fallback(raw_event: Any) -> str | None:
    """Infer event type from boolean flags or event class name."""
    for attr, kind in _EVENT_BOOL_ATTRS:
        if getattr(raw_event, attr, False):
            return kind

    event_name = type(raw_event).__name__.lower()
    for keyword, kind in _EVENT_NAME_KEYWORDS:
        if keyword in event_name:
            return kind

    return None


def _get_raw_event_type(raw_event: Any) -> str | None:
    """Extract the raw event type string from a watcher event."""
    if getattr(raw_event, "is_directory", False):
        return None
    if getattr(raw_event, "is_move", False):
        return "moved"
    raw_event_type = getattr(raw_event, "event_type", None)
    if raw_event_type in ("created", "modified", "deleted", "moved"):
        return str(raw_event_type)
    return _infer_event_type_fallback(raw_event)


def _coerce_path_str(value: Any) -> str | None:
    """Convert raw string, bytes, or PathLike attribute into a string path."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return os.fsdecode(bytes(value))
    return os.fspath(value) if isinstance(value, Path) else str(value)


def _get_event_path(raw_event: Any) -> str | None:
    """Get the primary source path from a watcher event."""
    if hasattr(raw_event, "src_path"):
        return _coerce_path_str(raw_event.src_path)
    if hasattr(raw_event, "path"):
        return _coerce_path_str(raw_event.path)
    return None


def _get_event_dest_path(raw_event: Any) -> str | None:
    """Get the destination path from a move event."""
    if hasattr(raw_event, "dest_path"):
        return _coerce_path_str(raw_event.dest_path)
    return None


def _matches_any_pattern(segment: str, patterns: tuple[str, ...]) -> bool:
    """Return True if ``segment`` matches at least one fnmatch pattern."""
    return any(fnmatch.fnmatch(segment, pattern) for pattern in patterns)


def _is_ignored_path(
    repo_relative_path: str,
    ignored_dirs: tuple[str, ...],
    *,
    include_tests: bool = True,
    test_dir_names: tuple[str, ...] = (),
) -> bool:
    """Check whether a repo-relative path should be ignored.

    Uses ``fnmatch.fnmatch`` for directory pattern matching (consistent with
    ``parsing.scanner.should_ignore_dir``), skips test directories when
    ``include_tests`` is False, and ignores dotfiles and editor temp/swap files.
    """
    parts = [part for part in repo_relative_path.split("/") if part]
    if not parts:
        return True

    dir_parts, filename = parts[:-1], parts[-1]
    active_dir_patterns = ignored_dirs if include_tests else (*ignored_dirs, *test_dir_names)
    if any(_matches_any_pattern(part, active_dir_patterns) for part in dir_parts):
        return True

    # Also match exact single-segment ignored directory names passed directly in tests.
    if not dir_parts and _matches_any_pattern(filename, ignored_dirs):
        return True

    return filename.startswith(".") or filename.endswith(_IGNORED_TEMP_SUFFIXES)


def _is_supported_file(repo_relative_path: str, supported_extensions: tuple[str, ...]) -> bool:
    """Check if the file has a supported extension."""
    return any(repo_relative_path.endswith(ext) for ext in supported_extensions)
