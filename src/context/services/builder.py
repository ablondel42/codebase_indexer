"""Bounded symbol context assembly service."""

from collections.abc import Mapping, Sequence

from context.models.source import SnippetOptions
from context.models.symbol_context import (
    CompactSymbol,
    ContextBuildOptions,
    ContextEdge,
    FocusSymbol,
    FreshnessEvalInputs,
    StructuralTotals,
    SymbolContext,
    SymbolEdgeBundle,
    SymbolHierarchy,
)
from context.services.evaluator import ContextEvaluator
from context.services.source import SourceReader
from context.utils.formatting import edge_sort_key, select_balanced_edges
from core.models.config import AppConfig
from core.models.edge import Edge
from core.models.enums import EdgeKind, SymbolKind, SymbolScope
from core.models.exceptions import (
    ParseError,
    PathSecurityError,
    SymbolNotFoundError,
    ValidationError,
)
from core.models.file import FileRecord
from core.models.node import SymbolNode
from core.ports.uow import UnitOfWorkProtocol
from core.utils.file_io import format_line_span
from core.utils.logging import get_logger
from core.utils.pathing import format_edge_location
from graph.services.navigator import GraphNavigator
from graph.services.references import ReferenceAnalyzer
from graph.utils.predicates import (
    derive_target_lines,
    extract_edge_line,
    resolve_endpoint_name,
)

logger = get_logger("context.services.builder")

_STRUCTURAL_EDGE_KINDS: frozenset[EdgeKind] = frozenset(
    {
        EdgeKind.CONTAINS,
        EdgeKind.SCOPE_PARENT,
    }
)


class SymbolContextBuilder:
    """Coordinates navigation, source slicing, and evaluation to build ``SymbolContext``."""

    def __init__(
        self,
        config: AppConfig | None = None,
        navigator: GraphNavigator | None = None,
        source_reader: SourceReader | None = None,
        evaluator: ContextEvaluator | None = None,
        reference_analyzer: ReferenceAnalyzer | None = None,
    ) -> None:
        """Initialize the context builder with optional collaborator overrides.

        Args:
            config: Optional ``AppConfig`` instance.
            navigator: Optional ``GraphNavigator`` collaborator.
            source_reader: Optional ``SourceReader`` collaborator.
            evaluator: Optional ``ContextEvaluator`` collaborator.
            reference_analyzer: Optional ``ReferenceAnalyzer`` collaborator.
        """
        self._config = config if config is not None else AppConfig()
        self._navigator = navigator if navigator is not None else GraphNavigator(self._config)
        self._source_reader = (
            source_reader if source_reader is not None else SourceReader(self._config)
        )
        self._evaluator = (
            evaluator if evaluator is not None else ContextEvaluator(self._config, self._navigator)
        )
        self._references = (
            reference_analyzer
            if reference_analyzer is not None
            else ReferenceAnalyzer(self._config)
        )

    @staticmethod
    def _has_real_lexical_parent(node: SymbolNode) -> bool:
        """Return ``True`` when ``node`` has a non-class-method lexical parent.

        Args:
            node: Focus ``SymbolNode``.

        Returns:
            ``True`` for function-nested symbols; ``False`` for top-level or class methods.
        """
        if node.lexical_parent_id is None:
            return False
        return not (
            node.kind in (SymbolKind.METHOD, SymbolKind.ASYNC_METHOD)
            and node.scope == SymbolScope.CLASS
        )

    def _load_hierarchy(
        self,
        uow: UnitOfWorkProtocol,
        focus_node: SymbolNode,
    ) -> SymbolHierarchy:
        """Load structural/lexical parents, children, and same-file direct siblings.

        Args:
            uow: Active read Unit of Work.
            focus_node: Focus ``SymbolNode``.

        Returns:
            Immutable ``SymbolHierarchy`` bundle.
        """
        structural_parent = self._navigator.get_parent(uow, focus_node)
        structural_children = self._navigator.get_children(uow, focus_node.id)
        lexical_parent = (
            self._navigator.get_lexical_parent(uow, focus_node)
            if self._has_real_lexical_parent(focus_node)
            else None
        )
        lexical_children = [
            child
            for child in self._navigator.get_lexical_children(uow, focus_node.id)
            if child.scope == SymbolScope.FUNCTION
        ]
        direct_siblings = [
            sibling
            for sibling in self._navigator.list_file_symbols(uow, focus_node.file_id)
            if sibling.id != focus_node.id and sibling.parent_id == focus_node.parent_id
        ]
        return SymbolHierarchy(
            structural_parent=structural_parent,
            structural_children=structural_children,
            lexical_parent=lexical_parent,
            lexical_children=lexical_children,
            direct_siblings=direct_siblings,
        )

    def _load_edges(
        self,
        uow: UnitOfWorkProtocol,
        symbol_id: str,
        edge_limit: int,
    ) -> SymbolEdgeBundle:
        """Load raw edges and sort/bound semantic incoming and outgoing edges.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Focus symbol identifier.
            edge_limit: Maximum semantic edges per direction.

        Returns:
            Immutable ``SymbolEdgeBundle``.
        """
        incoming_raw = self._navigator.list_incoming_edges(uow, symbol_id)
        outgoing_raw = self._navigator.list_outgoing_edges(uow, symbol_id)
        incoming_semantic = sorted(
            (edge for edge in incoming_raw if edge.kind not in _STRUCTURAL_EDGE_KINDS),
            key=edge_sort_key,
        )
        outgoing_semantic = sorted(
            (edge for edge in outgoing_raw if edge.kind not in _STRUCTURAL_EDGE_KINDS),
            key=edge_sort_key,
        )
        return SymbolEdgeBundle(
            all_raw=[*incoming_raw, *outgoing_raw],
            incoming_semantic=incoming_semantic,
            outgoing_semantic=outgoing_semantic,
            bounded_incoming=select_balanced_edges(incoming_semantic, edge_limit),
            bounded_outgoing=select_balanced_edges(outgoing_semantic, edge_limit),
        )

    @staticmethod
    def _resolve_peers_and_files(
        uow: UnitOfWorkProtocol,
        focus_file_id: str,
        hierarchy: SymbolHierarchy,
        edges: SymbolEdgeBundle,
    ) -> tuple[dict[str, SymbolNode], dict[str, FileRecord]]:
        """Batch-fetch peer ``SymbolNode`` records and ``FileRecord`` metadata.

        Args:
            uow: Active read Unit of Work.
            focus_file_id: Containing file ID of the focus symbol.
            hierarchy: Loaded ``SymbolHierarchy``.
            edges: Loaded ``SymbolEdgeBundle``.

        Returns:
            Tuple ``(peers_by_id, files_by_id)``.
        """
        peer_ids: list[str] = [
            *(edge.from_id for edge in edges.bounded_incoming),
            *(edge.to_id for edge in edges.bounded_outgoing),
        ]
        file_ids: list[str] = [focus_file_id]
        for parent in (hierarchy.structural_parent, hierarchy.lexical_parent):
            if parent is not None:
                file_ids.append(parent.file_id)
        for edge in [*edges.bounded_incoming, *edges.bounded_outgoing]:
            if edge.evidence_file_id:
                file_ids.append(edge.evidence_file_id)

        peers_by_id = uow.nodes.get_by_ids(peer_ids)
        file_ids.extend(peer.file_id for peer in peers_by_id.values() if peer.file_id)
        return peers_by_id, uow.files.get_by_ids(file_ids)

    @staticmethod
    def _node_to_focus_symbol(
        node: SymbolNode,
        file_record: FileRecord | None,
    ) -> FocusSymbol:
        """Convert a ``SymbolNode`` and optional ``FileRecord`` into a ``FocusSymbol``.

        Args:
            node: Source ``SymbolNode``.
            file_record: Optional containing ``FileRecord``.

        Returns:
            Populated ``FocusSymbol`` model.
        """
        payload_file = node.payload.get("file_path")
        payload_mod = node.payload.get("module_path")
        file_path = (
            file_record.file_path
            if file_record is not None
            else (payload_file if isinstance(payload_file, str) else None)
        )
        module_path = (
            file_record.module_path
            if file_record is not None
            else (payload_mod if isinstance(payload_mod, str) else None)
        )
        start_line, end_line = derive_target_lines(
            node.start_line,
            node.end_line,
            node.decl_range,
        )
        return FocusSymbol(
            id=node.id,
            kind=node.kind,
            name=node.name,
            qualified_name=node.qualified_name,
            location=node.location,
            file_path=file_path,
            module_path=module_path,
            start_line=start_line,
            end_line=end_line,
            lines=format_line_span(start_line, end_line),
            decl_range=node.decl_range,
            selection_range=node.selection_range,
            signature=node.signature,
            doc_summary=node.doc_summary,
            parent_id=node.parent_id,
            scope=node.scope,
            lexical_parent_id=node.lexical_parent_id,
            visibility_hint=node.visibility_hint,
            confidence=node.confidence,
        )

    @staticmethod
    def _node_to_compact_symbol(node: SymbolNode) -> CompactSymbol:
        """Convert a child or sibling ``SymbolNode`` into a ``CompactSymbol``.

        Args:
            node: Child or sibling ``SymbolNode``.

        Returns:
            Populated ``CompactSymbol`` model.
        """
        start_line, end_line = derive_target_lines(
            node.start_line,
            node.end_line,
            node.decl_range,
        )
        return CompactSymbol(
            id=node.id,
            kind=node.kind,
            name=node.name,
            location=node.location,
            lines=format_line_span(start_line, end_line),
            signature=node.signature,
            doc_summary=node.doc_summary,
        )

    @staticmethod
    def _adapt_context_edges(
        edges: Sequence[Edge],
        *,
        is_incoming: bool,
        peers_by_id: Mapping[str, SymbolNode],
        files_by_id: Mapping[str, FileRecord],
    ) -> list[ContextEdge]:
        """Adapt bounded ``Edge`` models into ``ContextEdge`` items.

        Args:
            edges: Bounded semantic ``Edge`` sequence.
            is_incoming: ``True`` for incoming edges, ``False`` for outgoing edges.
            peers_by_id: Resolved peer ``SymbolNode`` lookup map.
            files_by_id: Resolved ``FileRecord`` lookup map.

        Returns:
            Ordered list of ``ContextEdge`` models.
        """
        items: list[ContextEdge] = []
        for edge in edges:
            peer_id = edge.from_id if is_incoming else edge.to_id
            peer_name = resolve_endpoint_name(
                peer_id,
                peers_by_id,
                edge,
                allow_payload_fallback=not is_incoming,
            )
            ev_file = files_by_id.get(edge.evidence_file_id or "")
            edge_loc = edge.location or (
                format_edge_location(ev_file.uri, edge.evidence_range)
                if ev_file is not None
                else None
            )
            items.append(
                ContextEdge(
                    id=edge.id,
                    kind=edge.kind,
                    from_id=edge.from_id,
                    to_id=edge.to_id,
                    peer_name=peer_name,
                    location=edge_loc,
                    file_path=ev_file.file_path if ev_file is not None else None,
                    line=extract_edge_line(edge),
                    confidence=edge.confidence,
                    source=edge.source,
                    last_indexed_at=edge.last_indexed_at,
                )
            )
        return items

    def _attach_inline_source(
        self,
        uow: UnitOfWorkProtocol,
        focus: FocusSymbol,
        max_source_lines: int,
    ) -> FocusSymbol:
        """Attach bounded inline source (or ``source_error``) to ``focus``.

        Args:
            uow: Active read Unit of Work.
            focus: Base ``FocusSymbol`` model.
            max_source_lines: Maximum inline source lines to read.

        Returns:
            Updated ``FocusSymbol`` copy with ``source``/``raw_source`` or ``source_error``.
        """
        try:
            snippet = self._source_reader.read_symbol_snippet(
                uow,
                focus.id,
                SnippetOptions(max_lines=max_source_lines, offset=0),
            )
            return focus.model_copy(
                update={
                    "source": snippet.numbered_source,
                    "raw_source": snippet.raw_source,
                    "source_truncated": snippet.truncated,
                }
            )
        except PathSecurityError as exc:
            logger.warning(
                "Path security check blocked inline source for symbol %s: %s",
                focus.id,
                exc,
            )
            return focus.model_copy(
                update={
                    "source_error": (
                        "Access denied: source file is missing, symlinked, "
                        "or outside repository root."
                    )
                }
            )
        except (OSError, ParseError) as exc:
            logger.warning(
                "Unable to read inline source from disk for symbol %s: %s",
                focus.id,
                exc,
            )
            return focus.model_copy(
                update={"source_error": "Unable to read symbol source from disk."}
            )
        except ValidationError as exc:
            logger.warning(
                "Symbol %s lacks usable line range metadata for inline source: %s",
                focus.id,
                exc,
            )
            return focus.model_copy(
                update={"source_error": "Symbol has no usable line range metadata."}
            )

    def _build_base_context(
        self,
        uow: UnitOfWorkProtocol,
        focus_node: SymbolNode,
        hierarchy: SymbolHierarchy,
        edges: SymbolEdgeBundle,
        options: ContextBuildOptions,
    ) -> tuple[SymbolContext, FileRecord | None]:
        """Assemble the structural and edge core of ``SymbolContext`` prior to evaluation.

        Args:
            uow: Active read Unit of Work.
            focus_node: Focus ``SymbolNode``.
            hierarchy: Loaded ``SymbolHierarchy``.
            edges: Loaded ``SymbolEdgeBundle``.
            options: Active ``ContextBuildOptions``.

        Returns:
            Tuple ``(base_symbol_context, focus_file_record)``.
        """
        peers_by_id, files_by_id = self._resolve_peers_and_files(
            uow,
            focus_node.file_id,
            hierarchy,
            edges,
        )
        focus_file = files_by_id.get(focus_node.file_id)
        focus_symbol = self._node_to_focus_symbol(focus_node, focus_file)
        if options.include_source:
            focus_symbol = self._attach_inline_source(
                uow,
                focus_symbol,
                options.max_source_lines,
            )

        structural_parent = (
            self._node_to_focus_symbol(
                hierarchy.structural_parent,
                files_by_id.get(hierarchy.structural_parent.file_id),
            )
            if hierarchy.structural_parent is not None
            else None
        )
        lexical_parent = (
            self._node_to_focus_symbol(
                hierarchy.lexical_parent,
                files_by_id.get(hierarchy.lexical_parent.file_id),
            )
            if hierarchy.lexical_parent is not None
            else None
        )

        base_ctx = SymbolContext(
            focus_symbol=focus_symbol,
            structural_parent=structural_parent,
            structural_children=[
                self._node_to_compact_symbol(node)
                for node in hierarchy.structural_children[: options.child_limit]
            ],
            lexical_parent=lexical_parent,
            lexical_children=[
                self._node_to_compact_symbol(node)
                for node in hierarchy.lexical_children[: options.child_limit]
            ],
            incoming_edges=self._adapt_context_edges(
                edges.bounded_incoming,
                is_incoming=True,
                peers_by_id=peers_by_id,
                files_by_id=files_by_id,
            ),
            outgoing_edges=self._adapt_context_edges(
                edges.bounded_outgoing,
                is_incoming=False,
                peers_by_id=peers_by_id,
                files_by_id=files_by_id,
            ),
            file_siblings=[
                self._node_to_compact_symbol(node)
                for node in hierarchy.direct_siblings[: options.sibling_limit]
            ],
            reference_summary=self._references.build_stats(uow, focus_node.id),
        )
        return base_ctx, focus_file

    def build_context(
        self,
        uow: UnitOfWorkProtocol,
        symbol_id: str,
        options: ContextBuildOptions | None = None,
    ) -> SymbolContext:
        """Assemble a complete, bounded immutable ``SymbolContext`` around ``symbol_id``.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Canonical focus symbol identifier.
            options: Optional item budgets and inline source configuration.

        Returns:
            Populated immutable ``SymbolContext`` model.

        Raises:
            SymbolNotFoundError: If ``symbol_id`` does not exist in the graph.
        """
        opts = options if options is not None else ContextBuildOptions()
        focus_node = self._navigator.get_symbol(uow, symbol_id)
        if focus_node is None:
            raise SymbolNotFoundError(
                f"Symbol not found: {symbol_id}",
                context={"symbol_id": symbol_id},
            )

        hierarchy = self._load_hierarchy(uow, focus_node)
        edges = self._load_edges(uow, symbol_id, opts.edge_limit)
        base_ctx, focus_file = self._build_base_context(
            uow,
            focus_node,
            hierarchy,
            edges,
            opts,
        )

        structural_summary = self._evaluator.evaluate_structural(
            base_ctx,
            StructuralTotals(
                total_structural_child_count=len(hierarchy.structural_children),
                total_lexical_child_count=len(hierarchy.lexical_children),
                total_incoming_count=len(edges.incoming_semantic),
                total_outgoing_count=len(edges.outgoing_semantic),
                total_sibling_count=len(hierarchy.direct_siblings),
                relationship_counts=self._evaluator.count_relationships(
                    edges.incoming_semantic,
                    edges.outgoing_semantic,
                ),
            ),
        )
        freshness = self._evaluator.evaluate_freshness(
            base_ctx,
            FreshnessEvalInputs(
                focus_node=focus_node,
                raw_edges=edges.all_raw,
                file_record=focus_file,
            ),
            uow=uow,
        )
        confidence = self._evaluator.evaluate_confidence(base_ctx, raw_edges=edges.all_raw)

        return base_ctx.model_copy(
            update={
                "structural_summary": structural_summary,
                "freshness": freshness,
                "confidence": confidence,
            }
        )


__all__ = [
    "SymbolContextBuilder",
]
