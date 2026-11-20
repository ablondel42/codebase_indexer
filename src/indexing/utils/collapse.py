"""Pure stateless sequence-collapsing helpers for file change events."""

from collections import defaultdict
from collections.abc import Sequence

from core.models.repo import RepoRecord
from indexing.models.events import FileChangeEvent, FileEventType


def collapse_events(events: Sequence[FileChangeEvent]) -> list[FileChangeEvent]:
    """Collapse events per ``repo_relative_path`` preserving first-seen path order.

    Rules per path:
    - ``[created, deleted]`` (with no intermediate ``modified``) collapses to ``None``.
    - Final event ``deleted`` collapses to ``deleted``.
    - Prior ``deleted`` followed by ``created`` / ``modified`` collapses to ``modified``.
    - Initial ``created`` (with no ``deleted``) collapses to ``created``.
    - Otherwise collapses to the final event's ``event_type``.

    Args:
        events: Ordered sequence of normalized ``FileChangeEvent`` items.

    Returns:
        Collapsed list of net ``FileChangeEvent`` items.
    """
    by_path: dict[str, list[FileChangeEvent]] = defaultdict(list)
    for event in events:
        by_path[event.repo_relative_path].append(event)

    collapsed: list[FileChangeEvent] = []
    for path_events in by_path.values():
        final = collapse_path_events(path_events)
        if final is not None:
            collapsed.append(final)
    return collapsed


def collapse_path_events(events: Sequence[FileChangeEvent]) -> FileChangeEvent | None:
    """Collapse an ordered sequence of events targeting a single repository path.

    Args:
        events: Sequence of ``FileChangeEvent`` items sharing one ``repo_relative_path``.

    Returns:
        Net ``FileChangeEvent`` for the path, or ``None`` if an ephemeral create-then-delete
        sequence cancelled out completely.
    """
    if not events:
        return None

    types = [event.event_type for event in events]
    first_type = types[0]
    last_event = events[-1]
    last_type = last_event.event_type

    if (
        first_type == FileEventType.CREATED
        and last_type == FileEventType.DELETED
        and FileEventType.MODIFIED not in types
    ):
        return None

    if last_type == FileEventType.DELETED:
        effective_type = FileEventType.DELETED
    elif FileEventType.DELETED in types:
        effective_type = FileEventType.MODIFIED
    elif first_type == FileEventType.CREATED:
        effective_type = FileEventType.CREATED
    else:
        effective_type = last_type

    return FileChangeEvent(
        event_type=effective_type,
        absolute_path=last_event.absolute_path,
        repo_relative_path=last_event.repo_relative_path,
        is_supported=last_event.is_supported,
        old_absolute_path=last_event.old_absolute_path,
        old_repo_relative_path=last_event.old_repo_relative_path,
        repo_id=last_event.repo_id,
    )


def build_repo_upsert_record(
    existing: RepoRecord | None,
    candidate: RepoRecord,
    *,
    last_indexed_at: str | None = None,
) -> RepoRecord:
    """Merge ``candidate`` with ``existing`` preserving ``created_at`` and optional timestamp."""
    if existing is None:
        return candidate
    updates: dict[str, str | None] = {"created_at": existing.created_at}
    if last_indexed_at is not None:
        updates["last_indexed_at"] = last_indexed_at
    return candidate.model_copy(update=updates)


__all__ = [
    "build_repo_upsert_record",
    "collapse_events",
    "collapse_path_events",
]
