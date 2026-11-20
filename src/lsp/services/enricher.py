"""Port-driven LSP symbol reference enrichment orchestrator."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.models.config import AppConfig
from core.models.edge import Edge
from core.models.enums import EdgeKind, RefreshStatus
from core.models.exceptions import LspError, PathSecurityError
from core.models.file import FileRecord
from core.models.node import SymbolNode, SymbolSummary
from core.models.runs import ReferenceRefreshState
from core.ports.lsp import LspClientProtocol
from core.ports.uow import UnitOfWorkProtocol
from core.utils.logging import get_logger
from core.utils.pathing import ensure_safe_repo_file
from core.utils.timestamps import utc_now_iso
from lsp.models.enrichment import (
    ReferenceMappingCache,
    SymbolEnrichmentResult,
    TargetDocumentSpec,
)
from lsp.utils.locations import (
    build_reference_edge,
    find_module_node_for_file,
    get_reference_query_position,
    normalize_location,
    pick_smallest_containing_symbol,
    read_bounded_lsp_text,
)
from lsp.utils.utf16 import position_codepoint_to_utf16, range_utf16_to_codepoint

logger = get_logger("lsp.services.enricher")


class ReferenceEnricher:
    """Orchestrates Pyright reference discovery and persistence via Hexagonal ports."""

    def __init__(self, config: AppConfig | None = None) -> None:
        """Initialize the enricher with runtime file-size bounds.

        Args:
            config: Optional ``AppConfig`` instance; defaults to ``AppConfig()``.
        """
        self._config: AppConfig = config or AppConfig()

    def enrich_symbol(
        self,
        uow: UnitOfWorkProtocol,
        lsp_client: LspClientProtocol,
        target_symbol: SymbolSummary | SymbolNode,
        *,
        open_all_files: bool = False,
        raise_on_error: bool = False,
    ) -> SymbolEnrichmentResult:
        """Query LSP references for ``target_symbol`` and persist deduplicated ``Edge`` rows.

        ``PathSecurityError`` always propagates immediately. On ``LspError``, logs a
        warning, records ``RefreshStatus.UNAVAILABLE`` in ``uow.refresh``, and either
        returns an unavailable ``SymbolEnrichmentResult`` or re-raises when
        ``raise_on_error=True``.

        Args:
            uow: Active ``UnitOfWorkProtocol`` providing repository ports.
            lsp_client: ``LspClientProtocol`` session used to query ``textDocument/references``.
            target_symbol: Target ``SymbolSummary`` or ``SymbolNode`` to enrich.
            open_all_files: When ``True``, opens all tracked ``.py`` files in the repo first.
            raise_on_error: When ``True``, re-raises ``LspError`` after recording refresh state.

        Returns:
            ``SymbolEnrichmentResult`` summarizing reference fanout and refresh status.

        Raises:
            PathSecurityError: If the target file escapes ``repo_root`` or traverses symlinks.
            LspError: If ``raise_on_error=True`` and an LSP or target file error occurs.
        """
        try:
            return self._execute_enrichment(
                uow,
                lsp_client,
                target_symbol,
                open_all_files=open_all_files,
            )
        except LspError as exc:
            logger.warning(
                "LSP reference enrichment unavailable for symbol %s (code=%s): %s",
                target_symbol.id,
                exc.error_code,
                exc,
            )
            return self._handle_enrichment_error(
                uow,
                target_symbol.id,
                exc,
                raise_on_error=raise_on_error,
            )

    def _execute_enrichment(
        self,
        uow: UnitOfWorkProtocol,
        lsp_client: LspClientProtocol,
        target_symbol: SymbolSummary | SymbolNode,
        *,
        open_all_files: bool,
    ) -> SymbolEnrichmentResult:
        """Run document preparation, LSP query, location mapping, and UoW persistence.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            lsp_client: Active or unstarted ``LspClientProtocol``.
            target_symbol: Target symbol summary or node.
            open_all_files: Whether to synchronize auxiliary ``.py`` files beforehand.

        Returns:
            Completed ``SymbolEnrichmentResult`` with ``RefreshStatus.OK``.
        """
        target_node = self._to_symbol_node(target_symbol)
        doc_spec = self._prepare_target_document(uow, target_symbol, target_node)

        lsp_client.start(doc_spec.repo_root)
        lsp_client.did_open_or_change(doc_spec.target_uri, doc_spec.target_text)
        if open_all_files:
            self._open_auxiliary_python_files(uow, lsp_client, target_node.repo_id, doc_spec)

        raw_locations = lsp_client.find_references(
            doc_spec.target_uri,
            doc_spec.utf16_position,
            include_declaration=False,
        )
        edges = self._map_locations_to_edges(uow, raw_locations, target_node, doc_spec)
        return self._persist_and_summarize(uow, target_node, edges)

    @staticmethod
    def _handle_enrichment_error(
        uow: UnitOfWorkProtocol,
        target_symbol_id: str,
        exc: LspError,
        *,
        raise_on_error: bool,
    ) -> SymbolEnrichmentResult:
        """Record ``UNAVAILABLE`` refresh state and return or re-raise ``exc``.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            target_symbol_id: Identifier of the target symbol that failed enrichment.
            exc: Caught ``LspError`` instance.
            raise_on_error: Whether to re-raise ``exc`` after persisting refresh state.

        Returns:
            Unavailable ``SymbolEnrichmentResult`` when ``raise_on_error=False``.

        Raises:
            LspError: Re-raised when ``raise_on_error=True``.
        """
        refreshed_at = utc_now_iso()
        uow.refresh.upsert(
            ReferenceRefreshState(
                target_symbol_id=target_symbol_id,
                available=False,
                last_refreshed_at=refreshed_at,
                refresh_status=RefreshStatus.UNAVAILABLE,
                error_code=exc.error_code,
            )
        )
        if raise_on_error:
            raise exc
        return SymbolEnrichmentResult(
            target_symbol_id=target_symbol_id,
            available=False,
            reference_count=0,
            referencing_file_count=0,
            referencing_module_count=0,
            last_refreshed_at=refreshed_at,
            refresh_status=RefreshStatus.UNAVAILABLE,
            error_code=exc.error_code,
            error_message=exc.message,
        )

    @staticmethod
    def _to_symbol_node(target_symbol: SymbolSummary | SymbolNode) -> SymbolNode:
        """Coerce a ``SymbolSummary`` or ``SymbolNode`` into a canonical ``SymbolNode``.

        Args:
            target_symbol: Input ``SymbolSummary`` or ``SymbolNode``.

        Returns:
            ``SymbolNode`` preserving range, kind, and scope metadata.
        """
        if isinstance(target_symbol, SymbolNode):
            return target_symbol
        return SymbolNode(
            id=target_symbol.id,
            repo_id=target_symbol.repo_id,
            file_id=target_symbol.file_id,
            language=target_symbol.language,
            kind=target_symbol.kind,
            name=target_symbol.name,
            qualified_name=target_symbol.qualified_name,
            uri=target_symbol.uri,
            content_hash=target_symbol.content_hash,
            confidence=target_symbol.confidence,
            decl_range=target_symbol.decl_range,
            selection_range=target_symbol.selection_range,
            start_line=target_symbol.start_line,
            end_line=target_symbol.end_line,
            signature=target_symbol.signature,
            parent_id=target_symbol.parent_id,
            visibility_hint=target_symbol.visibility_hint,
            doc_summary=target_symbol.doc_summary,
            scope=target_symbol.scope,
            lexical_parent_id=target_symbol.lexical_parent_id,
            last_indexed_at=target_symbol.last_indexed_at,
        )

    def _prepare_target_document(
        self,
        uow: UnitOfWorkProtocol,
        target_symbol: SymbolSummary | SymbolNode,
        target_node: SymbolNode,
    ) -> TargetDocumentSpec:
        """Resolve ``repo_root`` and ``file_path``, read text, and compute UTF-16 position.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            target_symbol: Original ``SymbolSummary`` or ``SymbolNode``.
            target_node: Coerced ``SymbolNode``.

        Returns:
            Validated ``TargetDocumentSpec`` instance.

        Raises:
            PathSecurityError: If the target file violates repository confinement.
            LspError: If metadata, repository directory, range, or file reading fails.
        """
        codepoint_pos = get_reference_query_position(target_node)
        repo_root = self._resolve_repo_root(uow, target_node)
        rel_file_path = self._resolve_target_file_path(uow, target_symbol)

        target_file_path = ensure_safe_repo_file(repo_root, repo_root / rel_file_path)
        target_text = self._read_safe_repo_text(
            target_file_path,
            self._config.max_file_size_bytes,
        )
        target_lines = target_text.splitlines()
        target_uri = target_node.uri or target_file_path.as_uri()
        utf16_position = position_codepoint_to_utf16(target_lines, codepoint_pos)

        return TargetDocumentSpec(
            repo_root=repo_root,
            target_file_path=target_file_path,
            target_uri=target_uri,
            target_text=target_text,
            target_lines=target_lines,
            utf16_position=utf16_position,
        )

    @staticmethod
    def _resolve_repo_root(uow: UnitOfWorkProtocol, target_node: SymbolNode) -> Path:
        """Resolve and verify the on-disk repository root directory for ``target_node``.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            target_node: Target ``SymbolNode``.

        Returns:
            Resolved ``Path`` to the repository root directory.

        Raises:
            LspError: If the repository record is absent or its directory is missing.
        """
        repo_record = uow.repos.get_by_id(target_node.repo_id)
        if repo_record is None or not repo_record.root_path:
            raise LspError(
                f"Cannot determine repo_root for symbol {target_node.id}.",
                context={"symbol_id": target_node.id, "repo_id": target_node.repo_id},
            )
        repo_root = Path(repo_record.root_path).resolve()
        if not repo_root.is_dir():
            raise LspError(
                f"Repository root directory does not exist: {repo_root}",
                context={"repo_root": str(repo_root), "symbol_id": target_node.id},
            )
        return repo_root

    @staticmethod
    def _resolve_target_file_path(
        uow: UnitOfWorkProtocol,
        target_symbol: SymbolSummary | SymbolNode,
    ) -> str:
        """Resolve the relative POSIX ``file_path`` for ``target_symbol``.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            target_symbol: Input ``SymbolSummary`` or ``SymbolNode``.

        Returns:
            Non-empty repository-relative POSIX file path string.

        Raises:
            LspError: If the file record is missing or has an empty ``file_path``.
        """
        if isinstance(target_symbol, SymbolSummary) and target_symbol.file_path:
            return target_symbol.file_path

        file_record = uow.files.get_by_id(target_symbol.file_id)
        if file_record is None or not file_record.file_path:
            raise LspError(
                f"Target symbol {target_symbol.id} is missing file_path.",
                context={"symbol_id": target_symbol.id, "file_id": target_symbol.file_id},
            )
        return file_record.file_path

    @staticmethod
    def _read_safe_repo_text(file_path: Path, max_bytes: int) -> str:
        """Read bounded UTF-8 text from an already confined repository ``file_path``."""
        return read_bounded_lsp_text(file_path, max_bytes)

    def _open_auxiliary_python_files(
        self,
        uow: UnitOfWorkProtocol,
        lsp_client: LspClientProtocol,
        repo_id: str,
        doc_spec: TargetDocumentSpec,
    ) -> None:
        """Open all tracked ``.py`` files in ``repo_id`` other than ``doc_spec.target_file_path``.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            lsp_client: Active ``LspClientProtocol``.
            repo_id: Owning repository identifier.
            doc_spec: Prepared ``TargetDocumentSpec`` for the primary file.
        """
        max_bytes = self._config.max_file_size_bytes
        for file_rec in uow.files.list_for_repo(repo_id, include_tests=True):
            if not file_rec.file_path.endswith(".py"):
                continue
            try:
                safe_aux = ensure_safe_repo_file(
                    doc_spec.repo_root,
                    doc_spec.repo_root / file_rec.file_path,
                )
                if safe_aux == doc_spec.target_file_path:
                    continue
                aux_text = self._read_safe_repo_text(safe_aux, max_bytes)
                lsp_client.did_open_or_change(file_rec.uri, aux_text)
            except (PathSecurityError, LspError, OSError) as exc:
                logger.debug(
                    "Skipping auxiliary file %s during didOpen: %s",
                    file_rec.file_path,
                    exc,
                )

    def _map_locations_to_edges(
        self,
        uow: UnitOfWorkProtocol,
        raw_locations: Sequence[Mapping[str, Any]],
        target_node: SymbolNode,
        doc_spec: TargetDocumentSpec,
    ) -> list[Edge]:
        """Map raw LSP locations into deduplicated ``Edge`` entities.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            raw_locations: Raw ``Location`` / ``LocationLink`` dicts from the server.
            target_node: Enriched target ``SymbolNode``.
            doc_spec: Prepared ``TargetDocumentSpec``.

        Returns:
            Ordered list of deduplicated ``Edge`` models.
        """
        cache = ReferenceMappingCache(
            repo_id=target_node.repo_id,
            repo_root=doc_spec.repo_root,
            max_file_size_bytes=self._config.max_file_size_bytes,
            target_symbol=target_node,
            file_lines={target_node.file_id: doc_spec.target_lines},
        )
        edges: list[Edge] = []
        for raw_loc in raw_locations:
            edge = self._map_single_location(uow, raw_loc, cache)
            if edge is not None:
                edges.append(edge)
        return edges

    def _map_single_location(
        self,
        uow: UnitOfWorkProtocol,
        raw_loc: Mapping[str, Any],
        cache: ReferenceMappingCache,
    ) -> Edge | None:
        """Convert one raw LSP location into a deduplicated ``Edge``, or ``None``.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            raw_loc: Single raw LSP location mapping.
            cache: Mutable per-enrichment lookup and deduplication cache.

        Returns:
            Constructed ``Edge`` or ``None`` if unmapped or duplicate.
        """
        normalized = normalize_location(raw_loc)
        if normalized is None or not normalized.uri:
            return None

        resolved = self._resolve_cached_file_symbols(uow, normalized.uri, cache)
        if resolved is None:
            return None
        file_rec, symbols = resolved

        usage_range = normalized.range
        lines = self._load_cached_file_lines(file_rec, cache)
        if lines is not None:
            usage_range = range_utf16_to_codepoint(lines, usage_range)

        source_node = pick_smallest_containing_symbol(symbols, usage_range)
        if source_node is None:
            source_node = find_module_node_for_file(symbols)
        if source_node is None:
            return None

        dedupe_key = (
            cache.target_symbol.id,
            source_node.id,
            normalized.uri,
            usage_range.start.line,
            usage_range.start.character,
        )
        if dedupe_key in cache.seen_sites:
            return None
        cache.seen_sites.add(dedupe_key)

        return build_reference_edge(
            cache.repo_id,
            cache.target_symbol,
            source_node,
            file_rec.id,
            normalized.uri,
            usage_range,
        )

    @staticmethod
    def _resolve_cached_file_symbols(
        uow: UnitOfWorkProtocol,
        loc_uri: str,
        cache: ReferenceMappingCache,
    ) -> tuple[FileRecord, list[SymbolNode]] | None:
        """Resolve and memoize ``FileRecord`` and its ``SymbolNode`` list for ``loc_uri``.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            loc_uri: Referenced ``file://`` URI string.
            cache: Per-enrichment lookup cache.

        Returns:
            Tuple of ``(file_record, symbols)`` or ``None`` if URI is not tracked.
        """
        if loc_uri not in cache.files_by_uri:
            cache.files_by_uri[loc_uri] = uow.files.resolve_by_uri(
                loc_uri,
                repo_id=cache.repo_id,
            )
        file_rec = cache.files_by_uri[loc_uri]
        if file_rec is None:
            return None

        if file_rec.id not in cache.symbols_by_file:
            cache.symbols_by_file[file_rec.id] = uow.nodes.list_for_file(file_rec.id)
        return file_rec, cache.symbols_by_file[file_rec.id]

    def _load_cached_file_lines(
        self,
        file_rec: FileRecord,
        cache: ReferenceMappingCache,
    ) -> list[str] | None:
        """Load and memoize source lines for ``file_rec`` to convert UTF-16 ranges.

        Args:
            file_rec: Referenced ``FileRecord``.
            cache: Per-enrichment lookup cache.

        Returns:
            Split source lines, or ``None`` if the file is missing/inaccessible on disk.
        """
        if file_rec.id in cache.file_lines:
            return cache.file_lines[file_rec.id]

        if not file_rec.file_path:
            cache.file_lines[file_rec.id] = None
            return None

        try:
            safe_path = ensure_safe_repo_file(
                cache.repo_root,
                cache.repo_root / file_rec.file_path,
            )
            text = self._read_safe_repo_text(safe_path, cache.max_file_size_bytes)
            lines: list[str] | None = text.splitlines()
        except (PathSecurityError, LspError) as exc:
            logger.debug(
                "Unable to load source lines for UTF-16 range conversion on %s: %s",
                file_rec.file_path,
                exc,
            )
            lines = None

        cache.file_lines[file_rec.id] = lines
        return lines

    @staticmethod
    def _persist_and_summarize(
        uow: UnitOfWorkProtocol,
        target_node: SymbolNode,
        edges: Sequence[Edge],
    ) -> SymbolEnrichmentResult:
        """Persist replaced LSP edges and refresh state, then compute bucket statistics.

        Args:
            uow: Active ``UnitOfWorkProtocol``.
            target_node: Enriched target ``SymbolNode``.
            edges: Deduplicated LSP ``references`` edges to store.

        Returns:
            Populated ``SymbolEnrichmentResult`` with ``RefreshStatus.OK``.
        """
        refreshed_at = utc_now_iso()
        uow.edges.replace_lsp_references_for_target(
            target_node.repo_id,
            target_node.id,
            edges,
        )
        uow.refresh.upsert(
            ReferenceRefreshState(
                target_symbol_id=target_node.id,
                available=True,
                last_refreshed_at=refreshed_at,
                refresh_status=RefreshStatus.OK,
                error_code=None,
            )
        )
        stats = uow.edges.compute_bucket_stats(target_node.id, (EdgeKind.REFERENCES,))
        return SymbolEnrichmentResult(
            target_symbol_id=target_node.id,
            available=True,
            reference_count=stats.edge_count,
            referencing_file_count=stats.file_count,
            referencing_module_count=stats.module_count,
            last_refreshed_at=refreshed_at,
            refresh_status=RefreshStatus.OK,
            error_code=None,
            error_message=None,
        )
