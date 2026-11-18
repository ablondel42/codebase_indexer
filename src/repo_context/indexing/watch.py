"""Watch mode orchestration.

Sets up watchdog observer, routes raw events through normalization
and scheduler, and processes incremental updates sequentially.
"""

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Connection
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from repo_context.config import AppConfig, get_config
from repo_context.indexing.events import FileChangeEvent, normalize_event
from repo_context.indexing.incremental import process_event_batch
from repo_context.indexing.scheduler import EventScheduler
from repo_context.logging_config import get_logger
from repo_context.storage import close_connection, get_connection, initialize_database

logger = get_logger("indexing.watch")


class _RepoFileHandler(FileSystemEventHandler):
    """Watchdog event handler that routes normalized file events to the scheduler."""

    def __init__(
        self,
        repo_root: Path,
        config: AppConfig,
        scheduler: EventScheduler,
    ) -> None:
        super().__init__()
        self._repo_root = repo_root
        self._config = config
        self._scheduler = scheduler

    def on_created(self, event: Any) -> None:
        self._handle_event(event)

    def on_modified(self, event: Any) -> None:
        self._handle_event(event)

    def on_deleted(self, event: Any) -> None:
        self._handle_event(event)

    def on_moved(self, event: Any) -> None:
        self._handle_event(event)

    def _handle_event(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return

        normalized_events = normalize_event(event, self._repo_root, self._config)
        for normalized in normalized_events:
            self._scheduler.submit(normalized)


def make_watch_connection_factory(db_path: Path | str) -> Callable[[], Connection]:
    """Create a connection factory bound to ``db_path`` without re-initializing schema."""
    resolved_db_path = Path(db_path)

    def _open_connection() -> Connection:
        return get_connection(resolved_db_path)

    return _open_connection


def watch_repo(
    repo_root: Path,
    config: AppConfig | None = None,
    db_path: Path | str | None = None,
    debounce_ms: int = 500,
    verbose: bool = False,
) -> None:
    """Watch a repository for file changes and perform incremental updates.

    Args:
        repo_root: Path to repository root.
        config: Application configuration (uses default if None).
        db_path: Optional path to database file (overrides ``config.db_path``).
        debounce_ms: Debounce window in milliseconds.
        verbose: Enable verbose logging.

    Raises:
        FileNotFoundError: If ``repo_root`` does not exist.
        NotADirectoryError: If ``repo_root`` is not a directory.
    """
    repo_root = Path(repo_root).resolve()

    if not repo_root.exists():
        raise FileNotFoundError(f"Repository root not found: {repo_root}")
    if not repo_root.is_dir():
        raise NotADirectoryError(f"Not a directory: {repo_root}")

    cfg = config or get_config()
    resolved_db_path = Path(db_path or cfg.db_path)

    logger.info(
        "Starting watch mode",
        extra={
            "repo_root": str(repo_root),
            "db_path": str(resolved_db_path),
            "debounce_ms": debounce_ms,
        },
    )

    # Initialize database once at startup (never on every batch).
    init_conn = get_connection(resolved_db_path)
    try:
        initialize_database(init_conn)
    finally:
        close_connection(init_conn)

    bound_conn_factory = make_watch_connection_factory(resolved_db_path)

    def on_batch_ready(events: list[FileChangeEvent]) -> None:
        _process_watch_batch(bound_conn_factory, repo_root, events, cfg, verbose)

    scheduler = EventScheduler(
        debounce_ms=debounce_ms,
        on_batch_ready=on_batch_ready,
    )

    handler = _RepoFileHandler(repo_root, cfg, scheduler)
    observer = Observer()
    observer.schedule(handler, str(repo_root), recursive=True)
    observer.start()

    print(
        json.dumps(
            {
                "event": "watch_started",
                "repo_root": str(repo_root),
                "db_path": str(resolved_db_path),
                "debounce_ms": debounce_ms,
            },
            indent=2,
        ),
        flush=True,
    )

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "event": "watch_stopped",
                    "repo_root": str(repo_root),
                },
                indent=2,
            ),
            flush=True,
        )
    finally:
        observer.stop()
        observer.join()
        scheduler.stop()
        logger.info("Watch mode stopped")


def _log_watch_result(result: dict[str, Any], *, verbose: bool) -> None:
    """Emit structured log entry for a single processed watch event result."""
    status = result.get("status", "unknown")
    file_path = result.get("file_path", "unknown")

    if status == "reindexed" and verbose:
        logger.info(
            "File reindexed",
            extra={
                "file_path": file_path,
                "node_count": result.get("node_count", 0),
                "edge_count": result.get("edge_count", 0),
                "invalidated_refs": result.get("invalidated_reference_edge_count", 0),
            },
        )
    elif status == "deleted" and verbose:
        logger.info(
            "File deleted",
            extra={
                "file_path": file_path,
                "deleted_nodes": result.get("deleted_node_count", 0),
                "deleted_edges": result.get("deleted_edge_count", 0),
                "invalidated_symbols": result.get("invalidated_target_symbol_count", 0),
            },
        )
    elif status == "parse_failed":
        logger.warning("File parse failed during watch", extra={"file_path": file_path})
    elif status == "error":
        logger.error("File indexing error during watch", extra={"file_path": file_path})


def _process_watch_batch(
    conn_factory_func: Callable[[], Connection],
    repo_root: Path,
    events: list[FileChangeEvent],
    config: AppConfig,
    verbose: bool,
) -> None:
    """Process one collapsed batch of file change events."""
    conn = conn_factory_func()
    try:
        results = process_event_batch(conn, repo_root, events, config)
        for result in results:
            _log_watch_result(result, verbose=verbose)
            if result.get("status", "unknown") == "skipped" and not verbose:
                continue
            print(json.dumps({"event": "file_processed", **result}, indent=2), flush=True)
    finally:
        close_connection(conn)
