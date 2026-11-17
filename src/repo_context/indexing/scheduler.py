"""Event scheduler with debounce, sequence-aware collapsing, and serial worker execution."""

from collections import defaultdict
import queue
import threading
from typing import Callable

from repo_context.indexing.events import FileChangeEvent
from repo_context.logging_config import get_logger

logger = get_logger("indexing.scheduler")


class EventScheduler:
    """Debouncing and batching scheduler with serialized worker execution.

    Collects per-path event sequences within a debounce window, collapses each
    path's sequence into a single effective action, and dispatches batches one at
    a time on a dedicated background worker thread so callbacks never run
    concurrently against SQLite.
    """

    def __init__(
        self,
        debounce_ms: int = 500,
        on_batch_ready: Callable[[list[FileChangeEvent]], None] | None = None,
    ) -> None:
        """Initialize the scheduler.

        Args:
            debounce_ms: Debounce window in milliseconds (default 500ms).
            on_batch_ready: Callback invoked sequentially when a batch is ready.
        """
        self._debounce_sec = max(0.0, debounce_ms / 1000.0)
        self._on_batch_ready = on_batch_ready or self._default_batch_handler
        self._pending_events: dict[str, list[FileChangeEvent]] = defaultdict(list)
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._running = True
        self._stopped = False
        self._batch_queue: queue.Queue[list[FileChangeEvent] | None] = queue.Queue()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="repo-context-event-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def submit(self, event: FileChangeEvent) -> None:
        """Submit a normalized file change event into the current debounce window."""
        with self._lock:
            if not self._running:
                return

            self._pending_events[event.repo_relative_path].append(event)

            if self._timer is not None:
                self._timer.cancel()

            self._timer = threading.Timer(self._debounce_sec, self._emit_batch)
            self._timer.daemon = True
            self._timer.start()

    def stop(self) -> None:
        """Flush any pending events, stop the worker thread, and wait for completion."""
        with self._lock:
            if self._stopped:
                return
            self._running = False
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

            collapsed = self._drain_pending_locked()
            if collapsed:
                self._batch_queue.put(collapsed)
            self._batch_queue.put(None)

        if threading.current_thread() is not self._worker_thread:
            self._worker_thread.join()

    def _emit_batch(self) -> None:
        """Collapse pending events and enqueue them for sequential execution."""
        with self._lock:
            if not self._running:
                return
            self._timer = None
            collapsed = self._drain_pending_locked()
            if collapsed:
                self._batch_queue.put(collapsed)

    def _drain_pending_locked(self) -> list[FileChangeEvent]:
        """Drain and collapse all pending per-path event sequences (caller holds ``_lock``)."""
        if not self._pending_events:
            return []

        ordered_events: list[FileChangeEvent] = []
        for path_events in self._pending_events.values():
            ordered_events.extend(path_events)
        self._pending_events.clear()
        return _collapse_events(ordered_events)

    def _worker_loop(self) -> None:
        """Consume collapsed batches strictly one at a time."""
        while True:
            batch = self._batch_queue.get()
            try:
                if batch is None:
                    return
                self._on_batch_ready(batch)
            except Exception:
                logger.exception("Unhandled error in EventScheduler batch handler")
            finally:
                self._batch_queue.task_done()

    def _default_batch_handler(self, events: list[FileChangeEvent]) -> None:
        """Default batch handler (no-op if not overridden)."""
        return None


def collapse_events(events: list[FileChangeEvent]) -> list[FileChangeEvent]:
    """Collapse a list of events by repo-relative path using sequence semantics.

    Rules per path:
    - ``[created, deleted]`` (no intermediate modification) collapses to ``None``
    - ``[deleted, created]`` / ``[deleted, created, modified]`` collapses to ``'modified'``
    - Final event is ``'deleted'`` collapses to ``'deleted'``
    - ``[created, modified]`` (no prior delete) collapses to ``'created'``
    - Otherwise collapses to the final non-deleted event.
    """
    return _collapse_events(events)


def _collapse_events(events: list[FileChangeEvent]) -> list[FileChangeEvent]:
    """Group events by ``repo_relative_path`` preserving insertion order and collapse each."""
    by_path: dict[str, list[FileChangeEvent]] = defaultdict(list)
    for event in events:
        by_path[event.repo_relative_path].append(event)

    collapsed: list[FileChangeEvent] = []
    for path_events in by_path.values():
        final = _collapse_path_events(path_events)
        if final is not None:
            collapsed.append(final)

    return collapsed


def _collapse_path_events(events: list[FileChangeEvent]) -> FileChangeEvent | None:
    """Collapse a non-empty sequence of events for a single path."""
    if not events:
        return None

    types = [event.event_type for event in events]
    first_type = types[0]
    last_event = events[-1]
    last_type = last_event.event_type

    # Ephemeral create-then-delete with no intermediate modification disappears cleanly.
    if (
        first_type == "created"
        and last_type == "deleted"
        and "modified" not in types
        and all(t in ("created", "deleted") for t in types)
    ):
        return None

    if last_type == "deleted":
        effective_type = "deleted"
    elif "deleted" in types:
        # e.g., [deleted, created] or [deleted, created, modified] -> file was replaced
        effective_type = "modified"
    elif first_type == "created":
        # e.g., [created] or [created, modified] -> net creation within window
        effective_type = "created"
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
