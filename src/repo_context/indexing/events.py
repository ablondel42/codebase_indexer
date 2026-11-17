"""File change event normalization.

Converts watcher-specific event objects into a stable internal event shape.
"""

from dataclasses import dataclass
import fnmatch
import os
from pathlib import Path
from typing import Any

from repo_context.config import AppConfig
from repo_context.exceptions import PathSecurityError
from repo_context.logging_config import get_logger
from repo_context.parsing.pathing import derive_repo_id, ensure_safe_repo_file

logger = get_logger("indexing.events")

_IGNORED_TEMP_SUFFIXES = ("~", ".swp", ".tmp")


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
    repo_id = derive_repo_id(resolved_root)

    if raw_type == "moved":
        return _normalize_move_event(raw_event, raw_root, resolved_root, repo_id, config)

    raw_path = _get_event_path(raw_event)
    if raw_path is None:
        return []

    check_regular = raw_type in ("created", "modified")
    normalized = _normalize_single_path(
        raw_path=raw_path,
        event_type=raw_type,
        raw_root=raw_root,
        resolved_root=resolved_root,
        repo_id=repo_id,
        config=config,
        require_safe_existing_file=check_regular,
    )
    return [normalized] if normalized is not None else []


def _normalize_move_event(
    raw_event: Any,
    raw_root: Path,
    resolved_root: Path,
    repo_id: str,
    config: AppConfig,
) -> list[FileChangeEvent]:
    """Normalize a move/rename event into delete(src) and/or modify(dest) events."""
    events: list[FileChangeEvent] = []
    src_path = _get_event_path(raw_event)
    dest_path = _get_event_dest_path(raw_event)

    src_event: FileChangeEvent | None = None
    if src_path is not None:
        src_event = _normalize_single_path(
            raw_path=src_path,
            event_type="deleted",
            raw_root=raw_root,
            resolved_root=resolved_root,
            repo_id=repo_id,
            config=config,
            require_safe_existing_file=False,
        )
        if src_event is not None:
            events.append(src_event)

    if dest_path is not None:
        dest_event = _normalize_single_path(
            raw_path=dest_path,
            event_type="modified",
            raw_root=raw_root,
            resolved_root=resolved_root,
            repo_id=repo_id,
            config=config,
            require_safe_existing_file=True,
            old_absolute_path=src_event.absolute_path if src_event is not None else None,
            old_repo_relative_path=src_event.repo_relative_path if src_event is not None else None,
        )
        if dest_event is not None:
            events.append(dest_event)

    return events


def _normalize_single_path(
    *,
    raw_path: str,
    event_type: str,
    raw_root: Path,
    resolved_root: Path,
    repo_id: str,
    config: AppConfig,
    require_safe_existing_file: bool,
    old_absolute_path: str | None = None,
    old_repo_relative_path: str | None = None,
) -> FileChangeEvent | None:
    """Resolve, validate, filter, and wrap one path into a ``FileChangeEvent``."""
    resolved = _resolve_candidate_in_repo(
        raw_root=raw_root,
        resolved_root=resolved_root,
        raw_path=raw_path,
        require_safe_existing_file=require_safe_existing_file,
    )
    if resolved is None:
        return None

    resolved_path, repo_relative_path = resolved
    if _is_ignored_path(
        repo_relative_path,
        config.ignored_dirs,
        include_tests=config.include_tests,
        test_dir_names=config.test_dir_names,
    ):
        return None

    if not _is_supported_file(repo_relative_path, config.supported_extensions):
        return None

    return FileChangeEvent(
        event_type=event_type,
        absolute_path=str(resolved_path),
        repo_relative_path=repo_relative_path,
        is_supported=True,
        old_absolute_path=old_absolute_path,
        old_repo_relative_path=old_repo_relative_path,
        repo_id=repo_id,
    )


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

    if candidate.is_symlink():
        logger.debug("Ignoring symlink event path: %s", candidate)
        return None

    # Check intermediate symlink components below the repository root without
    # flagging system-level symlinks above repo_root (e.g., /tmp -> /private/tmp).
    if _has_symlink_component_below_root(candidate, raw_root, resolved_root):
        logger.debug("Ignoring event under symlinked directory: %s", candidate)
        return None

    try:
        resolved_candidate = candidate.resolve()
    except OSError:
        return None

    if not resolved_candidate.is_relative_to(resolved_root):
        return None

    try:
        rel_path = resolved_candidate.relative_to(resolved_root).as_posix()
    except ValueError:
        return None

    if not rel_path or rel_path == ".":
        return None

    if require_safe_existing_file and os.path.lexists(candidate):
        try:
            resolved_candidate = ensure_safe_repo_file(raw_root, candidate)
        except PathSecurityError as exc:
            logger.warning("Rejecting unsafe event target %s: %s", candidate, exc)
            return None

    return resolved_candidate, rel_path


def _has_symlink_component_below_root(
    candidate: Path,
    raw_root: Path,
    resolved_root: Path,
) -> bool:
    """Return True if any path segment below ``repo_root`` is a symlink."""
    base_for_parts: Path | None = None
    if candidate.is_relative_to(resolved_root):
        base_for_parts = resolved_root
    elif candidate.is_relative_to(raw_root):
        base_for_parts = raw_root
    else:
        for parent in candidate.parents:
            try:
                if parent.resolve() == resolved_root:
                    base_for_parts = parent
                    break
            except OSError:
                continue

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


def _get_raw_event_type(raw_event: Any) -> str | None:
    """Extract the raw event type string from a watcher event."""
    if getattr(raw_event, "is_directory", False):
        return None

    if getattr(raw_event, "is_move", False):
        return "moved"

    raw_event_type = getattr(raw_event, "event_type", None)
    if raw_event_type in ("created", "modified", "deleted", "moved"):
        return str(raw_event_type)

    if getattr(raw_event, "is_created", False):
        return "created"
    if getattr(raw_event, "is_modified", False):
        return "modified"
    if getattr(raw_event, "is_deleted", False):
        return "deleted"

    event_name = type(raw_event).__name__.lower()
    if "moved" in event_name or "move" in event_name:
        return "moved"
    if "created" in event_name:
        return "created"
    if "modified" in event_name:
        return "modified"
    if "deleted" in event_name:
        return "deleted"

    return None


def _coerce_path_str(value: Any) -> str | None:
    """Convert raw string, bytes, or PathLike attribute into a string path."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return os.fsdecode(value)
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

    dir_parts = parts[:-1]
    filename = parts[-1]

    for part in dir_parts:
        if any(fnmatch.fnmatch(part, pattern) for pattern in ignored_dirs):
            return True
        if not include_tests and any(fnmatch.fnmatch(part, pattern) for pattern in test_dir_names):
            return True

    # Also match exact single-segment ignored directory names passed directly in tests.
    if not dir_parts and any(fnmatch.fnmatch(filename, pattern) for pattern in ignored_dirs):
        return True

    if filename.startswith(".") or filename.endswith(_IGNORED_TEMP_SUFFIXES):
        return True

    return False


def _is_supported_file(repo_relative_path: str, supported_extensions: tuple[str, ...]) -> bool:
    """Check if the file has a supported extension."""
    return any(repo_relative_path.endswith(ext) for ext in supported_extensions)
