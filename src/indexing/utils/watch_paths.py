"""Stateless path-confinement, symlink, and watchdog attribute helpers."""

import os
from pathlib import Path
from typing import Any

from core.models.exceptions import PathSecurityError
from core.utils.logging import get_logger
from core.utils.pathing import ensure_safe_repo_file
from indexing.models.events import NormalizeContext
from parsing.utils.filters import should_ignore_dir

logger = get_logger("indexing.utils.watch_paths")

_IGNORED_TEMP_SUFFIXES: tuple[str, ...] = ("~", ".swp", ".tmp")
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


def is_ignored_watch_path(
    repo_relative_path: str,
    ignored_dirs: tuple[str, ...],
    *,
    include_tests: bool = True,
    test_dir_names: tuple[str, ...] = (),
) -> bool:
    """Determine whether a repository-relative POSIX path should be ignored during watch.

    Matches directory components against ``ignored_dirs`` (and ``test_dir_names`` when
    ``include_tests=False``), and filters dotfiles plus editor swap/temp suffixes.

    Args:
        repo_relative_path: Repository-relative POSIX path string.
        ignored_dirs: Directory names or ``fnmatch`` patterns to ignore.
        include_tests: Whether files inside test directories are permitted.
        test_dir_names: Directory names or patterns identifying test trees.

    Returns:
        ``True`` when the path should be ignored; ``False`` otherwise.
    """
    parts = [part for part in repo_relative_path.split("/") if part]
    if not parts:
        return True

    dir_parts, filename = parts[:-1], parts[-1]
    active_patterns = ignored_dirs if include_tests else (*ignored_dirs, *test_dir_names)
    if any(should_ignore_dir(part, active_patterns) for part in dir_parts):
        return True

    if not dir_parts and should_ignore_dir(filename, ignored_dirs):
        return True

    return filename.startswith(".") or filename.endswith(_IGNORED_TEMP_SUFFIXES)


def is_supported_watch_file(
    repo_relative_path: str,
    supported_extensions: tuple[str, ...],
) -> bool:
    """Check whether ``repo_relative_path`` ends with a supported source file suffix.

    Args:
        repo_relative_path: Repository-relative POSIX file path.
        supported_extensions: Allowed file suffixes including leading dots (e.g. ``(".py",)``).

    Returns:
        ``True`` if ``repo_relative_path`` matches at least one supported extension.
    """
    return any(repo_relative_path.endswith(ext) for ext in supported_extensions)


def _find_root_base_for_candidate(
    candidate: Path,
    raw_root: Path,
    resolved_root: Path,
) -> Path | None:
    """Locate the ancestor ``Path`` matching ``repo_root`` without flagging OS symlinks above root.

    Args:
        candidate: Absolute candidate path under inspection.
        raw_root: Raw ``repo_root`` path as supplied by the caller.
        resolved_root: Resolved ``repo_root`` directory path.

    Returns:
        Matching ancestor ``Path`` for relative segment walking, or ``None``.
    """
    if candidate.is_relative_to(resolved_root):
        return resolved_root
    if candidate.is_relative_to(raw_root):
        return raw_root
    for parent in candidate.parents:
        try:
            if parent.resolve() == resolved_root:
                return parent
        except OSError as exc:
            logger.debug(
                "Skipping unresolvable ancestor %s while matching root %s: %s",
                parent,
                resolved_root,
                exc,
            )
    return None


def has_symlink_below_root(
    candidate: Path,
    raw_root: Path,
    resolved_root: Path,
) -> bool:
    """Return ``True`` if any path segment of ``candidate`` below ``repo_root`` is a symlink.

    Args:
        candidate: Absolute candidate path being checked.
        raw_root: Raw ``repo_root`` path supplied by the caller.
        resolved_root: Resolved ``repo_root`` path.

    Returns:
        ``True`` if a symlink component exists below ``repo_root``, else ``False``.
    """
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


def _coerce_path_str(value: Any) -> str | None:
    """Decode raw ``str``, ``bytes``, or ``os.PathLike`` event attributes into a ``str``.

    Args:
        value: Raw attribute value extracted from a watcher event.

    Returns:
        Decoded filesystem path string, or ``None`` if ``value`` is ``None``.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return os.fsdecode(bytes(value))
    if isinstance(value, os.PathLike):
        raw_fspath = os.fspath(value)
        return os.fsdecode(raw_fspath) if isinstance(raw_fspath, bytes) else raw_fspath
    return str(value)


def _infer_event_type_fallback(raw_event: Any) -> str | None:
    """Infer a raw event type from boolean flags or the event class name.

    Args:
        raw_event: Arbitrary watcher or synthetic event instance.

    Returns:
        One of ``"moved"``, ``"created"``, ``"modified"``, ``"deleted"``, or ``None``.
    """
    for attr, kind in _EVENT_BOOL_ATTRS:
        if getattr(raw_event, attr, False):
            return kind

    event_name = type(raw_event).__name__.lower()
    for keyword, kind in _EVENT_NAME_KEYWORDS:
        if keyword in event_name:
            return kind
    return None


def _extract_raw_event_type(raw_event: Any) -> str | None:
    """Extract a normalized raw event kind string from a filesystem event object.

    Args:
        raw_event: Watchdog ``FileSystemEvent`` or compatible synthetic event object.

    Returns:
        ``"created"``, ``"modified"``, ``"deleted"``, ``"moved"``, or ``None`` if ignored.
    """
    if getattr(raw_event, "is_directory", False):
        return None
    if getattr(raw_event, "is_move", False):
        return "moved"
    raw_type = getattr(raw_event, "event_type", None)
    if raw_type in ("created", "modified", "deleted", "moved"):
        return str(raw_type)
    return _infer_event_type_fallback(raw_event)


def extract_watchdog_paths(raw_event: Any) -> tuple[str | None, str | None, str | None]:
    """Extract ``(raw_event_type, src_path, dest_path)`` from a raw watcher event.

    Args:
        raw_event: Watchdog ``FileSystemEvent`` or compatible synthetic event object.

    Returns:
        Tuple ``(raw_type, src_path, dest_path)`` where ``raw_type`` is ``None`` for
        directory or unrecognized events.
    """
    raw_type = _extract_raw_event_type(raw_event)
    if raw_type is None:
        return None, None, None

    if hasattr(raw_event, "src_path"):
        src_path = _coerce_path_str(raw_event.src_path)
    elif hasattr(raw_event, "path"):
        src_path = _coerce_path_str(raw_event.path)
    else:
        src_path = None

    dest_path = (
        _coerce_path_str(raw_event.dest_path) if hasattr(raw_event, "dest_path") else None
    )
    return raw_type, src_path, dest_path


def _resolve_confined_rel_path(
    candidate: Path,
    resolved_root: Path,
) -> tuple[Path, str] | None:
    """Resolve ``candidate`` and compute its non-empty POSIX path inside ``resolved_root``.

    Args:
        candidate: Absolute candidate path.
        resolved_root: Resolved repository root directory path.

    Returns:
        Tuple ``(resolved_candidate, repo_relative_posix_path)``, or ``None`` if outside root.
    """
    try:
        resolved_candidate = candidate.resolve()
    except OSError as exc:
        logger.debug("Cannot resolve watch candidate path %s: %s", candidate, exc)
        return None

    if not resolved_candidate.is_relative_to(resolved_root):
        logger.debug(
            "Ignoring watch path escaping repository root %s: %s",
            resolved_root,
            resolved_candidate,
        )
        return None

    try:
        rel_path = resolved_candidate.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        logger.debug(
            "Watch candidate %s is not relative to %s: %s",
            resolved_candidate,
            resolved_root,
            exc,
        )
        return None

    if not rel_path or rel_path == ".":
        return None
    return resolved_candidate, rel_path


def resolve_watch_candidate(
    ctx: NormalizeContext,
    raw_path: str,
    *,
    require_safe_existing_file: bool,
) -> tuple[Path, str] | None:
    """Resolve and validate a raw watcher path inside ``ctx.resolved_root``.

    Blocks leaf and intermediate symlinks below ``repo_root``, paths escaping
    ``resolved_root``, and non-regular files when ``require_safe_existing_file=True``
    and the candidate exists on disk.

    Args:
        ctx: Active ``NormalizeContext`` for the watched repository.
        raw_path: Raw filesystem path string from the watcher event.
        require_safe_existing_file: Whether existing paths must pass regular-file safety.

    Returns:
        Tuple ``(resolved_path, repo_relative_path)`` or ``None`` if rejected.
    """
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = ctx.resolved_root / candidate

    if candidate.is_symlink() or has_symlink_below_root(
        candidate,
        ctx.repo_root,
        ctx.resolved_root,
    ):
        logger.debug("Ignoring symlink watch event path: %s", candidate)
        return None

    confined = _resolve_confined_rel_path(candidate, ctx.resolved_root)
    if confined is None:
        return None
    resolved_candidate, rel_path = confined

    if require_safe_existing_file and os.path.lexists(candidate):
        try:
            resolved_candidate = ensure_safe_repo_file(ctx.repo_root, candidate)
        except PathSecurityError as exc:
            logger.warning("Rejecting unsafe watch event target %s: %s", candidate, exc)
            return None

    return resolved_candidate, rel_path


__all__ = [
    "extract_watchdog_paths",
    "has_symlink_below_root",
    "is_ignored_watch_path",
    "is_supported_watch_file",
    "resolve_watch_candidate",
]
