"""Single-symbol and full-repository LSP reference enrichment coordinator."""

from collections.abc import Sequence
from pathlib import Path

from core.models.config import AppConfig
from core.models.enums import SymbolKind
from core.models.exceptions import DatabaseError, LspError, PathSecurityError
from core.models.file import FileRecord
from core.models.node import SymbolNode, SymbolSummary
from core.models.repo import RepoRecord
from core.ports.lsp import LspClientProtocol, LspPoolProtocol
from core.ports.uow import UnitOfWorkProtocol
from core.utils.logging import get_logger
from indexing.models.results import LspBatchSummary
from lsp.models.enrichment import SymbolEnrichmentResult
from lsp.services.client import PyrightLspClient
from lsp.services.enricher import ReferenceEnricher

logger = get_logger("indexing.services.lsp_runner")


class RepoLspCoordinator:
    """Coordinates Pyright reference enrichment across pooled or ephemeral LSP sessions."""

    def __init__(
        self,
        config: AppConfig | None = None,
        enricher: ReferenceEnricher | None = None,
    ) -> None:
        """Initialize the LSP coordinator with configuration and ``ReferenceEnricher``.

        Args:
            config: Optional ``AppConfig`` supplying timeout and file-size bounds.
            enricher: Optional injected ``ReferenceEnricher`` instance.
        """
        resolved_config = config if config is not None else AppConfig()
        self._config = resolved_config
        self._enricher = (
            enricher if enricher is not None else ReferenceEnricher(config=resolved_config)
        )

    def enrich_symbol(
        self,
        uow: UnitOfWorkProtocol,
        lsp_pool: LspPoolProtocol,
        symbol: SymbolSummary | SymbolNode,
        *,
        open_all_files: bool = False,
    ) -> SymbolEnrichmentResult:
        """Refresh LSP references for ``symbol`` using a warm pooled session.

        Evicts the cached session for ``repo.root_path`` if the language server
        terminates during the enrichment call.

        Args:
            uow: Active ``UnitOfWorkProtocol`` providing repository ports.
            lsp_pool: ``LspPoolProtocol`` managing warm per-repository LSP sessions.
            symbol: Target ``SymbolSummary`` or ``SymbolNode`` to enrich.
            open_all_files: Whether to synchronize all auxiliary ``.py`` files beforehand.

        Returns:
            ``SymbolEnrichmentResult`` with updated reference counts and status.

        Raises:
            DatabaseError: If repository metadata for ``symbol.repo_id`` is missing.
            PathSecurityError: If the target file escapes ``repo.root_path`` or is a symlink.
            LspError: If acquiring the LSP client or querying references fails.
        """
        repo = uow.repos.get_by_id(symbol.repo_id)
        if repo is None or not repo.root_path:
            raise DatabaseError(
                f"Repository metadata missing for LSP refresh on symbol {symbol.id}.",
                error_code="references_unavailable",
                context={"symbol_id": symbol.id, "repo_id": symbol.repo_id},
            )

        repo_root = Path(repo.root_path)
        client = lsp_pool.acquire(repo_root)
        try:
            return self._enricher.enrich_symbol(
                uow,
                client,
                symbol,
                open_all_files=open_all_files,
                raise_on_error=True,
            )
        finally:
            if not client.is_running:
                lsp_pool.evict(repo_root)

    def _enrich_one_repo_symbol(
        self,
        uow: UnitOfWorkProtocol,
        client: LspClientProtocol,
        node: SymbolNode,
        file_rec: FileRecord | None,
    ) -> int | None:
        """Enrich one repository symbol, returning its reference count or ``None`` on error.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            client: Active ``LspClientProtocol`` session.
            node: Non-module ``SymbolNode`` to enrich.
            file_rec: Containing ``FileRecord`` if present in ``files_by_id``.

        Returns:
            Persisted reference count on success, or ``None`` if enrichment was skipped.
        """
        if file_rec is None:
            logger.warning(
                "Skipping LSP enrichment for %s: missing file record %s",
                node.id,
                node.file_id,
            )
            return None

        summary = SymbolSummary.from_node(
            node,
            file_path=file_rec.file_path,
            module_path=file_rec.module_path,
            is_test=file_rec.is_test,
        )
        try:
            result = self._enricher.enrich_symbol(
                uow,
                client,
                summary,
                open_all_files=False,
                raise_on_error=True,
            )
            return result.reference_count
        except (LspError, PathSecurityError, ValueError, OSError) as exc:
            logger.warning("LSP enrichment skipped for symbol %s: %s", node.id, exc)
            return None

    def _enrich_symbols_with_client(
        self,
        uow: UnitOfWorkProtocol,
        client: LspClientProtocol,
        symbols: Sequence[SymbolNode],
        files_by_id: dict[str, FileRecord],
    ) -> LspBatchSummary:
        """Iterate over ``symbols`` with an active LSP client and aggregate counters.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            client: Started ``LspClientProtocol`` session.
            symbols: Non-module ``SymbolNode`` declarations in the repository.
            files_by_id: Lookup table of ``FileRecord`` entities keyed by ``file_id``.

        Returns:
            Aggregated ``LspBatchSummary`` with ``available=True``.
        """
        refreshed = 0
        errors = 0
        total_refs = 0

        for node in symbols:
            ref_count = self._enrich_one_repo_symbol(
                uow,
                client,
                node,
                files_by_id.get(node.file_id),
            )
            if ref_count is None:
                errors += 1
            else:
                refreshed += 1
                total_refs += ref_count

        return LspBatchSummary(
            available=True,
            symbols_attempted=len(symbols),
            symbols_refreshed=refreshed,
            errors=errors,
            total_references=total_refs,
            error=None,
        )

    def _enrich_with_pool(
        self,
        uow: UnitOfWorkProtocol,
        lsp_pool: LspPoolProtocol,
        repo_root: Path,
        symbols: Sequence[SymbolNode],
        files_by_id: dict[str, FileRecord],
    ) -> LspBatchSummary:
        """Run batch LSP enrichment using a session from ``lsp_pool``.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            lsp_pool: Warm ``LspPoolProtocol`` session pool.
            repo_root: Repository root directory path.
            symbols: Non-module symbols to enrich.
            files_by_id: Tracked files keyed by ``file_id``.

        Returns:
            ``LspBatchSummary`` capturing availability, counts, and any startup error.
        """
        try:
            client = lsp_pool.acquire(repo_root)
        except LspError as exc:
            logger.warning("LSP pool session unavailable for %s: %s", repo_root, exc)
            return LspBatchSummary(
                available=False,
                symbols_attempted=len(symbols),
                error=str(exc),
            )

        try:
            return self._enrich_symbols_with_client(uow, client, symbols, files_by_id)
        finally:
            if not client.is_running:
                lsp_pool.evict(repo_root)

    def _enrich_with_ephemeral_client(
        self,
        uow: UnitOfWorkProtocol,
        repo_root: Path,
        symbols: Sequence[SymbolNode],
        files_by_id: dict[str, FileRecord],
    ) -> LspBatchSummary:
        """Run batch LSP enrichment inside a scoped ``PyrightLspClient`` context.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            repo_root: Repository root directory path.
            symbols: Non-module symbols to enrich.
            files_by_id: Tracked files keyed by ``file_id``.

        Returns:
            ``LspBatchSummary`` capturing availability, counts, and any startup error.
        """
        try:
            with PyrightLspClient(config=self._config) as client:
                client.start(repo_root)
                return self._enrich_symbols_with_client(uow, client, symbols, files_by_id)
        except LspError as exc:
            logger.warning("LSP enrichment unavailable for %s: %s", repo_root, exc)
            return LspBatchSummary(
                available=False,
                symbols_attempted=len(symbols),
                error=str(exc),
            )

    def enrich_repository(
        self,
        uow: UnitOfWorkProtocol,
        repo: RepoRecord,
        *,
        lsp_pool: LspPoolProtocol | None = None,
    ) -> LspBatchSummary:
        """Enrich LSP references for all non-module symbols in ``repo``.

        Args:
            uow: Active ``UnitOfWorkProtocol`` providing repository ports.
            repo: Target ``RepoRecord`` to enrich.
            lsp_pool: Optional ``LspPoolProtocol``; when omitted, spawns a scoped
                ``PyrightLspClient`` closed on completion.

        Returns:
            ``LspBatchSummary`` reporting refreshed symbol and reference counts.
        """
        files_by_id = {
            rec.id: rec
            for rec in uow.files.list_for_repo(repo.id, include_tests=True)
        }
        symbols = [
            node
            for node in uow.nodes.list_for_repo(repo.id)
            if node.kind != SymbolKind.MODULE
        ]
        if not symbols:
            return LspBatchSummary(available=True, symbols_attempted=0)

        repo_root = Path(repo.root_path)
        if lsp_pool is not None:
            return self._enrich_with_pool(uow, lsp_pool, repo_root, symbols, files_by_id)
        return self._enrich_with_ephemeral_client(uow, repo_root, symbols, files_by_id)


__all__ = [
    "RepoLspCoordinator",
]
