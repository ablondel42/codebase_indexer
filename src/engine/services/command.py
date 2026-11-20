"""Write-side command, LSP enrichment, and filesystem watch orchestration engine."""

from collections.abc import Sequence
from pathlib import Path

from core.models.config import AppConfig
from core.models.exceptions import ValidationError
from core.ports.events import EventCallback
from core.ports.lsp import LspPoolProtocol
from core.ports.uow import UnitOfWorkFactoryProtocol
from core.utils.logging import get_logger
from engine.models.collaborators import CommandCollaborators
from engine.services.resolver import SymbolTargetResolver
from indexing.models.events import FileChangeEvent
from indexing.models.results import (
    BatchIndexOptions,
    BatchIndexResult,
    IncrementalBatchResult,
)
from indexing.services.batch import BatchIndexer
from indexing.services.incremental import IncrementalIndexer
from indexing.services.lsp_runner import RepoLspCoordinator
from indexing.services.watcher import RepoWatcher
from lsp.models.enrichment import SymbolEnrichmentResult

logger = get_logger("engine.services.command")


class CommandEngine:
    """Executes mutating indexing, incremental batch, LSP refresh, and watch commands."""

    def __init__(
        self,
        config: AppConfig,
        uow_factory: UnitOfWorkFactoryProtocol,
        lsp_pool: LspPoolProtocol,
        *,
        resolver: SymbolTargetResolver | None = None,
        collaborators: CommandCollaborators | None = None,
    ) -> None:
        """Initialize the command engine with injected persistence, LSP, and domain services."""
        deps = collaborators if collaborators is not None else CommandCollaborators()
        self._config = config
        self._uow_factory = uow_factory
        self._lsp_pool = lsp_pool
        self._resolver = resolver if resolver is not None else SymbolTargetResolver()
        self._lsp_coordinator = (
            deps.lsp_coordinator
            if deps.lsp_coordinator is not None
            else RepoLspCoordinator(config=config)
        )
        self._batch_indexer = (
            deps.batch_indexer
            if deps.batch_indexer is not None
            else BatchIndexer(config=config, lsp_coordinator=self._lsp_coordinator)
        )
        self._incremental_indexer = (
            deps.incremental_indexer
            if deps.incremental_indexer is not None
            else IncrementalIndexer(config=config)
        )
        self._watcher = (
            deps.watcher
            if deps.watcher is not None
            else RepoWatcher(config=config, incremental=self._incremental_indexer)
        )

    def index_repository(
        self,
        repo_root: Path | str,
        options: BatchIndexOptions | None = None,
    ) -> BatchIndexResult:
        """Execute a full or content-hash-diffed repository indexing run.

        Args:
            repo_root: Path to the repository root directory on disk.
            options: Optional ``BatchIndexOptions`` controlling ``force``, ``include_tests``,
                and ``enrich_lsp``.

        Returns:
            Populated ``BatchIndexResult`` with run counters, link report, and graph stats.

        Raises:
            FileNotFoundError: If ``repo_root`` does not exist.
            NotADirectoryError: If ``repo_root`` is not a directory.
            DatabaseError: If database initialization or persistence fails.
        """
        return self._batch_indexer.index_repository(
            self._uow_factory,
            repo_root,
            options,
            lsp_pool=self._lsp_pool,
        )

    def process_watch_events(
        self,
        repo_root: Path | str,
        events: Sequence[FileChangeEvent],
    ) -> IncrementalBatchResult:
        """Apply a batch of normalized ``FileChangeEvent`` updates atomically.

        Args:
            repo_root: Watched repository root directory path.
            events: Sequence of ``FileChangeEvent`` items to collapse and apply.

        Returns:
            ``IncrementalBatchResult`` summarizing per-file statuses and batch linking.

        Raises:
            DatabaseError: If opening or committing the Unit of Work fails.
        """
        with self._uow_factory.open(initialize=True) as uow:
            return self._incremental_indexer.process_batch(uow, repo_root, events)

    def refresh_symbol_references(
        self,
        symbol: str,
        *,
        repo: str | None = None,
        open_all_files: bool = False,
    ) -> SymbolEnrichmentResult:
        """Resolve ``symbol`` and refresh its persisted LSP ``references`` edges.

        Args:
            symbol: Symbol ID (``sym:...``), ``file:symbol``, qualified name, or short name.
            repo: Optional repository ID, short name, or root path.
            open_all_files: Whether to synchronize all tracked ``.py`` files beforehand.

        Returns:
            ``SymbolEnrichmentResult`` with updated reference counts and refresh status.

        Raises:
            DatabaseNotFoundError: If the backing database file does not exist.
            DatabaseError: If repository metadata or persistence fails.
            SymbolNotFoundError: If ``symbol`` cannot be uniquely resolved.
            PathSecurityError: If the target file violates repository confinement.
            LspError: If acquiring or querying the language server fails.
        """
        with self._uow_factory.open(must_exist=True, initialize=True) as uow:
            target = self._resolver.resolve_one(uow, symbol, repo_selector=repo)
            result = self._lsp_coordinator.enrich_symbol(
                uow,
                self._lsp_pool,
                target,
                open_all_files=open_all_files,
            )
            uow.commit()
            logger.debug(
                "Refreshed LSP references for %s (count=%d, status=%s)",
                target.id,
                result.reference_count,
                result.refresh_status,
            )
            return result

    def watch_repository(
        self,
        repo_root: Path | str,
        *,
        debounce_ms: int = 500,
        include_tests: bool | None = None,
        verbose: bool = False,
        on_event: EventCallback | None = None,
    ) -> None:
        """Watch ``repo_root`` continuously and apply debounced incremental updates.

        Args:
            repo_root: Repository root directory path to watch.
            debounce_ms: Quiet-period debounce window in milliseconds (must be ``>= 0``).
            include_tests: Optional override controlling whether test files are watched.
            verbose: When ``True``, emits ``SKIPPED`` file events and ``INFO`` logs.
            on_event: Optional ``EventCallback`` receiving typed watch lifecycle events.

        Raises:
            ValidationError: If ``debounce_ms < 0``.
            FileNotFoundError: If ``repo_root`` does not exist.
            NotADirectoryError: If ``repo_root`` is not a directory.
        """
        if debounce_ms < 0:
            raise ValidationError(
                "debounce_ms must be >= 0",
                context={"debounce_ms": debounce_ms},
            )
        active_watcher = self._watcher
        if include_tests is not None and include_tests != self._config.include_tests:
            override_config = self._config.model_copy(update={"include_tests": include_tests})
            active_watcher = RepoWatcher(config=override_config)

        active_watcher.watch(
            self._uow_factory,
            repo_root,
            debounce_ms=debounce_ms,
            verbose=verbose,
            on_event=on_event,
        )

    def close(self) -> None:
        """Close all warm language server sessions held by ``lsp_pool``."""
        self._lsp_pool.close_all()


__all__ = [
    "CommandEngine",
]
