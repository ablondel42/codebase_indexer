"""Single-symbol and full-repository LSP reference enrichment coordinator."""

from collections.abc import Generator, Sequence
from contextlib import contextmanager
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
        """Initialize the LSP coordinator with configuration and ``ReferenceEnricher``."""
        resolved_config = config if config is not None else AppConfig()
        self._config = resolved_config
        self._enricher = (
            enricher if enricher is not None else ReferenceEnricher(config=resolved_config)
        )

    @staticmethod
    @contextmanager
    def _pooled_client(
        lsp_pool: LspPoolProtocol,
        repo_root: Path,
    ) -> Generator[LspClientProtocol, None, None]:
        """Acquire a pooled LSP session for ``repo_root`` and evict on server termination."""
        client = lsp_pool.acquire(repo_root)
        try:
            yield client
        finally:
            if not client.is_running:
                lsp_pool.evict(repo_root)

    def enrich_symbol(
        self,
        uow: UnitOfWorkProtocol,
        lsp_pool: LspPoolProtocol,
        symbol: SymbolSummary | SymbolNode,
        *,
        open_all_files: bool = False,
    ) -> SymbolEnrichmentResult:
        """Refresh LSP references for ``symbol`` using a warm pooled session."""
        repo = uow.repos.get_by_id(symbol.repo_id)
        if repo is None or not repo.root_path:
            raise DatabaseError(
                f"Repository metadata missing for LSP refresh on symbol {symbol.id}.",
                error_code="references_unavailable",
                context={"symbol_id": symbol.id, "repo_id": symbol.repo_id},
            )

        repo_root = Path(repo.root_path)
        with self._pooled_client(lsp_pool, repo_root) as client:
            return self._enricher.enrich_symbol(
                uow,
                client,
                symbol,
                open_all_files=open_all_files,
                raise_on_error=True,
            )

    def _enrich_one_repo_symbol(
        self,
        uow: UnitOfWorkProtocol,
        client: LspClientProtocol,
        node: SymbolNode,
        file_rec: FileRecord | None,
    ) -> int | None:
        """Enrich one repository symbol, returning its reference count or ``None`` on error."""
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
        """Iterate over ``symbols`` with an active LSP client and aggregate counters."""
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

    def _run_batch_enrichment(
        self,
        uow: UnitOfWorkProtocol,
        repo_root: Path,
        symbols: Sequence[SymbolNode],
        files_by_id: dict[str, FileRecord],
        *,
        lsp_pool: LspPoolProtocol | None,
    ) -> LspBatchSummary:
        """Execute batch LSP enrichment via ``lsp_pool`` or a scoped ``PyrightLspClient``."""
        try:
            if lsp_pool is not None:
                with self._pooled_client(lsp_pool, repo_root) as client:
                    return self._enrich_symbols_with_client(uow, client, symbols, files_by_id)
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
        """Enrich LSP references for all non-module symbols in ``repo``."""
        files_by_id = {rec.id: rec for rec in uow.files.list_for_repo(repo.id, include_tests=True)}
        symbols = [
            node for node in uow.nodes.list_for_repo(repo.id) if node.kind != SymbolKind.MODULE
        ]
        if not symbols:
            return LspBatchSummary(available=True, symbols_attempted=0)

        return self._run_batch_enrichment(
            uow,
            Path(repo.root_path),
            symbols,
            files_by_id,
            lsp_pool=lsp_pool,
        )


__all__ = [
    "RepoLspCoordinator",
]
