"""Callback-driven filesystem watcher orchestrator service."""

import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from core.models.base import FrozenModel
from core.models.config import AppConfig
from core.ports.events import EventCallback
from core.ports.uow import UnitOfWorkFactoryProtocol
from core.utils.logging import get_logger
from core.utils.pathing import normalize_repo_root
from indexing.models.events import (
    FileChangeEvent,
    WatchFileProcessedEvent,
    WatchStartedEvent,
    WatchStoppedEvent,
)
from indexing.models.results import IncrementalFileResult, IncrementalStatus
from indexing.services.incremental import IncrementalIndexer
from indexing.services.normalizer import WatchEventNormalizer
from indexing.services.scheduler import EventScheduler

logger = get_logger("indexing.services.watcher")


class _WatchdogBridge(FileSystemEventHandler):
    """Watchdog ``FileSystemEventHandler`` routing normalized events to ``EventScheduler``."""

    def __init__(
        self,
        repo_root: Path,
        normalizer: WatchEventNormalizer,
        scheduler: EventScheduler,
    ) -> None:
        """Initialize the watchdog event bridge.

        Args:
            repo_root: Resolved repository root directory path.
            normalizer: ``WatchEventNormalizer`` instance.
            scheduler: Target ``EventScheduler`` receiving normalized events.
        """
        super().__init__()
        self._repo_root = repo_root
        self._normalizer = normalizer
        self._scheduler = scheduler

    def on_created(self, event: FileSystemEvent) -> None:
        """Forward file creation events into the normalizer and scheduler.

        Args:
            event: Raw watchdog creation event.
        """
        self._forward(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        """Forward file modification events into the normalizer and scheduler.

        Args:
            event: Raw watchdog modification event.
        """
        self._forward(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Forward file deletion events into the normalizer and scheduler.

        Args:
            event: Raw watchdog deletion event.
        """
        self._forward(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        """Forward file move/rename events into the normalizer and scheduler.

        Args:
            event: Raw watchdog move event.
        """
        self._forward(event)

    def _forward(self, event: FileSystemEvent) -> None:
        """Normalize ``event`` and submit resulting ``FileChangeEvent`` items.

        Args:
            event: Raw watchdog filesystem event.
        """
        if event.is_directory:
            return
        for normalized in self._normalizer.normalize(event, self._repo_root):
            self._scheduler.submit(normalized)


class RepoWatcher:
    """Watches a repository directory and applies debounced incremental updates."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        normalizer: WatchEventNormalizer | None = None,
        incremental: IncrementalIndexer | None = None,
        observer_factory: Callable[[], BaseObserver] | None = None,
    ) -> None:
        """Initialize the repository watcher and its collaborating services.

        Args:
            config: Optional ``AppConfig`` supplying ignore and extension rules.
            normalizer: Optional injected ``WatchEventNormalizer`` instance.
            incremental: Optional injected ``IncrementalIndexer`` instance.
            observer_factory: Optional zero-argument factory returning a ``BaseObserver``.
        """
        resolved_config = config if config is not None else AppConfig()
        self._config = resolved_config
        self._normalizer = (
            normalizer
            if normalizer is not None
            else WatchEventNormalizer(config=resolved_config)
        )
        self._incremental = (
            incremental
            if incremental is not None
            else IncrementalIndexer(config=resolved_config)
        )
        self._observer_factory = observer_factory if observer_factory is not None else Observer

    @staticmethod
    def _emit_event(on_event: EventCallback | None, payload: FrozenModel) -> None:
        """Invoke ``on_event`` with ``payload`` when a callback was supplied.

        Args:
            on_event: Optional ``EventCallback`` consumer.
            payload: Immutable domain event model to emit.
        """
        if on_event is not None:
            on_event(payload)

    @staticmethod
    def _log_file_result(result: IncrementalFileResult, *, verbose: bool) -> None:
        """Emit structured ``stderr`` log entries for a processed watch file result.

        Args:
            result: Per-file incremental processing outcome.
            verbose: Whether verbose ``INFO`` logs are enabled for normal updates.
        """
        if result.status == IncrementalStatus.REINDEXED and verbose:
            logger.info(
                "Watch reindexed %s (nodes=%d, edges=%d, invalidated_refs=%d)",
                result.file_path,
                result.node_count,
                result.edge_count,
                result.invalidated_reference_edge_count,
            )
        elif result.status == IncrementalStatus.DELETED and verbose:
            logger.info(
                "Watch deleted %s (deleted_nodes=%d, deleted_edges=%d, reset_edges=%d)",
                result.file_path,
                result.deleted_node_count,
                result.deleted_edge_count,
                result.reset_edge_count,
            )
        elif result.status == IncrementalStatus.PARSE_FAILED:
            logger.warning("Watch parse failed for %s: %s", result.file_path, result.error_message)
        elif result.status == IncrementalStatus.ERROR:
            logger.error("Watch indexing error for %s: %s", result.file_path, result.error_message)

    def _handle_batch(
        self,
        uow_factory: UnitOfWorkFactoryProtocol,
        resolved_root: Path,
        events: list[FileChangeEvent],
        *,
        verbose: bool,
        on_event: EventCallback | None,
    ) -> None:
        """Process one debounced batch inside a fresh UoW and emit file events.

        Args:
            uow_factory: ``UnitOfWorkFactoryProtocol`` bound to the active database.
            resolved_root: Resolved repository root directory path.
            events: Collapsed ``FileChangeEvent`` batch from ``EventScheduler``.
            verbose: Whether ``SKIPPED`` files should also emit callback events.
            on_event: Optional ``EventCallback`` receiving ``WatchFileProcessedEvent`` items.
        """
        with uow_factory.open() as uow:
            batch_result = self._incremental.process_batch(uow, resolved_root, events)

        for item in batch_result.results:
            self._log_file_result(item, verbose=verbose)
            if item.status == IncrementalStatus.SKIPPED and not verbose:
                continue
            self._emit_event(on_event, WatchFileProcessedEvent.from_file_result(item))

    @staticmethod
    def _wait_until_stopped(stop_event: threading.Event | None) -> None:
        """Block in bounded sleep intervals until ``stop_event`` is set or interrupted.

        Args:
            stop_event: Optional external ``threading.Event`` signaling shutdown.
        """
        if stop_event is not None:
            while not stop_event.wait(timeout=0.2):
                continue
            return

        while True:
            time.sleep(0.5)

    @staticmethod
    def _teardown_watch(observer: BaseObserver, scheduler: EventScheduler) -> None:
        """Stop and join ``observer``, then flush and stop ``scheduler``.

        Args:
            observer: Active or unstarted watchdog ``BaseObserver``.
            scheduler: Active ``EventScheduler`` instance.
        """
        try:
            observer.stop()
            if observer.is_alive():
                observer.join(timeout=5.0)
        except (OSError, RuntimeError) as exc:
            logger.warning("Error stopping watchdog observer: %s", exc)
        finally:
            scheduler.stop()

    def watch(
        self,
        uow_factory: UnitOfWorkFactoryProtocol,
        repo_root: Path | str,
        *,
        debounce_ms: int = 500,
        verbose: bool = False,
        on_event: EventCallback | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Watch ``repo_root`` continuously and apply incremental graph updates.

        Initializes the database once on startup, emits ``WatchStartedEvent``,
        ``WatchFileProcessedEvent``, and ``WatchStoppedEvent`` via ``on_event``,
        and guarantees observer + scheduler teardown on exit.

        Args:
            uow_factory: ``UnitOfWorkFactoryProtocol`` managing SQLite sessions.
            repo_root: Path to the repository root directory to watch.
            debounce_ms: Quiet-period debounce window in milliseconds (default ``500``).
            verbose: When ``True``, emits ``SKIPPED`` file events and ``INFO`` logs.
            on_event: Optional ``EventCallback`` receiving typed watch events.
            stop_event: Optional ``threading.Event`` allowing programmatic shutdown.

        Raises:
            FileNotFoundError: If ``repo_root`` does not exist on disk.
            NotADirectoryError: If ``repo_root`` is not a directory.
        """
        resolved_root = normalize_repo_root(repo_root)
        logger.info(
            "Starting watch mode on %s (db=%s, debounce_ms=%d)",
            resolved_root,
            uow_factory.db_path,
            debounce_ms,
        )

        with uow_factory.open(initialize=True):
            pass

        scheduler = EventScheduler(
            debounce_ms=debounce_ms,
            on_batch_ready=lambda batch: self._handle_batch(
                uow_factory,
                resolved_root,
                batch,
                verbose=verbose,
                on_event=on_event,
            ),
        )
        observer = self._observer_factory()
        bridge = _WatchdogBridge(resolved_root, self._normalizer, scheduler)
        stopped_cleanly = False

        try:
            observer.schedule(bridge, str(resolved_root), recursive=True)
            observer.start()
            self._emit_event(
                on_event,
                WatchStartedEvent(
                    repo_root=str(resolved_root),
                    db_path=str(uow_factory.db_path),
                    debounce_ms=debounce_ms,
                ),
            )
            self._wait_until_stopped(stop_event)
            stopped_cleanly = True
        except KeyboardInterrupt:
            logger.info("Watch mode interrupted by KeyboardInterrupt on %s", resolved_root)
            stopped_cleanly = True
        finally:
            self._teardown_watch(observer, scheduler)
            if stopped_cleanly:
                self._emit_event(on_event, WatchStoppedEvent(repo_root=str(resolved_root)))
            logger.info("Watch mode stopped for %s", resolved_root)


__all__ = [
    "RepoWatcher",
]
