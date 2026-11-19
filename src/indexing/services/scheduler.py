"""Thread-safe debouncing event scheduler and serialized worker queue."""

import queue
import threading
from collections import defaultdict
from collections.abc import Callable
from types import TracebackType
from typing import Self

from core.utils.logging import get_logger
from indexing.models.events import FileChangeEvent
from indexing.utils.collapse import collapse_events

logger = get_logger("indexing.services.scheduler")


class EventScheduler:
    """Debounces ``FileChangeEvent`` streams and dispatches batches on one worker thread."""

    def __init__(
        self,
        debounce_ms: int = 500,
        on_batch_ready: Callable[[list[FileChangeEvent]], None] | None = None,
    ) -> None:
        """Initialize the debouncer and start the background batch worker thread.

        Args:
            debounce_ms: Quiet-period debounce window in milliseconds (default ``500``).
            on_batch_ready: Optional callback invoked sequentially for each collapsed batch.
        """
        self._debounce_sec = max(0.0, debounce_ms / 1000.0)
        self._on_batch_ready = (
            on_batch_ready if on_batch_ready is not None else self._noop_batch_handler
        )
        self._pending_events: dict[str, list[FileChangeEvent]] = defaultdict(list)
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._running = True
        self._stopped = False
        self._batch_queue: queue.Queue[list[FileChangeEvent] | None] = queue.Queue()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="indexer-event-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def submit(self, event: FileChangeEvent) -> None:
        """Submit a normalized ``FileChangeEvent`` into the active debounce window.

        Args:
            event: Normalized file change event to accumulate and debounce.
        """
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
        """Flush any pending debounced events and join the background worker thread."""
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

    def __enter__(self) -> Self:
        """Enter the scheduler context manager.

        Returns:
            Active ``EventScheduler`` instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the scheduler context manager, flushing and stopping the worker.

        Args:
            exc_type: Exception class if raised inside the ``with`` block, else ``None``.
            exc_val: Exception instance if raised inside the ``with`` block, else ``None``.
            exc_tb: Traceback if an exception was raised, else ``None``.
        """
        _ = (exc_type, exc_val, exc_tb)
        self.stop()

    def _emit_batch(self) -> None:
        """Collapse accumulated pending events and enqueue them for worker execution."""
        with self._lock:
            if not self._running:
                return
            self._timer = None
            collapsed = self._drain_pending_locked()
            if collapsed:
                self._batch_queue.put(collapsed)

    def _drain_pending_locked(self) -> list[FileChangeEvent]:
        """Drain and collapse all pending per-path event sequences while holding ``_lock``.

        Returns:
            Collapsed list of net ``FileChangeEvent`` items.
        """
        if not self._pending_events:
            return []

        ordered_events: list[FileChangeEvent] = []
        for path_events in self._pending_events.values():
            ordered_events.extend(path_events)
        self._pending_events.clear()
        return collapse_events(ordered_events)

    def _worker_loop(self) -> None:
        """Consume collapsed batches sequentially until the ``None`` sentinel arrives."""
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

    @staticmethod
    def _noop_batch_handler(events: list[FileChangeEvent]) -> None:
        """No-op fallback batch handler used when ``on_batch_ready`` is omitted.

        Args:
            events: Collapsed batch of ``FileChangeEvent`` items.
        """
        _ = events


__all__ = [
    "EventScheduler",
]
