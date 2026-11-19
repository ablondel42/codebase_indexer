"""Watcher filesystem event normalization service."""

from pathlib import Path
from typing import Any

from core.models.config import AppConfig
from core.utils.logging import get_logger
from indexing.models.events import FileChangeEvent, FileEventType, NormalizeContext
from indexing.utils.watch_paths import (
    extract_watchdog_paths,
    is_ignored_watch_path,
    is_supported_watch_file,
    resolve_watch_candidate,
)

logger = get_logger("indexing.services.normalizer")


class WatchEventNormalizer:
    """Normalizes raw ``watchdog`` events into confined ``FileChangeEvent`` models."""

    def __init__(self, config: AppConfig | None = None) -> None:
        """Initialize the normalizer with optional application configuration.

        Args:
            config: Optional ``AppConfig`` supplying ignore globs, test filtering,
                and supported extensions.
        """
        self._config = config if config is not None else AppConfig()

    @property
    def config(self) -> AppConfig:
        """Return the ``AppConfig`` active on this normalizer.

        Returns:
            Configured ``AppConfig`` instance.
        """
        return self._config

    @staticmethod
    def _normalize_single_path(
        ctx: NormalizeContext,
        raw_path: str,
        event_type: FileEventType,
        *,
        require_safe_existing_file: bool,
        old_source: FileChangeEvent | None = None,
    ) -> FileChangeEvent | None:
        """Resolve, filter, and wrap one candidate path into a ``FileChangeEvent``.

        Args:
            ctx: Repository normalization context.
            raw_path: Raw event path string.
            event_type: Target ``FileEventType`` to emit.
            require_safe_existing_file: Whether existing paths must pass regular-file checks.
            old_source: Optional source ``FileChangeEvent`` for move/rename destinations.

        Returns:
            Normalized ``FileChangeEvent``, or ``None`` if ignored, unsupported, or unsafe.
        """
        resolved = resolve_watch_candidate(
            ctx,
            raw_path,
            require_safe_existing_file=require_safe_existing_file,
        )
        if resolved is None:
            return None

        resolved_path, repo_relative_path = resolved
        if is_ignored_watch_path(
            repo_relative_path,
            ctx.ignored_dirs,
            include_tests=ctx.include_tests,
            test_dir_names=ctx.test_dir_names,
        ):
            return None

        if not is_supported_watch_file(repo_relative_path, ctx.supported_extensions):
            return None

        return FileChangeEvent(
            event_type=event_type,
            absolute_path=str(resolved_path),
            repo_relative_path=repo_relative_path,
            is_supported=True,
            old_absolute_path=old_source.absolute_path if old_source is not None else None,
            old_repo_relative_path=(
                old_source.repo_relative_path if old_source is not None else None
            ),
            repo_id=ctx.repo_id,
        )

    def _normalize_move_event(
        self,
        ctx: NormalizeContext,
        src_path: str | None,
        dest_path: str | None,
    ) -> list[FileChangeEvent]:
        """Normalize a move/rename event into ``[deleted(src), modified(dest)]`` items.

        Args:
            ctx: Repository normalization context.
            src_path: Source path prior to the move, if present.
            dest_path: Destination path after the move, if present.

        Returns:
            Zero, one, or two normalized ``FileChangeEvent`` items.
        """
        events: list[FileChangeEvent] = []
        src_event: FileChangeEvent | None = None

        if src_path is not None:
            src_event = self._normalize_single_path(
                ctx,
                src_path,
                FileEventType.DELETED,
                require_safe_existing_file=False,
            )
            if src_event is not None:
                events.append(src_event)

        if dest_path is not None:
            dest_event = self._normalize_single_path(
                ctx,
                dest_path,
                FileEventType.MODIFIED,
                require_safe_existing_file=True,
                old_source=src_event,
            )
            if dest_event is not None:
                events.append(dest_event)

        return events

    def normalize(self, raw_event: Any, repo_root: Path | str) -> list[FileChangeEvent]:
        """Normalize a raw filesystem event into zero or more ``FileChangeEvent`` models.

        Args:
            raw_event: Watchdog ``FileSystemEvent`` (or compatible synthetic event).
            repo_root: Watched repository root directory path.

        Returns:
            Ordered list of confined, supported ``FileChangeEvent`` models.
        """
        raw_type, src_path, dest_path = extract_watchdog_paths(raw_event)
        if raw_type is None:
            return []

        ctx = NormalizeContext.from_config(Path(repo_root), self._config)
        if raw_type == "moved":
            events = self._normalize_move_event(ctx, src_path, dest_path)
            logger.debug("Normalized moved event into %d change(s)", len(events))
            return events

        if src_path is None:
            return []

        event_type = FileEventType(raw_type)
        normalized = self._normalize_single_path(
            ctx,
            src_path,
            event_type,
            require_safe_existing_file=event_type
            in (FileEventType.CREATED, FileEventType.MODIFIED),
        )
        if normalized is None:
            return []
        logger.debug("Normalized %s event for %s", event_type, normalized.repo_relative_path)
        return [normalized]


__all__ = [
    "WatchEventNormalizer",
]
