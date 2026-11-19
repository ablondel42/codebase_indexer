"""Pass 3 intra-repository ``calls`` edge resolver service."""

from collections.abc import Sequence

from core.models.edge import Edge
from core.models.enums import CALLABLE_KINDS, SymbolKind
from core.utils.logging import get_logger
from parsing.models.linking import ResolutionState
from parsing.services.symbol_index import RepoSymbolIndex

logger = get_logger("parsing.services.call_resolver")

_DEFAULT_CALL_CONFIDENCE = 0.7
_CALL_OR_CLASS_KINDS: frozenset[SymbolKind] = CALLABLE_KINDS | frozenset({SymbolKind.CLASS})


class CallLinkResolver:
    """Resolves ``self``/``cls``/``super()``, dotted, imported, and lexical ``calls`` edges."""

    @staticmethod
    def _resolve_self_or_cls_call(
        index: RepoSymbolIndex,
        state: ResolutionState,
        caller_id: str,
        target: str,
    ) -> str | None:
        """Resolve ``self.method()`` or ``cls.method()`` across the enclosing class hierarchy."""
        member_name = target.split(".", 1)[1] if "." in target else target
        enclosing_cls_id = index.find_enclosing_class_id(caller_id)
        if enclosing_cls_id is None or "." in member_name:
            return None
        return index.pick_member_on_hierarchy(
            enclosing_cls_id,
            member_name,
            state.class_bases,
        )

    @staticmethod
    def _resolve_super_call(
        index: RepoSymbolIndex,
        state: ResolutionState,
        caller_id: str,
        target: str,
    ) -> str | None:
        """Resolve ``super().method()`` strictly on ancestor base classes."""
        method_name = target[len("super().") :]
        if "." in method_name:
            return None
        enclosing_cls_id = index.find_enclosing_class_id(caller_id)
        if enclosing_cls_id is None:
            return None
        return index.pick_member_on_hierarchy(
            enclosing_cls_id,
            method_name,
            state.class_bases,
            include_start=False,
        )

    @staticmethod
    def _find_unqualified_call_in_scopes(
        index: RepoSymbolIndex,
        caller_id: str,
        target: str,
    ) -> str | None:
        """Search enclosing function/module scopes (skipping class bodies per Python LEGB)."""
        cur_scope_id: str | None = caller_id
        visited_scopes: set[str] = set()

        while cur_scope_id is not None and cur_scope_id not in visited_scopes:
            visited_scopes.add(cur_scope_id)
            scope_node = index.get_node(cur_scope_id)
            if scope_node is None:
                break
            if scope_node.kind != SymbolKind.CLASS or cur_scope_id == caller_id:
                child = index.find_direct_child(
                    cur_scope_id,
                    target,
                    prefer_kinds=_CALL_OR_CLASS_KINDS,
                )
                if child is not None:
                    return child.id
            cur_scope_id = scope_node.parent_id
        return None

    def _resolve_bare_name_call(
        self,
        index: RepoSymbolIndex,
        state: ResolutionState,
        caller_id: str,
        ev_file_id: str | None,
        target: str,
    ) -> str | None:
        """Resolve an unqualified ``func()`` or ``Cls()`` call via LEGB scopes and imports."""
        scoped_id = self._find_unqualified_call_in_scopes(index, caller_id, target)
        if scoped_id is not None:
            return scoped_id

        bound_id = state.get_binding_id(ev_file_id, target)
        if bound_id and index.has_node(bound_id):
            return bound_id

        caller_node = index.get_node(caller_id)
        if (
            caller_node is not None
            and caller_node.name == target
            and caller_node.kind in CALLABLE_KINDS
        ):
            return caller_id
        return None

    @staticmethod
    def _extract_call_target(edge: Edge) -> tuple[str, str]:
        """Return ``(target, placeholder_id)`` from ``edge.payload`` or ``edge.to_id``."""
        payload = edge.payload
        raw_placeholder = str(payload.get("unresolved_target") or "")
        target = str(
            payload.get("target")
            or (raw_placeholder or edge.to_id).removeprefix("unresolved_call:")
        )
        placeholder_id = raw_placeholder or f"unresolved_call:{target}"
        return target, placeholder_id

    def _resolve_single_call(
        self,
        index: RepoSymbolIndex,
        state: ResolutionState,
        edge: Edge,
        target: str,
        qualifier: str | None,
    ) -> str | None:
        """Resolve the target ``SymbolNode`` ID for one ``calls`` edge."""
        caller_id = edge.from_id
        ev_file_id = edge.evidence_file_id
        if qualifier in ("self", "cls"):
            return self._resolve_self_or_cls_call(index, state, caller_id, target)
        if qualifier == "module":
            return index.resolve_prefixed_target(state, ev_file_id, target)
        if qualifier is None and target.startswith("super()."):
            return self._resolve_super_call(index, state, caller_id, target)
        if qualifier is None and "." not in target:
            return self._resolve_bare_name_call(index, state, caller_id, ev_file_id, target)
        return None

    def resolve(
        self,
        index: RepoSymbolIndex,
        state: ResolutionState,
        call_edges: Sequence[Edge],
    ) -> None:
        """Execute Pass 3 over ``call_edges`` and stage target resolution/reset updates.

        Args:
            index: In-memory ``RepoSymbolIndex`` for the repository.
            state: Shared mutable ``ResolutionState`` populated by Passes 1 and 2.
            call_edges: Candidate ``EdgeKind.CALLS`` edges to evaluate.
        """
        for edge in call_edges:
            if state.file_id is not None and edge.evidence_file_id != state.file_id:
                continue
            state.edges_checked += 1
            target, placeholder_id = self._extract_call_target(edge)
            raw_qualifier = edge.payload.get("qualifier")
            qualifier = str(raw_qualifier) if raw_qualifier is not None else None
            resolved_id = self._resolve_single_call(index, state, edge, target, qualifier)
            r_delta, reset_delta = state.stage_edge_update(
                edge,
                resolved_id,
                placeholder_id,
                _DEFAULT_CALL_CONFIDENCE,
            )
            state.calls_resolved += r_delta
            state.edges_reset += reset_delta


__all__ = [
    "CallLinkResolver",
]
