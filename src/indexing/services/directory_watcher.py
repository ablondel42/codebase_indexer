"""Multi-directory ``watchdog`` observer adapter implementing ``DirectoryWatchHandleProtocol``."""

import os
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from core.ports.watch import (
    DirectoryWatchHandleProtocol,
    PathChangeCallback,
    WatchEventKind,
)
from core.utils.logging import get_logger

logger = get_logger("indexing.services.directory_watcher")


class _RawPathBridge(FileSystemEventHandler):
    """Forwards raw ``watchdog`` path events to a normalized ``PathChangeCallback``."""

    def __init__(self, on_change: PathChangeCallback) -> None:
        """Initialize the bridge bound to ``on_change``."""
        super().__init__()
        self._on_change = on_change

    def _emit(
        self,
        kind: WatchEventKind,
        raw_path: bytes | str,
        *,
        is_directory: bool,
    ) -> None:
        """Decode ``raw_path`` and forward event to ``on_change``."""
        text_path = os.fsdecode(raw_path)
        if not text_path:
            return
        self._on_change(kind, Path(text_path), is_directory)

    def on_created(self, event: FileSystemEvent) -> None:
        """Emit creation event."""
        self._emit("created", event.src_path, is_directory=bool(event.is_directory))

    def on_modified(self, event: FileSystemEvent) -> None:
        """Emit modification event."""
        self._emit("modified", event.src_path, is_directory=bool(event.is_directory))

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Emit deletion event."""
        self._emit("deleted", event.src_path, is_directory=bool(event.is_directory))

    def on_moved(self, event: FileSystemEvent) -> None:
        """Emit deletion for source path and move arrival for destination path."""
        is_dir = bool(event.is_directory)
        self._emit("deleted", event.src_path, is_directory=is_dir)
        if isinstance(event, FileSystemMovedEvent):
            self._emit("moved", event.dest_path, is_directory=is_dir)


class DirectoryWatchHandle:
    """Thread-safe controller supporting dynamic directory registration and shutdown."""

    def __init__(self, observer: BaseObserver, bridge: _RawPathBridge) -> None:
        """Bind handle to an active ``BaseObserver`` and ``_RawPathBridge``."""
        self._observer = observer
        self._bridge = bridge
        self._lock = threading.RLock()
        self._watched: set[tuple[Path, bool]] = set()
        self._stopped = False

    @staticmethod
    def _resolve_watch_dir(path: Path) -> Path | None:
        """Resolve ``path`` and confirm it is an existing non-file directory."""
        try:
            resolved = path.resolve()
        except OSError as exc:
            logger.debug("Skipping unresolvable watch directory %s: %s", path, exc)
            return None
        return resolved if resolved.is_dir() else None

    def add_watch(self, path: Path, *, recursive: bool = False) -> bool:
        """Schedule watching ``path`` if it exists and is not yet covered."""
        resolved = self._resolve_watch_dir(path)
        if resolved is None:
            return False
        with self._lock:
            key = (resolved, recursive)
            already_covered = key in self._watched or (
                not recursive and (resolved, True) in self._watched
            )
            if self._stopped or already_covered:
                return False
            try:
                self._observer.schedule(self._bridge, str(resolved), recursive=recursive)
            except (OSError, RuntimeError) as exc:
                logger.warning("Failed scheduling watch on %s: %s", resolved, exc)
                return False
            self._watched.add(key)
            return True

    def stop(self) -> None:
        """Stop and join the underlying filesystem observer thread."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        try:
            self._observer.stop()
            if self._observer.is_alive():
                self._observer.join(timeout=5.0)
        except (OSError, RuntimeError) as exc:
            logger.warning("Error stopping directory watcher observer: %s", exc)


class DirectoryTreeWatcher:
    """Factory service launching ``DirectoryWatchHandle`` instances over arbitrary trees."""

    def __init__(
        self,
        *,
        observer_factory: Callable[[], BaseObserver] | None = None,
    ) -> None:
        """Initialize the directory watcher factory."""
        self._observer_factory = observer_factory if observer_factory is not None else Observer

    def start(
        self,
        watches: Sequence[tuple[Path, bool]],
        on_change: PathChangeCallback,
    ) -> DirectoryWatchHandleProtocol:
        """Start watching initial ``watches`` pairs and return the handle."""
        observer = self._observer_factory()
        bridge = _RawPathBridge(on_change)
        handle = DirectoryWatchHandle(observer, bridge)
        for watch_path, recursive in watches:
            handle.add_watch(watch_path, recursive=recursive)
        try:
            observer.start()
        except (OSError, RuntimeError) as exc:
            logger.warning("Failed starting directory watcher observer: %s", exc)
            handle.stop()
            raise
        return handle


__all__ = [
    "DirectoryTreeWatcher",
    "DirectoryWatchHandle",
]
