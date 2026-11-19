"""Pass 2 intra-repository ``inherits`` edge resolver service."""

from collections.abc import Sequence

from core.models.edge import Edge
from core.models.enums import SymbolKind
from core.utils.logging import get_logger
from parsing.models.linking import ResolutionState
from parsing.services.symbol_index import RepoSymbolIndex

logger = get_logger("parsing.services.inheritance_resolver")

_DEFAULT_INHERIT_CONFIDENCE = 0.75
_CLASS_PREFER_KINDS: frozenset[SymbolKind] = frozenset({SymbolKind.CLASS})


class InheritanceLinkResolver:
    """Resolves ``inherits`` base-class targets and populates direct-base hierarchy maps."""

    @staticmethod
    def _find_base_in_enclosing_scopes(
        index: RepoSymbolIndex,
        from_id: str,
        lookup_name: str,
    ) -> str | None:
        """Search enclosing class/function/module scopes upward for ``lookup_name``."""
        from_node = index.get_node(from_id)
        cur_parent = from_node.parent_id if from_node is not None else None
        visited_parents: set[str] = set()

        while cur_parent is not None and cur_parent not in visited_parents:
            visited_parents.add(cur_parent)
            child = index.find_direct_child(
                cur_parent,
                lookup_name,
                prefer_kinds=_CLASS_PREFER_KINDS,
            )
            if child is not None and child.id != from_id:
                return child.id
            parent_node = index.get_node(cur_parent)
            cur_parent = parent_node.parent_id if parent_node is not None else None
        return None

    @staticmethod
    def _extract_base_name(edge: Edge) -> tuple[str, str]:
        """Return ``(base_name, placeholder_id)`` from ``edge.payload`` or ``edge.to_id``."""
        payload = edge.payload
        raw_placeholder = str(payload.get("unresolved_target") or "")
        base_name = str(
            payload.get("base_name")
            or (raw_placeholder or edge.to_id).removeprefix("unresolved_base:")
        )
        placeholder_id = raw_placeholder or f"unresolved_base:{base_name}"
        return base_name, placeholder_id

    def _resolve_single_target(
        self,
        index: RepoSymbolIndex,
        state: ResolutionState,
        edge: Edge,
        lookup_name: str,
    ) -> str | None:
        """Resolve the target ``SymbolKind.CLASS`` ID for one ``inherits`` edge."""
        if not lookup_name:
            return None
        ev_file_id = edge.evidence_file_id
        if "." not in lookup_name:
            scoped_id = self._find_base_in_enclosing_scopes(index, edge.from_id, lookup_name)
            if scoped_id is not None:
                return scoped_id
            bound_id = state.get_binding_id(ev_file_id, lookup_name)
            return bound_id if bound_id and index.has_node(bound_id) else None

        prefixed_id = index.resolve_prefixed_target(
            state,
            ev_file_id,
            lookup_name,
            prefer_kind=SymbolKind.CLASS,
        )
        if prefixed_id is not None:
            return prefixed_id
        matched = index.find_node(lookup_name, prefer_kind=SymbolKind.CLASS)
        return matched.id if matched is not None else None

    def resolve(
        self,
        index: RepoSymbolIndex,
        state: ResolutionState,
        inherit_edges: Sequence[Edge],
    ) -> None:
        """Execute Pass 2 over ``inherit_edges``, building ``class_bases`` and staging updates.

        Args:
            index: In-memory ``RepoSymbolIndex`` for the repository.
            state: Shared mutable ``ResolutionState`` across passes.
            inherit_edges: Candidate ``EdgeKind.INHERITS`` edges for the repository.
        """
        for edge in inherit_edges:
            base_name, placeholder_id = self._extract_base_name(edge)
            lookup_name = base_name.split("[", 1)[0].strip()
            resolved_id = self._resolve_single_target(index, state, edge, lookup_name)

            if resolved_id is not None:
                state.add_class_base(edge.from_id, resolved_id)

            if state.file_id is None or edge.evidence_file_id == state.file_id:
                state.edges_checked += 1
                r_delta, reset_delta = state.stage_edge_update(
                    edge,
                    resolved_id,
                    placeholder_id,
                    _DEFAULT_INHERIT_CONFIDENCE,
                )
                state.inherits_resolved += r_delta
                state.edges_reset += reset_delta


__all__ = [
    "InheritanceLinkResolver",
]
