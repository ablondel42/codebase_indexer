"""Paginated symbol reference inspection and LSP/AST fanout statistics service."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from core.models.config import AppConfig
from core.models.edge import DirectedEdgeQuery, Edge, SymbolReferenceStats
from core.models.enums import AST_DEPENDENCY_EDGE_KINDS, EdgeKind
from core.models.exceptions import ParseError, PathSecurityError, SymbolNotFoundError
from core.models.file import FileRecord
from core.models.node import SymbolSummary
from core.models.repo import RepoRecord
from core.ports.uow import UnitOfWorkProtocol
from core.utils.file_io import (
    extract_line_preview,
    format_line_span,
    read_verified_file_text,
)
from core.utils.logging import get_logger
from core.utils.pathing import format_edge_location
from graph.models.references import (
    ReferenceQueryOptions,
    ReferenceSiteEntry,
    ReferenceTargetSummary,
    SymbolReferencesReport,
)
from graph.utils.predicates import (
    classify_edge_peer,
    derive_target_lines,
    extract_edge_line,
    format_site_location,
    resolve_endpoint_name,
    resolve_peer_label,
)

logger = get_logger("graph.services.references")


class ReferenceAnalyzer:
    """Computes symbol reference statistics and paginated site reports with previews."""

    def __init__(self, config: AppConfig | None = None) -> None:
        """Initialize the analyzer with optional runtime configuration.

        Args:
            config: Optional ``AppConfig`` controlling maximum file size during previews.
        """
        self._config = config if config is not None else AppConfig()

    def build_stats(self, uow: UnitOfWorkProtocol, target_id: str) -> SymbolReferenceStats:
        """Compute reference fanout counts for ``target_id`` with LSP-first AST fallback.

        Uses persisted LSP ``references`` edges when refresh state is ``available`` or
        when stored LSP reference edges exist; otherwise falls back to AST dependency
        edges (``calls``, ``imports``, ``inherits``).

        Args:
            uow: Active read Unit of Work.
            target_id: Target symbol identifier.

        Returns:
            Populated ``SymbolReferenceStats`` model.
        """
        refresh_state = uow.refresh.get(target_id)
        if refresh_state.available or uow.edges.has_incoming_kind(target_id, EdgeKind.REFERENCES):
            reference_source = "lsp"
            kinds: tuple[EdgeKind, ...] = (EdgeKind.REFERENCES,)
        else:
            reference_source = "ast"
            kinds = AST_DEPENDENCY_EDGE_KINDS

        bucket = uow.edges.compute_bucket_stats(target_id, kinds)
        return SymbolReferenceStats(
            reference_count=bucket.edge_count,
            referencing_file_count=bucket.file_count,
            referencing_module_count=bucket.module_count,
            available=refresh_state.available,
            last_refreshed_at=refresh_state.last_refreshed_at,
            reference_source=reference_source,
        )

    @staticmethod
    def _resolve_repo_root(repo_record: RepoRecord | None) -> Path | None:
        """Return an existing repository root ``Path`` when available on disk.

        Args:
            repo_record: Owning ``RepoRecord`` or ``None``.

        Returns:
            Existing directory ``Path`` or ``None``.
        """
        if repo_record is not None and repo_record.root_path:
            candidate = Path(repo_record.root_path)
            if candidate.is_dir():
                return candidate
        return None

    def _load_cached_file_lines(
        self,
        repo_root: Path | None,
        ev_file: FileRecord | None,
        line_cache: dict[str, list[str] | None],
    ) -> list[str] | None:
        """Read and cache verified file lines once per ``file_id`` for source previews.

        Args:
            repo_root: Repository root directory or ``None``.
            ev_file: Evidence ``FileRecord`` or ``None``.
            line_cache: Per-call cache mapping ``file_id`` to split lines or ``None``.

        Returns:
            Cached list of source lines, or ``None`` if unavailable/unreadable.
        """
        if repo_root is None or ev_file is None:
            return None
        if ev_file.id in line_cache:
            return line_cache[ev_file.id]

        try:
            verified = read_verified_file_text(
                repo_root,
                ev_file.file_path,
                expected_content_hash=ev_file.content_hash,
                max_file_size_bytes=self._config.max_file_size_bytes,
            )
            lines: list[str] | None = verified.lines
        except (PathSecurityError, ParseError, OSError) as exc:
            logger.debug(
                "Skipping source preview for unreadable/missing file %s (file_id=%s): %s",
                ev_file.file_path,
                ev_file.id,
                exc,
            )
            lines = None

        line_cache[ev_file.id] = lines
        return lines

    def _build_site_entry(
        self,
        edge: Edge,
        target_id: str,
        summaries_by_id: Mapping[str, SymbolSummary],
        ev_file: FileRecord | None,
        preview: str | None,
    ) -> ReferenceSiteEntry:
        """Construct a ``ReferenceSiteEntry`` from an ``Edge`` and resolved metadata.

        Args:
            edge: Directed relationship ``Edge``.
            target_id: Inspected target symbol identifier.
            summaries_by_id: Endpoint ``SymbolSummary`` lookup map.
            ev_file: Evidence ``FileRecord`` or ``None``.
            preview: Optional 1-line trimmed source preview.

        Returns:
            Populated ``ReferenceSiteEntry``.
        """
        file_path = ev_file.file_path if ev_file is not None else None
        line_num = extract_edge_line(edge)
        col_num = (
            edge.evidence_range.start.character + 1 if edge.evidence_range is not None else None
        )
        site_loc = (
            edge.location
            or (
                format_edge_location(ev_file.uri, edge.evidence_range)
                if ev_file is not None
                else None
            )
            or format_site_location(file_path, line_num)
        )
        edge_dir, peer_id = classify_edge_peer(edge, target_id)
        return ReferenceSiteEntry(
            id=edge.id,
            direction=edge_dir,
            kind=edge.kind,
            from_id=edge.from_id,
            to_id=edge.to_id,
            peer_id=peer_id,
            peer_name=resolve_peer_label(peer_id, summaries_by_id, edge),
            from_name=resolve_endpoint_name(
                edge.from_id,
                summaries_by_id,
                edge,
                allow_payload_fallback=False,
            ),
            to_name=resolve_endpoint_name(
                edge.to_id,
                summaries_by_id,
                edge,
                allow_payload_fallback=True,
            ),
            file_path=file_path,
            line=line_num,
            column=col_num,
            location=site_loc,
            confidence=edge.confidence,
            source=edge.source,
            preview=preview,
        )

    def _build_entries(
        self,
        uow: UnitOfWorkProtocol,
        target: SymbolSummary,
        edges: Sequence[Edge],
        options: ReferenceQueryOptions,
    ) -> tuple[FileRecord | None, list[ReferenceSiteEntry]]:
        """Batch-resolve endpoints/files and build ``ReferenceSiteEntry`` items.

        Args:
            uow: Active read Unit of Work.
            target: Target ``SymbolSummary``.
            edges: Paginated directed ``Edge`` sequence.
            options: Active ``ReferenceQueryOptions``.

        Returns:
            Tuple ``(target_file_record, site_entries)``.
        """
        endpoint_ids = [eid for edge in edges for eid in (edge.from_id, edge.to_id)]
        summaries_by_id = uow.nodes.get_summaries_by_ids(endpoint_ids)
        file_ids = [
            target.file_id,
            *(edge.evidence_file_id for edge in edges if edge.evidence_file_id),
        ]
        files_by_id = uow.files.get_by_ids(file_ids)
        repo_root = (
            self._resolve_repo_root(uow.repos.get_by_id(target.repo_id))
            if options.include_previews
            else None
        )

        line_cache: dict[str, list[str] | None] = {}
        entries: list[ReferenceSiteEntry] = []
        for edge in edges:
            ev_file = files_by_id.get(edge.evidence_file_id or "")
            line_num = extract_edge_line(edge)
            preview: str | None = None
            if options.include_previews and line_num is not None:
                lines = self._load_cached_file_lines(repo_root, ev_file, line_cache)
                if lines:
                    preview = extract_line_preview(
                        lines,
                        line_num,
                        max_chars=options.max_preview_chars,
                    )
            entries.append(
                self._build_site_entry(edge, target.id, summaries_by_id, ev_file, preview)
            )
        return files_by_id.get(target.file_id), entries

    @staticmethod
    def _build_target_summary(
        target: SymbolSummary,
        target_file: FileRecord | None,
    ) -> ReferenceTargetSummary:
        """Build a ``ReferenceTargetSummary`` from ``target`` and optional ``target_file``.

        Args:
            target: Target ``SymbolSummary`` projection.
            target_file: Optional ``FileRecord`` for the target symbol.

        Returns:
            Populated ``ReferenceTargetSummary``.
        """
        start_line, end_line = derive_target_lines(
            target.start_line,
            target.end_line,
            target.decl_range,
        )
        file_path = (
            target_file.file_path
            if target_file is not None
            else (target.file_path if target.file_path else None)
        )
        module_path = (
            target_file.module_path
            if target_file is not None
            else (target.module_path if target.module_path else None)
        )
        return ReferenceTargetSummary(
            id=target.id,
            kind=target.kind,
            name=target.name,
            qualified_name=target.qualified_name,
            location=target.location,
            file_path=file_path,
            module_path=module_path,
            lines=format_line_span(start_line, end_line),
            signature=target.signature,
            doc_summary=target.doc_summary,
            scope=target.scope,
            visibility_hint=target.visibility_hint,
        )

    def inspect_references(
        self,
        uow: UnitOfWorkProtocol,
        symbol_id: str,
        options: ReferenceQueryOptions | None = None,
    ) -> SymbolReferencesReport:
        """Build a paginated ``SymbolReferencesReport`` with ``file:line`` previews.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Canonical target symbol identifier.
            options: Optional direction, kind filter, pagination, and preview settings.

        Returns:
            Populated ``SymbolReferencesReport``.

        Raises:
            SymbolNotFoundError: If ``symbol_id`` does not exist in the graph.
        """
        opts = options if options is not None else ReferenceQueryOptions()
        target = uow.nodes.get_summary_by_id(symbol_id)
        if target is None:
            raise SymbolNotFoundError(
                f"Symbol not found: {symbol_id}",
                context={"symbol_id": symbol_id},
            )

        query = DirectedEdgeQuery(
            symbol_id=symbol_id,
            direction=opts.direction.value,
            kinds=opts.kinds,
            limit=opts.limit,
            offset=opts.offset,
        )
        total_count = uow.edges.count_directed(query)
        edges = uow.edges.list_directed(query)
        target_file, entries = self._build_entries(uow, target, edges, opts)
        stats = self.build_stats(uow, symbol_id)

        return SymbolReferencesReport(
            symbol=self._build_target_summary(target, target_file),
            direction=opts.direction,
            total_count=total_count,
            returned_count=len(entries),
            offset=opts.offset,
            limit=opts.limit,
            has_more=(opts.offset + len(entries)) < total_count,
            stats=stats,
            references=entries,
        )


__all__ = [
    "ReferenceAnalyzer",
]
