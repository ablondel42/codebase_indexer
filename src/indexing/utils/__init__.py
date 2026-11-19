"""Pure stateless path, symlink, and event-collapsing utilities for ``indexing``."""

from indexing.utils.collapse import collapse_events, collapse_path_events
from indexing.utils.watch_paths import (
    extract_watchdog_paths,
    has_symlink_below_root,
    is_ignored_watch_path,
    is_supported_watch_file,
    resolve_watch_candidate,
)

__all__ = [
    "collapse_events",
    "collapse_path_events",
    "extract_watchdog_paths",
    "has_symlink_below_root",
    "is_ignored_watch_path",
    "is_supported_watch_file",
    "resolve_watch_candidate",
]
