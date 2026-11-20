"""Hybrid FTS5 trigram + live-disk reconciliation full-codebase search service."""

import re
from pathlib import Path

from context.utils.trigram import (
    compile_search_pattern,
    extract_required_ascii_trigrams,
    match_document_lines,
)
from core.models.config import AppConfig
from core.models.enums import SymbolKind
from core.models.node import SymbolNode
from core.models.repo import RepoRecord
from core.models.search import (
    CodeMatchItem,
    CodeSearchQuery,
    CodeSearchReport,
    CodeSearchStats,
    FileMatchCount,
    TextCandidateQuery,
    TextDocumentMeta,
    TextDocumentRecord,
)
from core.ports.uow import UnitOfWorkProtocol
from core.utils.logging import get_logger
from core.utils.pathing import derive_file_id, normalize_rel_path_prefix
from core.utils.text_files import read_text_document, walk_repo_text_stats

logger = get_logger("context.services.code_search")


class CodeSearchService:
    """Executes zero-miss full-codebase literal/regex searches with Python symbol attribution."""

    def __init__(self, config: AppConfig | None = None) -> None:
        """Initialize the code search service with optional runtime configuration."""
        self._config = config if config is not None else AppConfig()

    @staticmethod
    def _matches_doc_filters(doc: TextDocumentRecord, query: CodeSearchQuery) -> bool:
        """Return ``True`` when ``doc`` satisfies test, prefix, and language filters."""
        if not query.include_tests and doc.is_test:
            return False
        if query.language and doc.language != query.language.strip().lower():
            return False
        clean_prefix = normalize_rel_path_prefix(query.path_prefix)
        return not (clean_prefix and not doc.file_path.startswith(clean_prefix))

    @staticmethod
    def _is_stat_unchanged(prev: TextDocumentMeta | None, mtime_ns: int, size_bytes: int) -> bool:
        """Return ``True`` when indexed metadata matches on-disk ``(mtime_ns, size_bytes)``."""
        if prev is None or prev.mtime_ns <= 0:
            return False
        return prev.mtime_ns == mtime_ns and prev.size_bytes == size_bytes

    def _should_load_dirty_stat(
        self,
        is_test: bool,
        file_path: str,
        include_tests: bool,
        clean_prefix: str,
    ) -> bool:
        """Return ``True`` when a dirty on-disk file passes fast stat-level query filters."""
        if not include_tests and is_test:
            return False
        return not (clean_prefix and not file_path.startswith(clean_prefix))

    def _collect_live_overlay(
        self,
        repo: RepoRecord,
        indexed_by_path: dict[str, TextDocumentMeta],
        query: CodeSearchQuery,
    ) -> tuple[dict[str, TextDocumentRecord], set[str], set[str]]:
        """Detect unindexed, modified, or deleted disk files (~5.5ms) and load dirty texts."""
        root_path = Path(repo.root_path)
        if not query.verify_disk or not root_path.is_dir():
            return {}, set(), set()

        disk_stats = walk_repo_text_stats(
            root_path,
            max_file_size_bytes=self._config.max_file_size_bytes,
            test_dir_names=self._config.test_dir_names,
        )
        deleted_paths = set(indexed_by_path) - {st.file_path for st in disk_stats}
        clean_prefix = normalize_rel_path_prefix(query.path_prefix)
        dirty_paths: set[str] = set()
        live_docs: dict[str, TextDocumentRecord] = {}

        for st in disk_stats:
            prev = indexed_by_path.get(st.file_path)
            if self._is_stat_unchanged(prev, st.mtime_ns, st.size_bytes):
                continue
            dirty_paths.add(st.file_path)
            if not self._should_load_dirty_stat(
                st.is_test, st.file_path, query.include_tests, clean_prefix
            ):
                continue
            doc = read_text_document(
                repo.id,
                root_path,
                st.file_path,
                max_file_size_bytes=self._config.max_file_size_bytes,
                test_dir_names=self._config.test_dir_names,
            )
            if doc is not None and self._matches_doc_filters(doc, query):
                live_docs[doc.file_path] = doc

        return live_docs, dirty_paths, deleted_paths

    @staticmethod
    def _find_enclosing_symbol(nodes: list[SymbolNode], line: int) -> SymbolNode | None:
        """Return the innermost non-module ``SymbolNode`` spanning ``line``."""
        best: SymbolNode | None = None
        best_span = 10**9
        best_start = -1
        for node in nodes:
            if node.kind == SymbolKind.MODULE:
                continue
            start_line = node.start_line
            end_line = node.end_line
            if start_line is None or end_line is None:
                continue
            if start_line <= line <= end_line:
                span = end_line - start_line
                if span < best_span or (span == best_span and start_line > best_start):
                    best = node
                    best_span = span
                    best_start = start_line
        return best

    def _enrich_page_symbols(
        self,
        uow: UnitOfWorkProtocol,
        repo: RepoRecord,
        page_items: list[CodeMatchItem],
    ) -> list[CodeMatchItem]:
        """Attach Cmd+Click ``location`` and innermost enclosing Python symbol metadata."""
        nodes_cache: dict[str, list[SymbolNode]] = {}
        enriched: list[CodeMatchItem] = []
        root_path = Path(repo.root_path)

        for item in page_items:
            match_loc = f"{root_path / item.file_path}:{item.line}:{item.column}"
            if item.language != "python":
                enriched.append(item.model_copy(update={"location": match_loc}))
                continue
            if item.file_path not in nodes_cache:
                file_id = derive_file_id(repo.id, item.file_path)
                nodes_cache[item.file_path] = uow.nodes.list_for_file(file_id)
            sym = self._find_enclosing_symbol(nodes_cache[item.file_path], item.line)
            if sym is None:
                enriched.append(item.model_copy(update={"location": match_loc}))
                continue
            enriched.append(
                item.model_copy(
                    update={
                        "location": match_loc,
                        "symbol_id": sym.id,
                        "qualified_name": sym.qualified_name,
                        "symbol_kind": str(sym.kind),
                        "symbol_lines": f"{sym.start_line}-{sym.end_line}",
                        "symbol_location": sym.location,
                    }
                )
            )
        return enriched

    @staticmethod
    def _scan_documents(
        merged_by_path: dict[str, TextDocumentRecord],
        pattern: re.Pattern[str],
        context_lines: int,
    ) -> tuple[list[FileMatchCount], list[CodeMatchItem]]:
        """Run ``pattern`` across sorted ``merged_by_path`` documents."""
        file_counts: list[FileMatchCount] = []
        all_matches: list[CodeMatchItem] = []
        for file_path in sorted(merged_by_path):
            doc = merged_by_path[file_path]
            doc_hits = match_document_lines(doc, pattern, context_lines=context_lines)
            if doc_hits:
                file_counts.append(
                    FileMatchCount(
                        file_path=doc.file_path,
                        language=doc.language,
                        count=len(doc_hits),
                    )
                )
                all_matches.extend(doc_hits)
        return file_counts, all_matches

    def search(
        self,
        uow: UnitOfWorkProtocol,
        repo: RepoRecord,
        query: CodeSearchQuery,
    ) -> CodeSearchReport:
        """Execute hybrid trigram + live-disk search and return ``CodeSearchReport``."""
        pattern = compile_search_pattern(
            query.query,
            regex=query.regex,
            case_sensitive=query.case_sensitive,
        )
        trigrams = extract_required_ascii_trigrams(
            query.query,
            regex=query.regex,
            case_sensitive=query.case_sensitive,
        )
        indexed_meta = {
            meta.file_path: meta for meta in uow.text_search.list_metadata_for_repo(repo.id)
        }
        live_docs, dirty_paths, deleted_paths = self._collect_live_overlay(
            repo,
            indexed_meta,
            query,
        )
        db_candidates = uow.text_search.query_candidates(
            TextCandidateQuery(
                repo_id=repo.id,
                required_trigrams=trigrams,
                path_prefix=query.path_prefix,
                language=query.language,
                include_tests=query.include_tests,
            )
        )
        merged_by_path: dict[str, TextDocumentRecord] = {
            doc.file_path: doc
            for doc in db_candidates
            if doc.file_path not in deleted_paths and doc.file_path not in dirty_paths
        }
        merged_by_path.update(live_docs)

        file_counts, all_matches = self._scan_documents(
            merged_by_path,
            pattern,
            query.context_lines,
        )
        enriched_page = self._enrich_page_symbols(
            uow,
            repo,
            all_matches[query.offset : query.offset + query.limit],
        )
        return CodeSearchReport(
            repo_id=repo.id,
            query=query.query,
            regex=query.regex,
            case_sensitive=query.case_sensitive,
            total_matches=len(all_matches),
            matched_file_count=len(file_counts),
            returned_count=len(enriched_page),
            offset=query.offset,
            limit=query.limit,
            has_more=(query.offset + len(enriched_page)) < len(all_matches),
            file_counts=file_counts,
            matches=enriched_page,
            stats=CodeSearchStats(
                indexed_files=len(indexed_meta),
                candidate_files_scanned=len(merged_by_path),
                trigram_pruned=bool(trigrams),
                live_overlay_files=len(live_docs),
                deleted_overlay_files=len(deleted_paths),
            ),
        )


__all__ = [
    "CodeSearchService",
]
