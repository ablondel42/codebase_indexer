"""Bulk risk target loader and deterministic risk fact collector service."""

from collections.abc import Sequence
from typing import Any

from core.models.config import AppConfig
from core.models.enums import AST_DEPENDENCY_EDGE_KINDS, EdgeKind, SymbolKind, SymbolScope
from core.models.exceptions import SymbolNotFoundError, ValidationError
from core.models.file import FileRecord
from core.models.node import SymbolSummary
from core.ports.uow import UnitOfWorkProtocol
from core.utils.logging import get_logger
from graph.models.risk import RiskFacts, RiskTarget, TargetReferenceAggregation
from graph.services.navigator import GraphNavigator
from graph.services.references import ReferenceAnalyzer
from graph.utils.predicates import derive_target_lines, is_public_like

logger = get_logger("graph.services.risk_facts")

LOW_CONFIDENCE_EDGE_LIMIT: int = 25
LOW_CONFIDENCE_THRESHOLD: float = 0.8

_CLASS_MEMBER_INHERITANCE_KINDS: frozenset[SymbolKind] = frozenset(
    {
        SymbolKind.METHOD,
        SymbolKind.ASYNC_METHOD,
        SymbolKind.ATTRIBUTE,
    }
)


class RiskFactCollector:
    """Loads ``RiskTarget`` records in bulk and aggregates graph/disk ``RiskFacts``."""

    def __init__(
        self,
        config: AppConfig | None = None,
        navigator: GraphNavigator | None = None,
        reference_analyzer: ReferenceAnalyzer | None = None,
    ) -> None:
        """Initialize the collector with optional collaborator overrides.

        Args:
            config: Optional ``AppConfig`` controlling file-size bounds on freshness checks.
            navigator: Optional ``GraphNavigator`` collaborator.
            reference_analyzer: Optional ``ReferenceAnalyzer`` collaborator.
        """
        self._config = config if config is not None else AppConfig()
        self._navigator = navigator if navigator is not None else GraphNavigator(self._config)
        self._references = (
            reference_analyzer
            if reference_analyzer is not None
            else ReferenceAnalyzer(self._config)
        )

    @staticmethod
    def _summary_to_risk_target(summary: SymbolSummary) -> RiskTarget:
        """Convert a ``SymbolSummary`` projection into a normalized ``RiskTarget``.

        Args:
            summary: Joined ``SymbolSummary`` projection.

        Returns:
            Normalized ``RiskTarget`` model.
        """
        start_line, end_line = derive_target_lines(
            summary.start_line,
            summary.end_line,
            summary.decl_range,
        )
        return RiskTarget(
            symbol_id=summary.id,
            qualified_name=summary.qualified_name,
            kind=summary.kind,
            scope=summary.scope.value if summary.scope is not None else SymbolScope.MODULE.value,
            file_id=summary.file_id,
            file_path=summary.file_path,
            module_path=summary.module_path,
            visibility_hint=summary.visibility_hint,
            parent_id=summary.parent_id,
            lexical_parent_id=summary.lexical_parent_id,
            is_test=summary.is_test,
            start_line=start_line,
            end_line=end_line,
            signature=summary.signature,
            confidence=summary.confidence,
            last_indexed_at=summary.last_indexed_at,
            repo_id=summary.repo_id,
        )

    def load_targets(
        self,
        uow: UnitOfWorkProtocol,
        symbol_ids: Sequence[str],
    ) -> list[RiskTarget]:
        """Bulk-load and normalize ``RiskTarget`` items preserving exact input order.

        Args:
            uow: Active read Unit of Work.
            symbol_ids: Ordered sequence of canonical symbol identifiers.

        Returns:
            Deduplicated ordered list of ``RiskTarget`` models.

        Raises:
            ValidationError: If ``symbol_ids`` is empty or contains only blank strings.
            SymbolNotFoundError: If any requested symbol ID does not exist in the graph.
        """
        ordered_ids = list(dict.fromkeys(sid.strip() for sid in symbol_ids if sid and sid.strip()))
        if not ordered_ids:
            raise ValidationError(
                "At least one symbol ID must be provided for risk analysis.",
                context={"symbol_ids": list(symbol_ids)},
            )

        summaries_by_id = uow.nodes.get_summaries_by_ids(ordered_ids)
        missing = [sid for sid in ordered_ids if sid not in summaries_by_id]
        if missing:
            raise SymbolNotFoundError(
                f"Symbol not found: {missing[0]}",
                context={"symbol_id": missing[0], "missing_symbol_ids": missing},
            )

        return [self._summary_to_risk_target(summaries_by_id[sid]) for sid in ordered_ids]

    @staticmethod
    def _detect_target_impact_spread(
        uow: UnitOfWorkProtocol,
        target: RiskTarget,
        reference_source: str,
    ) -> tuple[bool, bool]:
        """Return ``(cross_file_impact, cross_module_impact)`` for ``target``.

        Args:
            uow: Active read Unit of Work.
            target: Target symbol being evaluated.
            reference_source: ``"lsp"`` or ``"ast"`` source selected by ``build_stats``.

        Returns:
            Tuple ``(cross_file_impact, cross_module_impact)``.
        """
        edge_kinds = (
            (EdgeKind.REFERENCES,) if reference_source == "lsp" else AST_DEPENDENCY_EDGE_KINDS
        )
        sites = uow.edges.list_referencing_sites(target.symbol_id, edge_kinds)
        if not sites:
            return False, False

        ref_files = {site.file_id for site in sites if site.file_id}
        ref_modules = {site.module_path for site in sites if site.module_path}
        cross_file = any(fid != target.file_id for fid in ref_files)
        if target.module_path:
            cross_module = any(mod != target.module_path for mod in ref_modules)
        else:
            cross_module = len(ref_modules) > 1
        return cross_file, cross_module

    @staticmethod
    def _target_has_inheritance_risk(uow: UnitOfWorkProtocol, target: RiskTarget) -> bool:
        """Return ``True`` when ``target`` or its owning class participates in ``inherits``.

        Args:
            uow: Active read Unit of Work.
            target: Target symbol being evaluated.

        Returns:
            ``True`` if ``target`` is a base/subclass or a method/attribute on one.
        """
        if target.scope == SymbolScope.FUNCTION.value:
            return False
        if target.kind == SymbolKind.CLASS:
            return uow.edges.has_edge_between_or_for(target.symbol_id, EdgeKind.INHERITS)
        if target.kind in _CLASS_MEMBER_INHERITANCE_KINDS:
            parent_id = target.parent_id
            if parent_id is None:
                node = uow.nodes.get_by_id(target.symbol_id)
                parent_id = node.parent_id if node is not None else None
            if parent_id:
                return uow.edges.has_edge_between_or_for(parent_id, EdgeKind.INHERITS)
        return False

    def _resolve_repo_root_cached(
        self,
        uow: UnitOfWorkProtocol,
        file_rec: FileRecord | None,
        repo_root_cache: dict[str, str | None],
    ) -> str | None:
        """Resolve and cache ``RepoRecord.root_path`` once per ``repo_id``.

        Args:
            uow: Active read Unit of Work.
            file_rec: Tracked ``FileRecord`` or ``None``.
            repo_root_cache: Per-call cache mapping ``repo_id`` to ``root_path``.

        Returns:
            Repository root path string or ``None``.
        """
        if file_rec is None:
            return None
        if file_rec.repo_id not in repo_root_cache:
            repo = uow.repos.get_by_id(file_rec.repo_id)
            repo_root_cache[file_rec.repo_id] = repo.root_path if repo is not None else None
        return repo_root_cache[file_rec.repo_id]

    def _collect_stale_symbols(
        self,
        uow: UnitOfWorkProtocol,
        targets: Sequence[RiskTarget],
    ) -> list[str]:
        """Collect stale target symbol IDs checking each unique file on disk at most once.

        Args:
            uow: Active read Unit of Work.
            targets: Sequence of ``RiskTarget`` items.

        Returns:
            Ordered list of stale symbol IDs.
        """
        files_by_id = uow.files.get_by_ids([t.file_id for t in targets if t.file_id])
        repo_root_cache: dict[str, str | None] = {}
        file_fresh_cache: dict[str, bool] = {}
        stale: list[str] = []

        for target in targets:
            if target.last_indexed_at is None:
                stale.append(target.symbol_id)
                continue
            if target.file_id not in file_fresh_cache:
                file_rec = files_by_id.get(target.file_id)
                repo_root = self._resolve_repo_root_cached(uow, file_rec, repo_root_cache)
                check = self._navigator.check_file_freshness(
                    uow,
                    target.file_id,
                    file_record=file_rec,
                    repo_root=repo_root,
                )
                file_fresh_cache[target.file_id] = check.is_fresh
            if not file_fresh_cache[target.file_id]:
                stale.append(target.symbol_id)
        return stale

    def _collect_reference_maps(
        self,
        uow: UnitOfWorkProtocol,
        targets: Sequence[RiskTarget],
    ) -> TargetReferenceAggregation:
        """Collect per-target fanout maps, reference sources, and cross-file/module spread.

        Args:
            uow: Active read Unit of Work.
            targets: Sequence of ``RiskTarget`` items.

        Returns:
            Populated ``TargetReferenceAggregation`` model.
        """
        ref_counts: dict[str, int] = {}
        ref_avail: dict[str, bool] = {}
        file_counts: dict[str, int] = {}
        mod_counts: dict[str, int] = {}
        sources: dict[str, str] = {}
        cross_file = False
        cross_module = False

        for target in targets:
            stats = self._references.build_stats(uow, target.symbol_id)
            ref_counts[target.symbol_id] = stats.reference_count
            ref_avail[target.symbol_id] = stats.available
            file_counts[target.symbol_id] = stats.referencing_file_count
            mod_counts[target.symbol_id] = stats.referencing_module_count
            sources[target.symbol_id] = stats.reference_source
            t_file, t_mod = self._detect_target_impact_spread(
                uow,
                target,
                stats.reference_source,
            )
            cross_file = cross_file or t_file
            cross_module = cross_module or t_mod

        return TargetReferenceAggregation(
            reference_counts=ref_counts,
            reference_availability=ref_avail,
            referencing_file_counts=file_counts,
            referencing_module_counts=mod_counts,
            reference_sources=sources,
            cross_file_impact=cross_file,
            cross_module_impact=cross_module,
        )

    def collect_facts(
        self,
        uow: UnitOfWorkProtocol,
        targets: Sequence[RiskTarget],
    ) -> RiskFacts:
        """Aggregate deterministic ``RiskFacts`` across ``targets`` with zero N+1 node queries.

        Args:
            uow: Active read Unit of Work.
            targets: Normalized ``RiskTarget`` sequence.

        Returns:
            Populated immutable ``RiskFacts`` model.
        """
        if not targets:
            return RiskFacts()

        symbol_ids = [t.symbol_id for t in targets]
        agg = self._collect_reference_maps(uow, targets)
        all_file_ids = {t.file_id for t in targets if t.file_id}
        all_module_keys = {key for t in targets if (key := (t.module_path or t.file_id))}
        stale_symbols = self._collect_stale_symbols(uow, targets)
        low_conf_symbols = [t.symbol_id for t in targets if t.confidence < LOW_CONFIDENCE_THRESHOLD]
        low_edge_sample = uow.edges.sample_low_confidence_references(
            symbol_ids,
            threshold=LOW_CONFIDENCE_THRESHOLD,
            limit=LOW_CONFIDENCE_EDGE_LIMIT,
        )

        extra: dict[str, Any] = {
            "all_references_available": all(agg.reference_availability.values()),
            "any_references_unavailable": not all(
                agg.reference_availability.get(tid, False) for tid in symbol_ids
            ),
            "reference_sources": agg.reference_sources,
            "low_confidence_edge_count": low_edge_sample.total_count,
            "low_confidence_edges_truncated": (
                low_edge_sample.total_count > len(low_edge_sample.edge_ids)
            ),
        }

        return RiskFacts(
            target_count=len(targets),
            symbol_ids=symbol_ids,
            symbol_kinds=sorted({str(t.kind) for t in targets}),
            reference_counts=agg.reference_counts,
            reference_availability=agg.reference_availability,
            referencing_file_counts=agg.referencing_file_counts,
            referencing_module_counts=agg.referencing_module_counts,
            touches_public_surface=any(is_public_like(t) for t in targets),
            touches_local_scope_only=all(t.scope == SymbolScope.FUNCTION.value for t in targets),
            target_spans_multiple_files=len(all_file_ids) > 1,
            target_spans_multiple_modules=len(all_module_keys) > 1,
            cross_file_impact=agg.cross_file_impact,
            cross_module_impact=agg.cross_module_impact,
            inheritance_involved=any(
                self._target_has_inheritance_risk(uow, t) for t in targets
            ),
            stale_symbols=stale_symbols,
            low_confidence_symbols=low_conf_symbols,
            low_confidence_edges=low_edge_sample.edge_ids,
            extra=extra,
        )


__all__ = [
    "LOW_CONFIDENCE_EDGE_LIMIT",
    "LOW_CONFIDENCE_THRESHOLD",
    "RiskFactCollector",
]
