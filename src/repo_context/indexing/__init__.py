"""Indexing package for watch mode and incremental updates.

Exposes event normalization, debounced scheduling, invalidation,
incremental file reindexing/deletion, and watch mode orchestration.
"""

from repo_context.indexing.events import FileChangeEvent, normalize_event
from repo_context.indexing.incremental import (
    handle_deleted_file,
    process_event_batch,
    reindex_changed_file,
)
from repo_context.indexing.invalidation import (
    collect_impacted_symbol_ids,
    invalidate_reference_summaries_for_file,
    mark_symbol_ids_stale,
    mark_symbols_in_file_stale,
)
from repo_context.indexing.scheduler import EventScheduler, collapse_events
from repo_context.indexing.watch import make_watch_connection_factory, watch_repo

__all__ = [
    "FileChangeEvent",
    "normalize_event",
    "EventScheduler",
    "collapse_events",
    "mark_symbol_ids_stale",
    "mark_symbols_in_file_stale",
    "invalidate_reference_summaries_for_file",
    "collect_impacted_symbol_ids",
    "reindex_changed_file",
    "handle_deleted_file",
    "process_event_batch",
    "make_watch_connection_factory",
    "watch_repo",
]
