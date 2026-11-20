"""Incremental per-file and batch reindexing service with non-destructive link recovery."""

from collections.abc import Sequence
from pathlib import Path

from core.models.config import AppConfig
from core.models.exceptions import IndexerError, ParseError, PathSecurityError
from core.ports.uow import UnitOfWorkProtocol
from core.utils.logging import get_logger
from core.utils.pathing import (
    derive_file_id,
    derive_repo_id,
    ensure_safe_repo_file,
    to_relative_path,
)
from core.utils.text_files import build_text_document_from_source, read_text_document
from indexing.models.events import FileChangeEvent, FileEventType
from indexing.models.results import (
    IncrementalBatchResult,
    IncrementalFileResult,
    IncrementalStatus,
)
from indexing.services.linker_runner import RepoLinkCoordinator
from indexing.utils.collapse import build_repo_upsert_record, collapse_events
from indexing.utils.watch_paths import is_ignored_watch_path
from parsing.models.extraction import FileGraphExtraction
from parsing.models.linking import LinkResolutionReport
from parsing.models.scan import ScannedFile
from parsing.services.pipeline import AstExtractionPipeline
from parsing.services.scanner import RepoScanner
from parsing.utils.filters import is_supported_source_file

logger = get_logger("indexing.services.incremental")

_MUTATED_STATUSES: frozenset[IncrementalStatus] = frozenset({
    IncrementalStatus.REINDEXED,
    IncrementalStatus.DELETED,
})


class IncrementalIndexer:
    """Executes isolated per-file reindex/delete updates and single-pass batch linking."""

    def __init__(
        self,
        config: AppConfig | None = None,
        scanner: RepoScanner | None = None,
        pipeline: AstExtractionPipeline | None = None,
        link_coordinator: RepoLinkCoordinator | None = None,
    ) -> None:
        """Initialize the incremental indexer and its static analysis collaborators."""
        resolved_config = config if config is not None else AppConfig()
        self._config = resolved_config
        self._scanner = scanner if scanner is not None else RepoScanner(config=resolved_config)
        self._pipeline = (
            pipeline if pipeline is not None else AstExtractionPipeline(config=resolved_config)
        )
        self._link_coordinator = (
            link_coordinator if link_coordinator is not None else RepoLinkCoordinator()
        )

    def _resolve_reindex_target(
        self,
        raw_root: Path,
        resolved_root: Path,
        file_path: Path | str,
    ) -> tuple[Path, str] | IncrementalFileResult:
        """Validate path confinement and extension/ignore filters for a changed file."""
        display_path = str(file_path)
        try:
            safe_path = ensure_safe_repo_file(raw_root, file_path)
        except PathSecurityError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                logger.info("Skipping missing file during incremental reindex: %s", file_path)
                return IncrementalFileResult(
                    file_path=display_path,
                    status=IncrementalStatus.SKIPPED,
                )
            logger.warning("Path security check failed during reindex for %s: %s", file_path, exc)
            return IncrementalFileResult(
                file_path=display_path,
                status=IncrementalStatus.ERROR,
                error_message=str(exc),
            )
        except OSError as exc:
            logger.warning(
                "OS error resolving file path during reindex for %s: %s",
                file_path,
                exc,
            )
            return IncrementalFileResult(
                file_path=display_path,
                status=IncrementalStatus.ERROR,
                error_message=str(exc),
            )

        repo_relative = to_relative_path(resolved_root, safe_path)
        is_skipped = not is_supported_source_file(
            safe_path,
            self._config.supported_extensions,
        ) or is_ignored_watch_path(
            repo_relative,
            self._config.ignored_dirs,
            include_tests=self._config.include_tests,
            test_dir_names=self._config.test_dir_names,
        )
        if is_skipped:
            logger.debug("Skipping ignored or unsupported file during reindex: %s", repo_relative)
            return IncrementalFileResult(file_path=repo_relative, status=IncrementalStatus.SKIPPED)
        return safe_path, repo_relative

    def _extract_file_graph(
        self,
        resolved_root: Path,
        safe_path: Path,
        repo_relative: str,
    ) -> tuple[str, ScannedFile, FileGraphExtraction] | IncrementalFileResult:
        """Scan and parse ``safe_path`` into a ``FileGraphExtraction`` in a single file read."""
        repo_id = derive_repo_id(resolved_root)
        try:
            scanned = self._scanner.scan_file_with_source(
                repo_id,
                resolved_root,
                safe_path,
                safe_path=safe_path,
            )
            extraction = self._pipeline.extract_file(
                resolved_root,
                scanned.record,
                source_text=scanned.source_text,
            )
            return repo_id, scanned, extraction
        except (ParseError, SyntaxError) as exc:
            logger.warning(
                "Parse failure reindexing %s; keeping previous graph: %s",
                repo_relative,
                exc,
            )
            return IncrementalFileResult(
                file_path=repo_relative,
                status=IncrementalStatus.PARSE_FAILED,
                error_message=str(exc),
            )
        except (FileNotFoundError, PathSecurityError) as exc:
            if isinstance(exc, FileNotFoundError) or isinstance(exc.__cause__, FileNotFoundError):
                logger.info("File disappeared before AST extraction: %s", repo_relative)
                return IncrementalFileResult(
                    file_path=repo_relative,
                    status=IncrementalStatus.SKIPPED,
                )
            logger.warning("Security error extracting %s: %s", repo_relative, exc)
            return IncrementalFileResult(
                file_path=repo_relative,
                status=IncrementalStatus.ERROR,
                error_message=str(exc),
            )
        except OSError as exc:
            logger.warning("OS error extracting %s: %s", repo_relative, exc)
            return IncrementalFileResult(
                file_path=repo_relative,
                status=IncrementalStatus.ERROR,
                error_message=str(exc),
            )

    def _ensure_repo_record(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        resolved_root: Path,
        last_indexed_at: str | None,
    ) -> None:
        """Ensure ``repo_id`` exists in ``uow.repos`` and update ``last_indexed_at``."""
        existing = uow.repos.get_by_id(repo_id)
        if existing is None:
            uow.repos.upsert(self._scanner.build_repo_record(resolved_root))
        elif last_indexed_at is not None:
            uow.repos.upsert(
                build_repo_upsert_record(existing, existing, last_indexed_at=last_indexed_at)
            )

    def _sync_preloaded_or_disk_text(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        resolved_root: Path,
        file_path: Path | str,
        scanned: ScannedFile | None = None,
    ) -> bool:
        """Upsert one UTF-8 repository text document using ``scanned`` when preloaded."""
        if scanned is not None and scanned.source_text is not None:
            rec = scanned.record
            doc = build_text_document_from_source(
                rec.id,
                repo_id,
                rec.file_path,
                scanned.source_text,
                meta=(rec.content_hash, rec.size_bytes, scanned.mtime_ns, rec.is_test),
            )
            uow.text_search.upsert_many([doc])
            return True

        doc = read_text_document(
            repo_id,
            resolved_root,
            file_path,
            max_file_size_bytes=self._config.max_file_size_bytes,
            test_dir_names=self._config.test_dir_names,
        )
        if doc is None:
            return False
        self._ensure_repo_record(uow, repo_id, resolved_root, None)
        uow.text_search.upsert_many([doc])
        return True

    def _persist_reindexed_file(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        resolved_root: Path,
        scanned: ScannedFile,
        extraction: FileGraphExtraction,
        *,
        run_linker: bool,
    ) -> IncrementalFileResult:
        """Persist ``extraction`` inside a savepoint and optionally run repository linking."""
        file_id = extraction.file.id
        repo_relative = extraction.file.file_path
        try:
            with uow.savepoint():
                self._ensure_repo_record(
                    uow,
                    repo_id,
                    resolved_root,
                    extraction.file.last_indexed_at,
                )
                invalidated_refs = uow.refresh.invalidate_for_file(file_id)
                uow.replace_file_graph(extraction.file, extraction.nodes, extraction.edges)
                self._sync_preloaded_or_disk_text(
                    uow,
                    repo_id,
                    resolved_root,
                    repo_relative,
                    scanned,
                )
                invalidated_targets = uow.refresh.mark_file_symbols_stale(file_id)
                if run_linker:
                    link_report = self._link_coordinator.link_repository(uow, repo_id)
                    resolved_count = link_report.total_resolved
                    reset_count = link_report.edges_reset
                else:
                    resolved_count = 0
                    reset_count = 0

            if run_linker:
                uow.commit()
        except IndexerError as exc:
            logger.error("Failed persisting reindex update for %s: %s", repo_relative, exc)
            return IncrementalFileResult(
                file_path=repo_relative,
                status=IncrementalStatus.ERROR,
                error_message=str(exc),
            )

        return IncrementalFileResult(
            file_path=repo_relative,
            status=IncrementalStatus.REINDEXED,
            node_count=len(extraction.nodes),
            edge_count=len(extraction.edges),
            invalidated_reference_edge_count=invalidated_refs,
            invalidated_target_symbol_count=len(invalidated_targets),
            resolved_edge_count=resolved_count,
            reset_edge_count=reset_count,
        )

    def reindex_file(
        self,
        uow: UnitOfWorkProtocol,
        repo_root: Path | str,
        file_path: Path | str,
        *,
        run_linker: bool = True,
    ) -> IncrementalFileResult:
        """Reindex a created or modified source file inside ``repo_root``."""
        raw_root = Path(repo_root)
        resolved_root = raw_root.resolve()

        target = self._resolve_reindex_target(raw_root, resolved_root, file_path)
        if isinstance(target, IncrementalFileResult):
            if target.status == IncrementalStatus.SKIPPED:
                synced = self._sync_preloaded_or_disk_text(
                    uow,
                    derive_repo_id(resolved_root),
                    resolved_root,
                    file_path,
                )
                if synced and run_linker:
                    uow.commit()
            return target
        safe_path, repo_relative = target

        extracted = self._extract_file_graph(resolved_root, safe_path, repo_relative)
        if isinstance(extracted, IncrementalFileResult):
            return extracted
        repo_id, scanned, extraction = extracted

        return self._persist_reindexed_file(
            uow,
            repo_id,
            resolved_root,
            scanned,
            extraction,
            run_linker=run_linker,
        )

    @staticmethod
    def _collect_external_targets(
        uow: UnitOfWorkProtocol,
        file_id: str,
        declared_ids: set[str],
    ) -> list[str]:
        """Collect external target symbol IDs referenced or called from ``file_id``."""
        impacted_ids = set(uow.refresh.collect_impacted_symbol_ids(file_id))
        for edge in uow.edges.list_for_file(file_id):
            if edge.to_id:
                impacted_ids.add(edge.to_id)
        return sorted(tid for tid in impacted_ids if tid not in declared_ids)

    def _persist_deleted_file(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        file_id: str,
        repo_relative_path: str,
        *,
        run_linker: bool,
    ) -> IncrementalFileResult:
        """Delete ``file_id`` non-destructively so surviving callers' AST edges reset cleanly."""
        try:
            with uow.savepoint():
                declared_ids = {node.id for node in uow.nodes.list_for_file(file_id)}
                external_targets = self._collect_external_targets(uow, file_id, declared_ids)
                marked_external = uow.refresh.mark_symbols_stale(external_targets)
                deleted_lsp_edges = uow.edges.delete_lsp_references_for_file(file_id)
                deleted_owned_edges = uow.edges.delete_owned_for_file(file_id)
                uow.files.delete_file_graph(file_id, preserve_incoming_ast_edges=True)

                if run_linker:
                    link_report = self._link_coordinator.link_repository(uow, repo_id)
                    resolved_count = link_report.total_resolved
                    reset_count = link_report.edges_reset
                else:
                    resolved_count = 0
                    reset_count = 0

            if run_linker:
                uow.commit()
        except IndexerError as exc:
            logger.error(
                "Failed deleting file graph for %s (%s): %s",
                repo_relative_path,
                file_id,
                exc,
            )
            return IncrementalFileResult(
                file_path=repo_relative_path,
                status=IncrementalStatus.ERROR,
                error_message=str(exc),
            )

        return IncrementalFileResult(
            file_path=repo_relative_path,
            status=IncrementalStatus.DELETED,
            deleted_node_count=len(declared_ids),
            deleted_edge_count=deleted_lsp_edges + deleted_owned_edges,
            invalidated_reference_edge_count=deleted_lsp_edges,
            invalidated_target_symbol_count=len(marked_external),
            resolved_edge_count=resolved_count,
            reset_edge_count=reset_count,
        )

    def delete_file(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        repo_relative_path: str,
        *,
        run_linker: bool = True,
    ) -> IncrementalFileResult:
        """Delete a removed file's symbols, owned edges, and stale LSP references."""
        deleted_text = uow.text_search.delete_by_paths(repo_id, [repo_relative_path])
        file_rec = uow.files.get_by_path(repo_id, repo_relative_path)
        if file_rec is None:
            file_rec = uow.files.get_by_id(derive_file_id(repo_id, repo_relative_path))
        if file_rec is None:
            if deleted_text > 0 and run_linker:
                uow.commit()
            logger.debug(
                "Deleted file %s is not tracked in repository %s",
                repo_relative_path,
                repo_id,
            )
            return IncrementalFileResult(
                file_path=repo_relative_path,
                status=IncrementalStatus.NOT_TRACKED,
            )

        return self._persist_deleted_file(
            uow,
            repo_id,
            file_rec.id,
            repo_relative_path,
            run_linker=run_linker,
        )

    def _process_single_event_isolated(
        self,
        uow: UnitOfWorkProtocol,
        resolved_root: Path,
        default_repo_id: str,
        event: FileChangeEvent,
    ) -> IncrementalFileResult:
        """Execute one collapsed ``FileChangeEvent`` isolated inside ``uow.savepoint()``."""
        try:
            with uow.savepoint():
                if event.event_type == FileEventType.DELETED:
                    target_repo_id = event.repo_id or default_repo_id
                    return self.delete_file(
                        uow,
                        target_repo_id,
                        event.repo_relative_path,
                        run_linker=False,
                    )
                return self.reindex_file(
                    uow,
                    resolved_root,
                    event.absolute_path,
                    run_linker=False,
                )
        except Exception as exc:
            logger.exception(
                "Unhandled error processing incremental event for %s",
                event.repo_relative_path,
            )
            return IncrementalFileResult(
                file_path=event.repo_relative_path,
                status=IncrementalStatus.ERROR,
                error_message=str(exc),
            )

    def process_batch(
        self,
        uow: UnitOfWorkProtocol,
        repo_root: Path | str,
        events: Sequence[FileChangeEvent],
        *,
        already_collapsed: bool = False,
    ) -> IncrementalBatchResult:
        """Collapse ``events``, isolate per-file updates, link once, and commit ``uow``."""
        resolved_root = Path(repo_root).resolve()
        repo_id = derive_repo_id(resolved_root)
        collapsed = list(events) if already_collapsed else collapse_events(events)

        results: list[IncrementalFileResult] = [
            self._process_single_event_isolated(uow, resolved_root, repo_id, event)
            for event in collapsed
        ]
        mutated_indices = [
            idx for idx, item in enumerate(results) if item.status in _MUTATED_STATUSES
        ]

        link_report: LinkResolutionReport | None = None
        dangling_deleted = 0
        if mutated_indices:
            link_report = self._link_coordinator.link_repository(uow, repo_id)
            dangling_deleted = link_report.dangling_lsp_deleted
            last_idx = mutated_indices[-1]
            results[last_idx] = results[last_idx].model_copy(
                update={
                    "resolved_edge_count": link_report.total_resolved,
                    "reset_edge_count": link_report.edges_reset,
                }
            )

        uow.commit()
        return IncrementalBatchResult(
            repo_id=repo_id,
            results=results,
            link_report=link_report,
            dangling_lsp_edges_deleted=dangling_deleted,
        )


__all__ = [
    "IncrementalIndexer",
]
