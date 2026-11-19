"""Full and hash-diff repository batch indexing orchestrator service."""

from collections.abc import Sequence
from pathlib import Path

from core.models.config import AppConfig
from core.models.exceptions import ParseError, PathSecurityError
from core.models.file import FileRecord
from core.models.repo import RepoRecord
from core.models.runs import IndexRunMetrics
from core.ports.lsp import LspPoolProtocol
from core.ports.uow import UnitOfWorkFactoryProtocol, UnitOfWorkProtocol
from core.utils.logging import get_logger
from core.utils.pathing import normalize_repo_root
from core.utils.timestamps import utc_now_iso
from indexing.models.results import (
    BatchExtractionSummary,
    BatchIndexOptions,
    BatchIndexResult,
    ExtractionFailureEntry,
    LspBatchSummary,
    PersistedBatchOutcome,
)
from indexing.services.linker_runner import RepoLinkCoordinator
from indexing.services.lsp_runner import RepoLspCoordinator
from parsing.models.extraction import FileGraphExtraction
from parsing.models.linking import LinkResolutionReport
from parsing.services.pipeline import AstExtractionPipeline
from parsing.services.scanner import RepoScanner

logger = get_logger("indexing.services.batch")


class BatchIndexer:
    """Executes audited full or content-hash-diffed repository indexing runs."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        scanner: RepoScanner | None = None,
        pipeline: AstExtractionPipeline | None = None,
        link_coordinator: RepoLinkCoordinator | None = None,
        lsp_coordinator: RepoLspCoordinator | None = None,
    ) -> None:
        """Initialize the batch indexer with optional injected collaborators.

        Args:
            config: Optional ``AppConfig`` supplying scan, parse, and LSP bounds.
            scanner: Optional injected ``RepoScanner`` instance.
            pipeline: Optional injected ``AstExtractionPipeline`` instance.
            link_coordinator: Optional injected ``RepoLinkCoordinator`` instance.
            lsp_coordinator: Optional injected ``RepoLspCoordinator`` instance.
        """
        resolved_config = config if config is not None else AppConfig()
        self._config = resolved_config
        self._scanner = scanner if scanner is not None else RepoScanner(config=resolved_config)
        self._pipeline = (
            pipeline if pipeline is not None else AstExtractionPipeline(config=resolved_config)
        )
        self._link_coordinator = (
            link_coordinator if link_coordinator is not None else RepoLinkCoordinator()
        )
        self._lsp_coordinator = (
            lsp_coordinator
            if lsp_coordinator is not None
            else RepoLspCoordinator(config=resolved_config)
        )

    def _resolve_scanner(self, include_tests: bool | None) -> RepoScanner:
        """Return a ``RepoScanner`` honoring an explicit ``include_tests`` override.

        Args:
            include_tests: Optional per-run ``include_tests`` override.

        Returns:
            Active ``RepoScanner`` instance for the current run.
        """
        if include_tests is None or include_tests == self._scanner.options.include_tests:
            return self._scanner
        override_cfg = self._config.model_copy(update={"include_tests": include_tests})
        return RepoScanner(config=override_cfg)

    @staticmethod
    def _prepare_repo_record(uow: UnitOfWorkProtocol, scanned_repo: RepoRecord) -> RepoRecord:
        """Preserve ``created_at`` when ``scanned_repo`` already exists and commit it.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            scanned_repo: Newly constructed ``RepoRecord`` from ``RepoScanner``.

        Returns:
            Persisted ``RepoRecord`` preserving any prior ``created_at`` timestamp.
        """
        existing = uow.repos.get_by_id(scanned_repo.id)
        repo = (
            scanned_repo.model_copy(update={"created_at": existing.created_at})
            if existing is not None
            else scanned_repo
        )
        uow.repos.upsert(repo)
        uow.commit()
        return repo

    @staticmethod
    def _select_files_to_extract(
        uow: UnitOfWorkProtocol,
        repo_id: str,
        scanned_files: Sequence[FileRecord],
        *,
        force: bool,
    ) -> list[FileRecord]:
        """Select all scanned files when ``force=True``, else only new or modified files.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            repo_id: Target repository identifier.
            scanned_files: Files discovered by ``RepoScanner``.
            force: Whether to bypass ``content_hash`` diffing.

        Returns:
            Ordered list of ``FileRecord`` items requiring AST extraction.
        """
        if force:
            return list(scanned_files)
        existing_files = {
            rec.id: rec
            for rec in uow.files.list_for_repo(repo_id, include_tests=True)
        }
        return [
            rec
            for rec in scanned_files
            if rec.id not in existing_files
            or existing_files[rec.id].content_hash != rec.content_hash
        ]

    def _extract_changed_files(
        self,
        repo_root: Path,
        to_extract: Sequence[FileRecord],
    ) -> BatchExtractionSummary:
        """Run ``AstExtractionPipeline`` over ``to_extract``, isolating per-file parse failures.

        Args:
            repo_root: Normalized repository root directory.
            to_extract: Candidate ``FileRecord`` items to parse and extract.

        Returns:
            ``BatchExtractionSummary`` containing extractions, failures, and counters.
        """
        extractions: list[FileGraphExtraction] = []
        failures: list[ExtractionFailureEntry] = []
        nodes_extracted = 0
        edges_extracted = 0

        for file_rec in to_extract:
            try:
                extraction = self._pipeline.extract_file(repo_root, file_rec)
                extractions.append(extraction)
                nodes_extracted += len(extraction.nodes)
                edges_extracted += len(extraction.edges)
            except (ParseError, PathSecurityError, OSError) as exc:
                logger.warning("Failed AST extraction for %s: %s", file_rec.file_path, exc)
                failures.append(
                    ExtractionFailureEntry(
                        file_path=file_rec.file_path,
                        error=str(exc),
                    )
                )

        return BatchExtractionSummary(
            attempted_count=len(to_extract),
            extractions=extractions,
            failures=failures,
            nodes_extracted=nodes_extracted,
            edges_extracted=edges_extracted,
        )

    def _persist_and_link_batch(
        self,
        uow: UnitOfWorkProtocol,
        repo: RepoRecord,
        scanned_files: Sequence[FileRecord],
        extractions: Sequence[FileGraphExtraction],
    ) -> tuple[int, LinkResolutionReport]:
        """Persist extracted file graphs, prune stale files, link edges, and commit.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            repo: Active ``RepoRecord``.
            scanned_files: Full list of scanned repository files.
            extractions: Successfully extracted file graphs.

        Returns:
            Tuple ``(files_pruned, link_report)``.
        """
        for extraction in extractions:
            uow.refresh.invalidate_for_file(extraction.file.id)
            uow.replace_file_graph(extraction.file, extraction.nodes, extraction.edges)
            uow.refresh.mark_file_symbols_stale(extraction.file.id)

        keep_file_ids = {rec.id for rec in scanned_files}
        pruned_count = uow.files.prune_not_in_ids(repo.id, keep_file_ids)
        link_report = self._link_coordinator.link_repository(uow, repo.id)
        uow.edges.delete_dangling_lsp_references(repo.id)
        uow.repos.upsert(repo)
        uow.commit()
        return pruned_count, link_report

    def _execute_audited_run(
        self,
        uow: UnitOfWorkProtocol,
        repo: RepoRecord,
        repo_root: Path,
        scanned_files: Sequence[FileRecord],
        *,
        force: bool,
    ) -> PersistedBatchOutcome:
        """Execute an audited indexing run and record terminal ``completed`` or ``failed`` state.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            repo: Prepared ``RepoRecord``.
            repo_root: Normalized repository root directory.
            scanned_files: Scanned ``FileRecord`` items.
            force: Whether all scanned files must be re-extracted.

        Returns:
            ``PersistedBatchOutcome`` containing audit run counters and ``LinkResolutionReport``.

        Raises:
            Exception: Re-raises any unexpected persistence or linking failure after recording
                ``status="failed"`` in ``index_runs``.
        """
        run_id = uow.runs.start_run(repo.id, run_type="full" if force else "incremental")
        uow.commit()

        batch = BatchExtractionSummary()
        try:
            to_extract = self._select_files_to_extract(
                uow,
                repo.id,
                scanned_files,
                force=force,
            )
            batch = self._extract_changed_files(repo_root, to_extract)
            pruned_count, link_report = self._persist_and_link_batch(
                uow,
                repo,
                scanned_files,
                batch.extractions,
            )
            error_msg = (
                f"{len(batch.failures)} file(s) failed AST extraction"
                if batch.failures
                else None
            )
            uow.runs.finish_run(
                run_id,
                "completed",
                IndexRunMetrics(
                    files_scanned=len(scanned_files),
                    nodes_extracted=batch.nodes_extracted,
                    edges_extracted=batch.edges_extracted,
                    error_message=error_msg,
                    completed_at=utc_now_iso(),
                ),
            )
            uow.commit()
        except Exception as exc:
            logger.exception("Batch indexing run %s failed for %s", run_id, repo.id)
            uow.rollback()
            uow.runs.finish_run(
                run_id,
                "failed",
                IndexRunMetrics(
                    files_scanned=len(scanned_files),
                    nodes_extracted=batch.nodes_extracted,
                    edges_extracted=batch.edges_extracted,
                    error_message=str(exc),
                    completed_at=utc_now_iso(),
                ),
            )
            uow.commit()
            raise

        return PersistedBatchOutcome(
            run_id=run_id,
            repo=repo,
            files_scanned=len(scanned_files),
            files_indexed=len(batch.extractions),
            files_unchanged=len(scanned_files) - batch.attempted_count,
            files_pruned=pruned_count,
            failures=batch.failures,
            nodes_extracted=batch.nodes_extracted,
            edges_extracted=batch.edges_extracted,
            link_report=link_report,
        )

    def index_repository(
        self,
        uow_factory: UnitOfWorkFactoryProtocol,
        repo_root: Path | str,
        options: BatchIndexOptions | None = None,
        *,
        lsp_pool: LspPoolProtocol | None = None,
    ) -> BatchIndexResult:
        """Scan, extract, persist, link, and optionally LSP-enrich a repository.

        Args:
            uow_factory: ``UnitOfWorkFactoryProtocol`` managing database sessions.
            repo_root: Path to the repository root directory on disk.
            options: Optional ``BatchIndexOptions`` controlling ``force``, ``include_tests``,
                and ``enrich_lsp``.
            lsp_pool: Optional warm ``LspPoolProtocol`` reused when ``options.enrich_lsp=True``.

        Returns:
            Immutable ``BatchIndexResult`` with run counters, link report, and graph stats.

        Raises:
            FileNotFoundError: If ``repo_root`` does not exist.
            NotADirectoryError: If ``repo_root`` is not a directory.
            DatabaseError: If initializing or writing to the database fails.
        """
        resolved_opts = options if options is not None else BatchIndexOptions()
        normalized_root = normalize_repo_root(repo_root)
        active_scanner = self._resolve_scanner(resolved_opts.include_tests)
        scan_report = active_scanner.scan_repository(normalized_root)

        logger.info(
            "Starting batch index for %s (%d files scanned, force=%s)",
            scan_report.repo.id,
            len(scan_report.files),
            resolved_opts.force,
        )

        with uow_factory.open(initialize=True) as uow:
            repo = self._prepare_repo_record(uow, scan_report.repo)
            outcome = self._execute_audited_run(
                uow,
                repo,
                normalized_root,
                scan_report.files,
                force=resolved_opts.force,
            )
            lsp_summary: LspBatchSummary | None = None
            if resolved_opts.enrich_lsp:
                lsp_summary = self._lsp_coordinator.enrich_repository(
                    uow,
                    repo,
                    lsp_pool=lsp_pool,
                )
                uow.commit()
            graph_stats = uow.diagnostics.get_repo_graph_stats(repo.id)

        logger.info(
            "Completed batch index run %s for %s (indexed=%d, unchanged=%d, pruned=%d, failed=%d)",
            outcome.run_id,
            repo.id,
            outcome.files_indexed,
            outcome.files_unchanged,
            outcome.files_pruned,
            len(outcome.failures),
        )
        return BatchIndexResult(
            run_id=outcome.run_id,
            repo=repo,
            db_path=str(uow_factory.db_path),
            files_scanned=outcome.files_scanned,
            files_indexed=outcome.files_indexed,
            files_unchanged=outcome.files_unchanged,
            files_pruned=outcome.files_pruned,
            files_failed=len(outcome.failures),
            failures=outcome.failures,
            nodes_extracted=outcome.nodes_extracted,
            edges_extracted=outcome.edges_extracted,
            link_report=outcome.link_report,
            graph_stats=graph_stats,
            lsp_summary=lsp_summary,
        )


__all__ = [
    "BatchIndexer",
]
