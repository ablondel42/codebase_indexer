"""Pass 1 intra-repository ``imports`` edge resolver and re-export propagator."""

from collections.abc import Sequence

from core.models.edge import Edge
from core.models.enums import SymbolKind
from core.utils.logging import get_logger
from parsing.models.linking import ResolutionState
from parsing.services.symbol_index import RepoSymbolIndex

logger = get_logger("parsing.services.import_resolver")

_MAX_REEXPORT_HOPS = 3
_DEFAULT_IMPORT_CONFIDENCE = 0.8


class ImportLinkResolver:
    """Resolves direct and ``from`` imports, propagates re-exports, and stages updates."""

    @staticmethod
    def _bind_resolved_symbol(
        index: RepoSymbolIndex,
        state: ResolutionState,
        ev_file_id: str | None,
        bound_name: str,
        resolved_id: str,
    ) -> None:
        """Bind ``bound_name`` in ``ev_file_id``, marking module targets for dotted lookup."""
        if not ev_file_id or not bound_name:
            return
        target_node = index.get_node(resolved_id)
        is_mod = target_node is not None and target_node.kind == SymbolKind.MODULE
        state.bind_symbol(ev_file_id, bound_name, resolved_id, is_dotted_module=is_mod)

    @staticmethod
    def _extract_from_symbol_name(edge: Edge, mod_name: str) -> str:
        """Extract the imported symbol name from ``edge.payload`` or placeholder target."""
        payload = edge.payload
        sym_name = payload.get("symbol") or payload.get("imported_name")
        if sym_name:
            return str(sym_name)
        fallback_target = str(payload.get("unresolved_target") or edge.to_id)
        raw_tail = fallback_target.removeprefix("external_or_unresolved:")
        if mod_name and raw_tail.startswith(f"{mod_name}."):
            return raw_tail[len(mod_name) + 1 :]
        return raw_tail

    @staticmethod
    def _resolve_direct_import(
        index: RepoSymbolIndex,
        state: ResolutionState,
        edge: Edge,
    ) -> tuple[str, str | None]:
        """Resolve an ``import pkg.mod [as alias]`` edge and populate file bindings."""
        payload = edge.payload
        mod_name = str(payload.get("module") or "")
        placeholder_id = str(
            payload.get("unresolved_target") or f"external_or_unresolved:{mod_name}"
        )
        mod_node = index.find_module(mod_name)
        if mod_node is None:
            return placeholder_id, None

        ev_file_id = edge.evidence_file_id
        alias = payload.get("alias")
        if alias:
            state.bind_symbol(ev_file_id, str(alias), mod_node.id, is_dotted_module=True)
        else:
            state.record_dotted_module(ev_file_id, mod_name, mod_node.id)
            top_name = mod_name.split(".")[0]
            top_mod = index.find_module(top_name)
            if top_mod is not None:
                state.bind_symbol(ev_file_id, top_name, top_mod.id)
        return placeholder_id, mod_node.id

    def _resolve_from_import(
        self,
        index: RepoSymbolIndex,
        state: ResolutionState,
        edge: Edge,
    ) -> tuple[str, str | None]:
        """Resolve a ``from mod import sym [as alias]`` edge and populate file bindings."""
        payload = edge.payload
        mod_name = str(payload.get("module") or "")
        sym_name = self._extract_from_symbol_name(edge, mod_name)
        target = f"{mod_name}.{sym_name}" if mod_name else sym_name
        placeholder_id = str(payload.get("unresolved_target") or f"external_or_unresolved:{target}")

        if sym_name == "*" or not sym_name:
            mod_node = index.find_module(mod_name)
            return placeholder_id, mod_node.id if mod_node is not None else None

        matched = index.find_node(target) or index.find_module(target)
        resolved_id = matched.id if matched is not None else None
        if resolved_id is not None:
            bound_name = str(payload.get("alias") or sym_name)
            self._bind_resolved_symbol(
                index,
                state,
                edge.evidence_file_id,
                bound_name,
                resolved_id,
            )
        return placeholder_id, resolved_id

    def _try_resolve_reexport(
        self,
        index: RepoSymbolIndex,
        state: ResolutionState,
        edge: Edge,
    ) -> str | None:
        """Attempt one ``__init__.py`` or module re-export hop for an unresolved ``from`` edge."""
        payload = edge.payload
        mod_name = str(payload.get("module") or "")
        sym_name = self._extract_from_symbol_name(edge, mod_name)
        if payload.get("import_type") != "from" or not mod_name or not sym_name or sym_name == "*":
            return None

        mod_node = index.find_module(mod_name)
        if mod_node is None:
            return None
        reexported_id = state.get_binding_id(mod_node.file_id, sym_name)
        if not reexported_id or not index.has_node(reexported_id):
            return None

        bound_name = str(payload.get("alias") or sym_name)
        self._bind_resolved_symbol(
            index,
            state,
            edge.evidence_file_id,
            bound_name,
            reexported_id,
        )
        return reexported_id

    def _propagate_reexports(
        self,
        index: RepoSymbolIndex,
        state: ResolutionState,
        cache: list[tuple[Edge, str, str | None]],
    ) -> None:
        """Propagate package ``__init__.py`` re-exports up to ``_MAX_REEXPORT_HOPS``."""
        for _ in range(_MAX_REEXPORT_HOPS):
            progressed = False
            for idx, (edge, placeholder_id, resolved_id) in enumerate(cache):
                if resolved_id is not None:
                    continue
                reexported_id = self._try_resolve_reexport(index, state, edge)
                if reexported_id is not None:
                    cache[idx] = (edge, placeholder_id, reexported_id)
                    progressed = True
            if not progressed:
                break

    def resolve(
        self,
        index: RepoSymbolIndex,
        state: ResolutionState,
        import_edges: Sequence[Edge],
    ) -> None:
        """Execute Pass 1 over ``import_edges``, recording bindings and staging updates.

        Args:
            index: In-memory ``RepoSymbolIndex`` for the repository.
            state: Shared mutable ``ResolutionState`` across passes.
            import_edges: Candidate ``EdgeKind.IMPORTS`` edges for the repository.
        """
        cache: list[tuple[Edge, str, str | None]] = []
        for edge in import_edges:
            if edge.payload.get("import_type") == "import":
                placeholder_id, resolved_id = self._resolve_direct_import(index, state, edge)
            else:
                placeholder_id, resolved_id = self._resolve_from_import(index, state, edge)
            cache.append((edge, placeholder_id, resolved_id))

        self._propagate_reexports(index, state, cache)

        for edge, placeholder_id, resolved_id in cache:
            if state.file_id is not None and edge.evidence_file_id != state.file_id:
                continue
            state.edges_checked += 1
            valid_id = (
                resolved_id if resolved_id is not None and index.has_node(resolved_id) else None
            )
            r_delta, reset_delta = state.stage_edge_update(
                edge,
                valid_id,
                placeholder_id,
                _DEFAULT_IMPORT_CONFIDENCE,
            )
            state.imports_resolved += r_delta
            state.edges_reset += reset_delta


__all__ = [
    "ImportLinkResolver",
]
