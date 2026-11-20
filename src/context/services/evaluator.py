"""Structural, freshness, and confidence evaluator service for symbol contexts."""

from collections import Counter
from collections.abc import Sequence

from context.models.symbol_context import (
    ConfidenceSummary,
    ContextEdge,
    FocusSymbol,
    FreshnessEvalInputs,
    FreshnessSummary,
    RelationshipCounts,
    StructuralSummary,
    StructuralTotals,
    SymbolContext,
)
from context.utils.formatting import (
    edges_have_placeholder_targets,
    is_lsp_enriched,
    is_nested_lexical_declaration,
)
from core.models.config import AppConfig
from core.models.edge import Edge
from core.models.enums import LOCAL_CALLABLE_KINDS, EdgeKind, SymbolKind, SymbolScope
from core.ports.uow import UnitOfWorkProtocol
from core.utils.file_io import check_file_disk_freshness
from core.utils.logging import get_logger
from core.utils.timestamps import latest_timestamp
from graph.services.navigator import GraphNavigator

logger = get_logger("context.services.evaluator")


class ContextEvaluator:
    """Evaluates ``StructuralSummary``, ``FreshnessSummary``, and ``ConfidenceSummary``."""

    def __init__(
        self,
        config: AppConfig | None = None,
        navigator: GraphNavigator | None = None,
    ) -> None:
        """Initialize the evaluator with optional configuration and navigator.

        Args:
            config: Optional ``AppConfig`` controlling disk file-size limits.
            navigator: Optional ``GraphNavigator`` collaborator.
        """
        self._config = config if config is not None else AppConfig()
        self._navigator = navigator if navigator is not None else GraphNavigator(self._config)

    @staticmethod
    def is_local_declaration(focus: FocusSymbol) -> bool:
        """Return ``True`` when ``focus`` is a function-local callable or local class.

        Args:
            focus: ``FocusSymbol`` being evaluated.

        Returns:
            ``True`` if ``focus`` is declared inside a function body.
        """
        return bool(
            focus.kind in LOCAL_CALLABLE_KINDS
            or (focus.kind == SymbolKind.CLASS and focus.scope == SymbolScope.FUNCTION)
        )

    @staticmethod
    def is_nested_declaration(focus: FocusSymbol) -> bool:
        """Return ``True`` strictly for lexically nested non-method declarations."""
        return is_nested_lexical_declaration(focus)

    @staticmethod
    def count_relationships(
        incoming: Sequence[Edge | ContextEdge],
        outgoing: Sequence[Edge | ContextEdge],
    ) -> RelationshipCounts:
        """Aggregate exact per-kind semantic relationship totals.

        Args:
            incoming: Incoming semantic ``Edge`` or ``ContextEdge`` items.
            outgoing: Outgoing semantic ``Edge`` or ``ContextEdge`` items.

        Returns:
            Populated ``RelationshipCounts`` model.
        """
        in_counts = Counter(edge.kind for edge in incoming)
        out_counts = Counter(edge.kind for edge in outgoing)
        return RelationshipCounts(
            callers=in_counts.get(EdgeKind.CALLS, 0),
            callees=out_counts.get(EdgeKind.CALLS, 0),
            imported_by=in_counts.get(EdgeKind.IMPORTS, 0),
            imports=out_counts.get(EdgeKind.IMPORTS, 0),
            subclasses=in_counts.get(EdgeKind.INHERITS, 0),
            bases=out_counts.get(EdgeKind.INHERITS, 0),
            references_in=in_counts.get(EdgeKind.REFERENCES, 0),
            references_out=out_counts.get(EdgeKind.REFERENCES, 0),
        )

    @staticmethod
    def _resolve_count(override: int | None, items: Sequence[object]) -> int:
        """Return explicit ``override`` when provided, otherwise ``len(items)``.

        Args:
            override: Optional unbounded count override.
            items: Bounded sequence fallback.

        Returns:
            Resolved non-negative integer count.
        """
        return override if override is not None else len(items)

    def evaluate_structural(
        self,
        context: SymbolContext,
        totals: StructuralTotals | None = None,
    ) -> StructuralSummary:
        """Compute ``StructuralSummary`` from ``context`` and optional unbounded ``totals``.

        Args:
            context: Assembled ``SymbolContext`` model.
            totals: Optional unbounded child, edge, sibling, and relationship counts.

        Returns:
            Populated ``StructuralSummary`` model.
        """
        focus = context.focus_symbol
        resolved = totals if totals is not None else StructuralTotals()
        structural_child_total = self._resolve_count(
            resolved.total_structural_child_count,
            context.structural_children,
        )
        rel_counts = (
            resolved.relationship_counts
            if resolved.relationship_counts is not None
            else self.count_relationships(context.incoming_edges, context.outgoing_edges)
        )
        return StructuralSummary(
            has_structural_parent=context.structural_parent is not None,
            structural_child_count=structural_child_total,
            structural_children_truncated=(
                structural_child_total > len(context.structural_children)
            ),
            has_lexical_parent=context.lexical_parent is not None,
            lexical_child_count=self._resolve_count(
                resolved.total_lexical_child_count,
                context.lexical_children,
            ),
            incoming_edge_count=self._resolve_count(
                resolved.total_incoming_count,
                context.incoming_edges,
            ),
            outgoing_edge_count=self._resolve_count(
                resolved.total_outgoing_count,
                context.outgoing_edges,
            ),
            same_file_sibling_count=self._resolve_count(
                resolved.total_sibling_count,
                context.file_siblings,
            ),
            scope=focus.scope,
            is_local_declaration=self.is_local_declaration(focus),
            is_nested_declaration=self.is_nested_declaration(focus),
            relationship_counts=rel_counts,
        )

    @staticmethod
    def _compute_edge_snapshot_timestamp(
        context: SymbolContext,
        focus_id: str,
        raw_edges: Sequence[Edge] | None,
        uow: UnitOfWorkProtocol | None,
    ) -> str | None:
        """Compute the latest ``last_indexed_at`` timestamp across edges touching ``focus_id``.

        Args:
            context: Assembled ``SymbolContext``.
            focus_id: Focus symbol identifier.
            raw_edges: Optional unbounded raw ``Edge`` sequence.
            uow: Optional active read Unit of Work.

        Returns:
            Latest ISO 8601 edge timestamp string or ``None``.
        """
        if raw_edges is not None:
            return latest_timestamp(*(edge.last_indexed_at for edge in raw_edges))

        adapted_ts = [
            edge.last_indexed_at
            for edge in [*context.incoming_edges, *context.outgoing_edges]
            if edge.last_indexed_at is not None
        ]
        if adapted_ts:
            return latest_timestamp(*adapted_ts)
        if uow is not None and focus_id:
            return uow.edges.latest_indexed_at_for_symbol(focus_id)
        return None

    def _resolve_disk_freshness(
        self,
        node_last_indexed_at: str | None,
        inputs: FreshnessEvalInputs,
        uow: UnitOfWorkProtocol | None,
    ) -> tuple[bool, str | None]:
        """Resolve ``(is_fresh, stale_reason)`` against node timestamp and disk state.

        Args:
            node_last_indexed_at: Focus symbol ``last_indexed_at`` timestamp or ``None``.
            inputs: Optional node, file, and repo-root freshness inputs.
            uow: Optional active read Unit of Work.

        Returns:
            Tuple ``(is_fresh, stale_reason)``.
        """
        if node_last_indexed_at is None:
            return False, "missing_last_indexed_at"
        if uow is not None and (inputs.focus_node is not None or inputs.file_record is not None):
            target_file_id = (
                inputs.focus_node.file_id
                if inputs.focus_node is not None
                else (inputs.file_record.id if inputs.file_record is not None else "")
            )
            check = self._navigator.check_file_freshness(
                uow,
                target_file_id,
                file_record=inputs.file_record,
                repo_root=inputs.repo_root,
            )
            return check.is_fresh, check.stale_reason
        if inputs.file_record is not None and inputs.repo_root is not None:
            check = check_file_disk_freshness(
                inputs.repo_root,
                inputs.file_record,
                max_file_size_bytes=self._config.max_file_size_bytes,
            )
            return check.is_fresh, check.stale_reason
        return True, None

    def evaluate_freshness(
        self,
        context: SymbolContext,
        inputs: FreshnessEvalInputs | None = None,
        *,
        uow: UnitOfWorkProtocol | None = None,
        lsp_enriched: bool | None = None,
    ) -> FreshnessSummary:
        """Compute ``FreshnessSummary`` verifying timestamps, LSP enrichment, and disk hash."""
        resolved_inputs = inputs if inputs is not None else FreshnessEvalInputs()
        focus_node = resolved_inputs.focus_node
        focus_id = focus_node.id if focus_node is not None else context.focus_symbol.id
        node_last_indexed_at = focus_node.last_indexed_at if focus_node is not None else None
        raw_edges = resolved_inputs.raw_edges

        edge_ts = self._compute_edge_snapshot_timestamp(context, focus_id, raw_edges, uow)
        if raw_edges is not None and focus_id:
            has_incoming = any(edge.to_id == focus_id for edge in raw_edges)
            has_outgoing = any(edge.from_id == focus_id for edge in raw_edges)
        else:
            has_incoming = len(context.incoming_edges) > 0
            has_outgoing = len(context.outgoing_edges) > 0

        is_fresh, stale_reason = self._resolve_disk_freshness(
            node_last_indexed_at,
            resolved_inputs,
            uow,
        )
        enriched = lsp_enriched if lsp_enriched is not None else is_lsp_enriched(context, raw_edges)

        return FreshnessSummary(
            node_last_indexed_at=node_last_indexed_at,
            edge_snapshot_last_indexed_at=edge_ts,
            has_incoming_edges=has_incoming,
            has_outgoing_edges=has_outgoing,
            context_source="lsp+ast" if enriched else "ast_only",
            is_fresh=is_fresh,
            stale_reason=stale_reason,
        )

    def evaluate_confidence(
        self,
        context: SymbolContext,
        *,
        raw_edges: Sequence[Edge] | None = None,
        lsp_enriched: bool | None = None,
    ) -> ConfidenceSummary:
        """Compute ``ConfidenceSummary`` across ``context.focus_symbol`` and incident edges."""
        all_edges: Sequence[Edge | ContextEdge] = (
            raw_edges
            if raw_edges is not None
            else [*context.incoming_edges, *context.outgoing_edges]
        )
        confidences = [float(edge.confidence) for edge in all_edges]
        enriched = lsp_enriched if lsp_enriched is not None else is_lsp_enriched(context, raw_edges)
        return ConfidenceSummary(
            focus_symbol_confidence=context.focus_symbol.confidence,
            edge_confidence_min=min(confidences) if confidences else None,
            edge_confidence_max=max(confidences) if confidences else None,
            contains_placeholder_targets=edges_have_placeholder_targets(all_edges),
            graph_only=not enriched,
        )


__all__ = [
    "ContextEvaluator",
]
