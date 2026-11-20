"""Full and hash-diff repository batch indexing orchestrator service."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from core.models.config import AppConfig
from core.models.exceptions import ParseError, PathSecurityError
from core.models.file import FileRecord
from core.models.repo import RepoRecord
from core.models.runs import IndexRunMetrics
from core.models.search import TextDocumentRecord
from core.ports.lsp import LspPoolProtocol
from core.ports.uow import UnitOfWorkFactoryProtocol, UnitOfWorkProtocol
from core.utils.logging import get_logger
from core.utils.pathing import normalize_repo_root
from core.utils.text_files import (
    build_text_document_from_source,
    read_text_document,
    walk_repo_text_stats,
)
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
from indexing.utils.collapse import build_repo_upsert_record
from parsing.models.extraction import FileGraphExtraction
from parsing.models.linking import LinkResolutionReport
from parsing.models.scan import ScanReport
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
        """Initialize the batch indexer with optional injected collaborators."""
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
        """Return a ``RepoScanner`` honoring an explicit ``include_tests`` override."""
        if include_tests is None or include_tests == self._scanner.options.include_tests:
            return self._scanner
        override_cfg = self._config.model_copy(update={"include_tests": include_tests})
        return RepoScanner(config=override_cfg)

    @staticmethod
    def _prepare_repo_record(uow: UnitOfWorkProtocol, scanned_repo: RepoRecord) -> RepoRecord:
        """Preserve ``created_at`` when ``scanned_repo`` already exists and commit it."""
        existing = uow.repos.get_by_id(scanned_repo.id)
        repo = build_repo_upsert_record(existing, scanned_repo)
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
        """Select all scanned files when ``force=True``, else only new or modified files."""
        if force:
            return list(scanned_files)
        existing_files = {
            rec.id: rec for rec in uow.files.list_for_repo(repo_id, include_tests=True)
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
        source_by_file_id: Mapping[str, str],
    ) -> BatchExtractionSummary:
        """Run ``AstExtractionPipeline`` over ``to_extract``, isolating per-file parse failures."""
        extractions: list[FileGraphExtraction] = []
        failures: list[ExtractionFailureEntry] = []
        nodes_extracted = 0
        edges_extracted = 0

        for file_rec in to_extract:
            try:
                extraction = self._pipeline.extract_file(
                    repo_root,
                    file_rec,
                    source_text=source_by_file_id.get(file_rec.id),
                )
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
        """Persist extracted file graphs, prune stale files, link edges, and commit."""
        for extraction in extractions:
            uow.refresh.invalidate_for_file(extraction.file.id)
            uow.replace_file_graph(extraction.file, extraction.nodes, extraction.edges)
            uow.refresh.mark_file_symbols_stale(extraction.file.id)

        keep_file_ids = {rec.id for rec in scanned_files}
        pruned_count = uow.files.prune_not_in_ids(repo.id, keep_file_ids)
        link_report = self._link_coordinator.link_repository(uow, repo.id)
        uow.repos.upsert(repo)
        uow.commit()
        return pruned_count, link_report

    def _build_or_read_text_doc(
        self,
        repo_id: str,
        repo_root: Path,
        rel_path: str,
        mtime_ns: int,
        preloaded: tuple[FileRecord, str] | None,
    ) -> TextDocumentRecord | None:
        """Build a ``TextDocumentRecord`` from preloaded scan data or read from disk."""
        if preloaded is not None:
            file_rec, text = preloaded
            return build_text_document_from_source(
                file_rec.id,
                repo_id,
                rel_path,
                text,
                meta=(file_rec.content_hash, file_rec.size_bytes, mtime_ns, file_rec.is_test),
            )
        return read_text_document(
            repo_id,
            repo_root,
            rel_path,
            max_file_size_bytes=self._config.max_file_size_bytes,
            test_dir_names=self._config.test_dir_names,
        )

    def _sync_text_corpus(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        repo_root: Path,
        scan_report: ScanReport,
        *,
        force: bool,
    ) -> None:
        """Hash-diff and synchronize all UTF-8 repository text documents into ``text_files``."""
        disk_stats = walk_repo_text_stats(
            repo_root,
            max_file_size_bytes=self._config.max_file_size_bytes,
            test_dir_names=self._config.test_dir_names,
        )
        existing_by_path = {
            meta.file_path: meta for meta in uow.text_search.list_metadata_for_repo(repo_id)
        }
        preloaded_by_path: dict[str, tuple[FileRecord, str]] = {
            rec.file_path: (rec, scan_report.source_by_file_id[rec.id])
            for rec in scan_report.files
            if rec.id in scan_report.source_by_file_id
        }
        keep_ids: set[str] = set()
        to_upsert: list[TextDocumentRecord] = []

        for st in disk_stats:
            prev = existing_by_path.get(st.file_path)
            if (
                not force
                and prev is not None
                and prev.mtime_ns > 0
                and prev.mtime_ns == st.mtime_ns
                and prev.size_bytes == st.size_bytes
            ):
                keep_ids.add(prev.id)
                continue

            doc = self._build_or_read_text_doc(
                repo_id,
                repo_root,
                st.file_path,
                st.mtime_ns,
                preloaded_by_path.get(st.file_path),
            )
            if doc is None:
                continue
            keep_ids.add(doc.id)
            if force or prev is None or prev.content_hash != doc.content_hash:
                to_upsert.append(doc)

        uow.text_search.upsert_many(to_upsert)
        uow.text_search.prune_not_in_ids(repo_id, keep_ids)

    def _execute_audited_run(
        self,
        uow: UnitOfWorkProtocol,
        repo: RepoRecord,
        repo_root: Path,
        scan_report: ScanReport,
        *,
        force: bool,
    ) -> PersistedBatchOutcome:
        """Execute an audited indexing run and record terminal ``completed`` or ``failed`` state."""
        scanned_files = scan_report.files
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
            batch = self._extract_changed_files(
                repo_root,
                to_extract,
                scan_report.source_by_file_id,
            )
            self._sync_text_corpus(uow, repo.id, repo_root, scan_report, force=force)
            pruned_count, link_report = self._persist_and_link_batch(
                uow,
                repo,
                scanned_files,
                batch.extractions,
            )
            error_msg = (
                f"{len(batch.failures)} file(s) failed AST extraction" if batch.failures else None
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
        """Scan, extract, persist, link, and optionally LSP-enrich a repository."""
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
                scan_report,
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
