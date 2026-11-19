"""Pure in-memory repository symbol lookup index for ``SymbolLinker``."""

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence

from core.models.enums import CALLABLE_KINDS, SymbolKind
from core.models.node import SymbolNode
from core.utils.logging import get_logger
from parsing.models.linking import ResolutionState

logger = get_logger("parsing.services.symbol_index")


class RepoSymbolIndex:
    """In-memory index of ``SymbolNode`` entities supporting ``src.`` and ``repo_name.`` lookup."""

    def __init__(self, repo_name: str | None, nodes: Sequence[SymbolNode]) -> None:
        """Build deterministic lookup tables over ``nodes``.

        Args:
            repo_name: Optional repository root directory name for prefix stripping.
            nodes: Sequence of ``SymbolNode`` entities belonging to the repository.
        """
        self._repo_name = repo_name
        self._node_by_id: dict[str, SymbolNode] = {}
        self._nodes_by_qname: dict[str, list[SymbolNode]] = defaultdict(list)
        self._nodes_by_alt_qname: dict[str, list[SymbolNode]] = defaultdict(list)
        self._module_by_file_id: dict[str, SymbolNode] = {}
        self._module_by_qname: dict[str, SymbolNode] = {}
        self._module_by_alt_qname: dict[str, SymbolNode] = {}
        self._children_by_parent_and_name: dict[tuple[str, str], list[SymbolNode]] = (
            defaultdict(list)
        )

        ordered = sorted(
            nodes,
            key=lambda item: (item.kind.value, item.qualified_name, item.id),
        )
        for node in ordered:
            self._register_node(node)

    def _register_node(self, node: SymbolNode) -> None:
        """Register one ``SymbolNode`` across all in-memory lookup maps."""
        qname = node.qualified_name
        self._node_by_id[node.id] = node
        self._nodes_by_qname[qname].append(node)
        if qname.startswith("src."):
            self._nodes_by_alt_qname[qname[4:]].append(node)

        if node.kind == SymbolKind.MODULE:
            self._module_by_file_id[node.file_id] = node
            self._module_by_qname[qname] = node
            if qname.startswith("src."):
                self._module_by_alt_qname.setdefault(qname[4:], node)

        if node.parent_id is not None:
            self._children_by_parent_and_name[(node.parent_id, node.name)].append(node)

    def has_node(self, node_id: str) -> bool:
        """Check whether ``node_id`` exists in this repository symbol index.

        Args:
            node_id: Candidate ``SymbolNode`` identifier.

        Returns:
            ``True`` when ``node_id`` is registered.
        """
        return node_id in self._node_by_id

    def get_node(self, node_id: str) -> SymbolNode | None:
        """Look up a ``SymbolNode`` by exact ``node_id``.

        Args:
            node_id: Target ``SymbolNode`` identifier.

        Returns:
            Matching ``SymbolNode`` or ``None``.
        """
        return self._node_by_id.get(node_id)

    def get_module_for_file(self, file_id: str) -> SymbolNode | None:
        """Look up the ``SymbolKind.MODULE`` node owning ``file_id``.

        Args:
            file_id: Tracked file identifier.

        Returns:
            Module ``SymbolNode`` or ``None``.
        """
        return self._module_by_file_id.get(file_id)

    def find_module(self, qname: str) -> SymbolNode | None:
        """Find a module node by qualified name with ``src.`` and ``repo_name.`` fallbacks.

        Args:
            qname: Dotted Python module path to resolve.

        Returns:
            Matching ``SymbolKind.MODULE`` ``SymbolNode``, or ``None``.
        """
        if not qname:
            return None
        mod = self._module_by_qname.get(qname) or self._module_by_alt_qname.get(qname)
        if mod is not None:
            return mod
        if self._repo_name and qname.startswith(f"{self._repo_name}."):
            stripped = qname[len(self._repo_name) + 1 :]
            return self._module_by_qname.get(stripped) or self._module_by_alt_qname.get(
                stripped
            )
        return None

    def find_node(
        self,
        qname: str,
        *,
        prefer_kind: SymbolKind | str | None = None,
    ) -> SymbolNode | None:
        """Find a symbol node by qualified name with ``src.`` and ``repo_name.`` fallbacks.

        Args:
            qname: Dotted symbol qualified name to resolve.
            prefer_kind: Optional preferred ``SymbolKind`` when multiple nodes share ``qname``.

        Returns:
            Matching ``SymbolNode``, or ``None``.
        """
        if not qname:
            return None
        candidates = self._nodes_by_qname.get(qname) or self._nodes_by_alt_qname.get(qname)
        if not candidates and self._repo_name and qname.startswith(f"{self._repo_name}."):
            stripped = qname[len(self._repo_name) + 1 :]
            candidates = self._nodes_by_qname.get(stripped) or self._nodes_by_alt_qname.get(
                stripped
            )
        if not candidates:
            return None
        if prefer_kind is not None:
            for cand in candidates:
                if cand.kind == prefer_kind:
                    return cand
        return candidates[0]

    def find_direct_child(
        self,
        parent_id: str,
        name: str,
        *,
        prefer_kinds: set[SymbolKind] | frozenset[SymbolKind] | None = None,
    ) -> SymbolNode | None:
        """Find a direct structural child of ``parent_id`` matching ``name``.

        Args:
            parent_id: Structural parent ``SymbolNode`` identifier.
            name: Simple declared child name.
            prefer_kinds: Optional preferred ``SymbolKind`` set for tie-breaking.

        Returns:
            Matching child ``SymbolNode``, or ``None``.
        """
        candidates = self._children_by_parent_and_name.get((parent_id, name))
        if not candidates:
            return None
        if prefer_kinds is not None:
            for cand in candidates:
                if cand.kind in prefer_kinds:
                    return cand
        return candidates[0]

    def find_enclosing_class_id(self, node_id: str) -> str | None:
        """Walk structural ``parent_id`` links upward to find the nearest enclosing class.

        Args:
            node_id: Starting ``SymbolNode`` identifier.

        Returns:
            Enclosing ``SymbolKind.CLASS`` node ID, or ``None`` if not inside a class.
        """
        visited: set[str] = set()
        cur_id: str | None = node_id
        while cur_id is not None and cur_id not in visited:
            visited.add(cur_id)
            node = self._node_by_id.get(cur_id)
            if node is None:
                return None
            if node.kind == SymbolKind.CLASS:
                return cur_id
            cur_id = node.parent_id
        return None

    def pick_member_on_hierarchy(
        self,
        start_class_id: str,
        member_name: str,
        class_bases: Mapping[str, Sequence[str]],
        *,
        include_start: bool = True,
        prefer_kinds: set[SymbolKind] | frozenset[SymbolKind] | None = CALLABLE_KINDS,
    ) -> str | None:
        """Breadth-first lookup of a member across ``start_class_id`` and its base classes.

        Args:
            start_class_id: Starting class ``SymbolNode`` identifier.
            member_name: Method, attribute, or nested class name to locate.
            class_bases: Mapping from subclass ID to resolved direct base class IDs.
            include_start: Whether to check ``start_class_id`` before base classes
                (``False`` for ``super()`` calls).
            prefer_kinds: Preferred ``SymbolKind`` set (defaults to ``CALLABLE_KINDS``).

        Returns:
            Resolved member ``SymbolNode`` ID, or ``None``.
        """
        queue: deque[str] = deque()
        visited: set[str] = set()

        if include_start:
            queue.append(start_class_id)
            visited.add(start_class_id)
        else:
            for base_id in class_bases.get(start_class_id, ()):
                if base_id not in visited:
                    visited.add(base_id)
                    queue.append(base_id)

        while queue:
            cls_id = queue.popleft()
            child = self.find_direct_child(cls_id, member_name, prefer_kinds=prefer_kinds)
            if child is not None:
                return child.id
            for base_id in class_bases.get(cls_id, ()):
                if base_id not in visited:
                    visited.add(base_id)
                    queue.append(base_id)
        return None

    def _lookup_bound_prefix(
        self,
        state: ResolutionState,
        ev_file_id: str,
        prefix: str,
        *,
        is_single_part: bool,
    ) -> SymbolNode | None:
        """Resolve a dotted-name prefix in ``ev_file_id`` to a bound module or symbol node."""
        bound_id = state.get_dotted_module_id(ev_file_id, prefix) or state.get_binding_id(
            ev_file_id,
            prefix,
        )
        if not bound_id and is_single_part:
            ev_module = self.get_module_for_file(ev_file_id)
            if ev_module is not None:
                same_file_child = self.find_direct_child(ev_module.id, prefix)
                if same_file_child is not None:
                    bound_id = same_file_child.id
        return self.get_node(bound_id) if bound_id else None

    def _resolve_from_bound_node(
        self,
        state: ResolutionState,
        bound_node: SymbolNode,
        remainder: Sequence[str],
        prefer_kind: SymbolKind | None,
    ) -> str | None:
        """Resolve trailing attribute parts starting from a bound module or class node."""
        if bound_node.kind == SymbolKind.MODULE:
            target_qname = f"{bound_node.qualified_name}.{'.'.join(remainder)}"
            target = self.find_node(target_qname, prefer_kind=prefer_kind)
            if target is not None:
                return target.id
            if len(remainder) == 1:
                reexport_id = state.get_binding_id(bound_node.file_id, remainder[0])
                if reexport_id and self.has_node(reexport_id):
                    return reexport_id
        elif bound_node.kind == SymbolKind.CLASS and len(remainder) == 1:
            member_prefer = (
                frozenset({prefer_kind}) if prefer_kind is not None else CALLABLE_KINDS
            )
            return self.pick_member_on_hierarchy(
                bound_node.id,
                remainder[0],
                state.class_bases,
                prefer_kinds=member_prefer,
            )
        return None

    def resolve_prefixed_target(
        self,
        state: ResolutionState,
        ev_file_id: str | None,
        dotted_name: str,
        *,
        prefer_kind: SymbolKind | None = None,
    ) -> str | None:
        """Resolve a dotted reference like ``utils.helper`` or ``pkg.mod.Cls`` via bindings.

        Args:
            state: Current ``ResolutionState`` containing Pass 1 bindings and Pass 2 bases.
            ev_file_id: Evidence file identifier where ``dotted_name`` appears.
            dotted_name: Dotted attribute reference string.
            prefer_kind: Optional preferred ``SymbolKind`` for disambiguation.

        Returns:
            Resolved target ``SymbolNode`` identifier, or ``None``.
        """
        if not ev_file_id or "." not in dotted_name:
            return None
        parts = dotted_name.split(".")
        for prefix_len in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:prefix_len])
            bound_node = self._lookup_bound_prefix(
                state,
                ev_file_id,
                prefix,
                is_single_part=prefix_len == 1,
            )
            if bound_node is None:
                continue
            resolved = self._resolve_from_bound_node(
                state,
                bound_node,
                parts[prefix_len:],
                prefer_kind,
            )
            if resolved is not None:
                return resolved
        return None


__all__ = [
    "RepoSymbolIndex",
]
