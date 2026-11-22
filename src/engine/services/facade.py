"""Headless and UI-ready ``IndexerEngine`` application facade."""

import threading
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

from context.services.builder import SymbolContextBuilder
from context.services.code_search import CodeSearchService
from context.services.outline import OutlineBuilder
from context.services.source import SourceReader
from core.models.config import AppConfig
from core.models.exceptions import IndexerError
from core.ports.chat import ChatStoreProtocol
from core.ports.lsp import LspPoolProtocol
from core.ports.uow import UnitOfWorkFactoryProtocol
from core.ports.watch import DirectoryWatchHandleProtocol, PathChangeCallback
from core.utils.logging import get_logger
from engine.models.collaborators import (
    CommandCollaborators,
    QueryCollaborators,
    ViewCollaborators,
)
from engine.services.command import CommandEngine
from engine.services.query import QueryEngine
from engine.services.resolver import SymbolTargetResolver
from engine.services.views import ViewEngine
from graph.services.navigator import GraphNavigator
from graph.services.references import ReferenceAnalyzer
from graph.services.risk import RiskAnalyzer
from indexing.services.batch import BatchIndexer
from indexing.services.directory_watcher import DirectoryTreeWatcher
from indexing.services.incremental import IncrementalIndexer
from indexing.services.lsp_runner import RepoLspCoordinator
from indexing.services.watcher import RepoWatcher
from lsp.services.pool import LspSessionPool
from storage.services.chat import SqlChatStore
from storage.services.uow import SqlUnitOfWorkFactory
from views.services.filesystem import FilesystemViewBuilder

logger = get_logger("engine.services.facade")


class _EngineRuntime:
    """Shared thread-safe runtime holding the warm LSP pool, UoW factory cache, and services."""

    def __init__(self, config: AppConfig, lsp_pool: LspPoolProtocol) -> None:
        """Initialize shared runtime collaborators and factory cache.

        Args:
            config: Base ``AppConfig`` supplying indexing, context, and LSP bounds.
            lsp_pool: Shared ``LspPoolProtocol`` session pool.
        """
        self.lsp_pool = lsp_pool
        self.lock = threading.RLock()
        self.factories_by_key: dict[str, UnitOfWorkFactoryProtocol] = {}
        self.chat_stores_by_key: dict[str, ChatStoreProtocol] = {}

        self.resolver = SymbolTargetResolver()
        self.navigator = GraphNavigator(config)
        self.reference_analyzer = ReferenceAnalyzer(config)
        self.risk_analyzer = RiskAnalyzer(config)
        self.source_reader = SourceReader(config)
        self.outline_builder = OutlineBuilder(config, navigator=self.navigator)
        self.context_builder = SymbolContextBuilder(
            config,
            navigator=self.navigator,
            source_reader=self.source_reader,
            reference_analyzer=self.reference_analyzer,
        )
        self.code_search = CodeSearchService(config)
        self.filesystem_view_builder = FilesystemViewBuilder()
        self.lsp_coordinator = RepoLspCoordinator(config=config)
        self.batch_indexer = BatchIndexer(config=config, lsp_coordinator=self.lsp_coordinator)
        self.incremental_indexer = IncrementalIndexer(config=config)
        self.watcher = RepoWatcher(config=config, incremental=self.incremental_indexer)
        self.directory_watcher = DirectoryTreeWatcher()

    @staticmethod
    def db_key(db_path: Path | str) -> str:
        """Normalize ``db_path`` into a canonical cache key string.

        Args:
            db_path: Database path or ``':memory:'``.

        Returns:
            ``':memory:'`` for in-memory SQLite, else the resolved filesystem path string.
        """
        raw = str(db_path).strip()
        if raw == ":memory:":
            return ":memory:"
        path_obj = Path(db_path)
        try:
            return str(path_obj.resolve())
        except (OSError, RuntimeError) as exc:
            logger.debug("Falling back to absolute path key for %s: %s", db_path, exc)
            return str(path_obj.absolute())

    def register_factory(self, factory: UnitOfWorkFactoryProtocol) -> None:
        """Store an explicitly injected ``UnitOfWorkFactoryProtocol`` under its DB key.

        Args:
            factory: Injected ``UnitOfWorkFactoryProtocol`` instance.
        """
        key = self.db_key(factory.db_path)
        with self.lock:
            self.factories_by_key[key] = factory

    def acquire_factory(self, db_path: Path) -> UnitOfWorkFactoryProtocol:
        """Fetch or create a cached ``SqlUnitOfWorkFactory`` for ``db_path``.

        Args:
            db_path: Target SQLite database path (or ``Path(':memory:')``).

        Returns:
            Cached or newly created ``UnitOfWorkFactoryProtocol`` instance.
        """
        key = self.db_key(db_path)
        with self.lock:
            cached = self.factories_by_key.get(key)
            if cached is not None:
                logger.debug("Reusing cached UoW factory for %s", key)
                return cached
            created = SqlUnitOfWorkFactory(db_path)
            self.factories_by_key[key] = created
            logger.debug("Created UoW factory for %s", key)
            return created

    def acquire_chat_store(self, db_path: Path) -> ChatStoreProtocol:
        """Fetch or create a cached ``SqlChatStore`` bound to ``db_path``."""
        key = self.db_key(db_path)
        with self.lock:
            cached = self.chat_stores_by_key.get(key)
            if cached is not None:
                return cached
            created = SqlChatStore(db_path)
            self.chat_stores_by_key[key] = created
            logger.debug("Created SqlChatStore for %s", key)
            return created

    def build_commands(
        self,
        config: AppConfig,
        uow_factory: UnitOfWorkFactoryProtocol,
    ) -> CommandEngine:
        """Construct a ``CommandEngine`` sharing this runtime's collaborators.

        Args:
            config: Scoped ``AppConfig`` for the engine instance.
            uow_factory: Scoped ``UnitOfWorkFactoryProtocol``.

        Returns:
            Configured ``CommandEngine`` instance.
        """
        return CommandEngine(
            config,
            uow_factory,
            self.lsp_pool,
            resolver=self.resolver,
            collaborators=CommandCollaborators(
                batch_indexer=self.batch_indexer,
                incremental_indexer=self.incremental_indexer,
                lsp_coordinator=self.lsp_coordinator,
                watcher=self.watcher,
            ),
        )

    def build_queries(
        self,
        config: AppConfig,
        uow_factory: UnitOfWorkFactoryProtocol,
        command_engine: CommandEngine,
    ) -> QueryEngine:
        """Construct a ``QueryEngine`` sharing this runtime's read collaborators."""
        return QueryEngine(
            config,
            uow_factory,
            resolver=self.resolver,
            command_engine=command_engine,
            collaborators=QueryCollaborators(
                navigator=self.navigator,
                reference_analyzer=self.reference_analyzer,
                risk_analyzer=self.risk_analyzer,
                source_reader=self.source_reader,
                outline_builder=self.outline_builder,
                context_builder=self.context_builder,
                code_search=self.code_search,
            ),
        )

    def build_views(
        self,
        config: AppConfig,
        uow_factory: UnitOfWorkFactoryProtocol,
    ) -> ViewEngine:
        """Construct a ``ViewEngine`` sharing this runtime's view builders."""
        return ViewEngine(
            config,
            uow_factory,
            collaborators=ViewCollaborators(
                filesystem_builder=self.filesystem_view_builder,
            ),
        )

    def close_all(self) -> None:
        """Close the shared LSP pool and any closable cached UoW factories."""
        self.lsp_pool.close_all()
        with self.lock:
            factories = list(self.factories_by_key.values())
            self.factories_by_key.clear()
            chat_stores = list(self.chat_stores_by_key.values())
            self.chat_stores_by_key.clear()

        for store in chat_stores:
            store.close()

        for factory in factories:
            close_fn = getattr(factory, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except (IndexerError, OSError, RuntimeError) as exc:
                    logger.warning(
                        "Error closing cached UoW factory for %s: %s",
                        factory.db_path,
                        exc,
                    )


class IndexerEngine:
    """Headless and UI-ready application facade combining ``QueryEngine`` and ``CommandEngine``."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        uow_factory: UnitOfWorkFactoryProtocol | None = None,
        lsp_pool: LspPoolProtocol | None = None,
        _runtime: _EngineRuntime | None = None,
    ) -> None:
        """Initialize an ``IndexerEngine`` with zero global mutable state.

        Args:
            config: Optional ``AppConfig`` (defaults to ``AppConfig()``).
            uow_factory: Optional injected ``UnitOfWorkFactoryProtocol`` for ``config.db_path``.
            lsp_pool: Optional injected ``LspPoolProtocol`` (defaults to ``LspSessionPool``).
            _runtime: Internal shared ``_EngineRuntime`` passed by ``for_db``.
        """
        base_config = config if config is not None else AppConfig()
        if uow_factory is not None and uow_factory.db_path != base_config.db_path:
            base_config = base_config.model_copy(update={"db_path": uow_factory.db_path})

        self._config = base_config
        resolved_pool = (
            lsp_pool
            if lsp_pool is not None
            else (_runtime.lsp_pool if _runtime is not None else LspSessionPool(config=base_config))
        )
        self._runtime = (
            _runtime if _runtime is not None else _EngineRuntime(base_config, resolved_pool)
        )
        self._lsp_pool = self._runtime.lsp_pool

        if uow_factory is not None:
            self._uow_factory = uow_factory
            self._runtime.register_factory(uow_factory)
        else:
            self._uow_factory = self._runtime.acquire_factory(self._config.db_path)

        self._commands = self._runtime.build_commands(self._config, self._uow_factory)
        self._queries = self._runtime.build_queries(
            self._config,
            self._uow_factory,
            self._commands,
        )
        self._views = self._runtime.build_views(self._config, self._uow_factory)

    @property
    def config(self) -> AppConfig:
        """Active immutable ``AppConfig`` for this engine instance."""
        return self._config

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database bound to this engine instance."""
        return self._uow_factory.db_path

    @property
    def queries(self) -> QueryEngine:
        """Side-effect-free ``QueryEngine`` bound to ``self.db_path``."""
        return self._queries

    @property
    def commands(self) -> CommandEngine:
        """Mutating ``CommandEngine`` bound to ``self.db_path`` and ``self.lsp_pool``."""
        return self._commands

    @property
    def views(self) -> ViewEngine:
        """Side-effect-free ``ViewEngine`` bound to ``self.db_path``."""
        return self._views

    @property
    def lsp_pool(self) -> LspPoolProtocol:
        """Shared ``LspPoolProtocol`` reused across ``for_db`` scopes."""
        return self._lsp_pool

    def open_chat_store(self, db_path: Path | str) -> ChatStoreProtocol:
        """Acquire or reuse the persistent ``ChatStoreProtocol`` for ``db_path``."""
        return self._runtime.acquire_chat_store(Path(db_path))

    def watch_directories(
        self,
        watches: Sequence[tuple[Path, bool]],
        on_change: PathChangeCallback,
    ) -> DirectoryWatchHandleProtocol:
        """Watch external profile directories without exposing ``watchdog`` types."""
        return self._runtime.directory_watcher.start(watches, on_change)

    def for_db(self, db_path: Path | str | None = None) -> "IndexerEngine":
        """Return an ``IndexerEngine`` targeting ``db_path`` while sharing ``lsp_pool``.

        Returns ``self`` when ``db_path`` is ``None`` or resolves to the same database
        key as ``self.db_path``.

        Args:
            db_path: Optional override SQLite database path (or ``':memory:'``).

        Returns:
            ``self`` if unchanged, otherwise a lightweight scoped ``IndexerEngine``
            sharing ``self.lsp_pool`` and ``self._factories_by_key``.
        """
        if db_path is None:
            return self
        target_path = Path(db_path)
        if self._runtime.db_key(target_path) == self._runtime.db_key(self.db_path):
            return self

        scoped_factory = self._runtime.acquire_factory(target_path)
        scoped_config = self._config.model_copy(update={"db_path": target_path})
        return IndexerEngine(
            scoped_config,
            uow_factory=scoped_factory,
            _runtime=self._runtime,
        )

    def close(self) -> None:
        """Close all pooled LSP sessions and any closable cached UoW factories."""
        self._runtime.close_all()

    def __enter__(self) -> Self:
        """Enter the ``IndexerEngine`` context manager.

        Returns:
            Active ``IndexerEngine`` instance.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the ``IndexerEngine`` context manager, releasing pooled resources.

        Args:
            exc_type: Exception class if raised inside the ``with`` block, else ``None``.
            exc_val: Exception instance if raised inside the ``with`` block, else ``None``.
            exc_tb: Traceback if an exception was raised, else ``None``.
        """
        _ = (exc_type, exc_val, exc_tb)
        self.close()


__all__ = [
    "IndexerEngine",
]
