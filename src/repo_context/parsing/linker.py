"""Intra-repository AST edge linker for imports, inheritance, and call-sites."""

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import bindparam, select, update
from sqlalchemy.engine import Connection

from repo_context.constants import (
    EDGE_KIND_CALLS,
    EDGE_KIND_IMPORTS,
    EDGE_KIND_INHERITS,
)
from repo_context.storage.schema import edges_table, nodes_table, repos_table

_CALLABLE_KINDS = frozenset(
    {
        "function",
        "async_function",
        "method",
        "async_method",
        "local_function",
        "local_async_function",
    }
)
_UNRESOLVED_PREFIXES = (
    "external_or_unresolved:",
    "unresolved_base:",
    "unresolved_call:",
)
_RESOLVED_CONFIDENCE = 0.9


def _parse_payload(raw_payload: str | dict | None) -> dict:
    """Safely parse edge ``payload_json`` into a dictionary."""
    if raw_payload is None:
        return {}
    if isinstance(raw_payload, dict):
        return raw_payload
    try:
        parsed = json.loads(raw_payload)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


class _RepoGraphIndex:
    """In-memory index of repository nodes for fast intra-repo linking."""

    def __init__(self, conn: Connection, repo_id: str) -> None:
        self.node_by_id: dict[str, dict] = {}
        self.nodes_by_qname: dict[str, list[dict]] = defaultdict(list)
        self.nodes_by_alt_qname: dict[str, list[dict]] = defaultdict(list)
        self.module_by_file_id: dict[str, dict] = {}
        self.module_by_qname: dict[str, dict] = {}
        self.module_by_alt_qname: dict[str, dict] = {}
        self.children_by_parent_and_name: dict[tuple[str, str], list[dict]] = defaultdict(list)

        repo_name = conn.execute(
            select(repos_table.c.name).where(repos_table.c.id == repo_id)
        ).scalar_one_or_none()
        self.repo_name: str | None = str(repo_name) if repo_name is not None else None

        rows = (
            conn.execute(
                select(
                    nodes_table.c.id,
                    nodes_table.c.file_id,
                    nodes_table.c.kind,
                    nodes_table.c.name,
                    nodes_table.c.qualified_name,
                    nodes_table.c.parent_id,
                    nodes_table.c.scope,
                    nodes_table.c.lexical_parent_id,
                )
                .where(nodes_table.c.repo_id == repo_id)
                .order_by(nodes_table.c.kind, nodes_table.c.qualified_name, nodes_table.c.id)
            )
            .mappings()
            .all()
        )
        for row in rows:
            self._register_node(dict(row))

    def _register_node(self, node: dict) -> None:
        """Index one repository symbol row into lookup tables."""
        node_id = node["id"]
        qname = node["qualified_name"]
        self.node_by_id[node_id] = node
        self.nodes_by_qname[qname].append(node)
        if qname.startswith("src."):
            self.nodes_by_alt_qname[qname[4:]].append(node)

        if node["kind"] == "module":
            self.module_by_file_id[node["file_id"]] = node
            self.module_by_qname[qname] = node
            if qname.startswith("src."):
                self.module_by_alt_qname.setdefault(qname[4:], node)

        parent_id = node.get("parent_id")
        if parent_id is not None:
            self.children_by_parent_and_name[(parent_id, node["name"])].append(node)

    def find_module(self, qname: str) -> dict | None:
        """Find a module node by qualified name (falling back to ``src.`` and ``repo_name.``)."""
        if not qname:
            return None
        mod = self.module_by_qname.get(qname) or self.module_by_alt_qname.get(qname)
        if mod is not None:
            return mod
        if self.repo_name and qname.startswith(f"{self.repo_name}."):
            stripped = qname[len(self.repo_name) + 1 :]
            return self.module_by_qname.get(stripped) or self.module_by_alt_qname.get(stripped)
        return None

    def find_node(self, qname: str, *, prefer_kind: str | None = None) -> dict | None:
        """Find a node by qualified name (falling back to ``src.`` and ``repo_name.``)."""
        if not qname:
            return None
        candidates = self.nodes_by_qname.get(qname) or self.nodes_by_alt_qname.get(qname)
        if not candidates and self.repo_name and qname.startswith(f"{self.repo_name}."):
            stripped = qname[len(self.repo_name) + 1 :]
            candidates = self.nodes_by_qname.get(stripped) or self.nodes_by_alt_qname.get(stripped)
        if not candidates:
            return None
        if prefer_kind is not None:
            for cand in candidates:
                if cand["kind"] == prefer_kind:
                    return cand
        return candidates[0]

    def find_direct_child(
        self,
        parent_id: str,
        name: str,
        *,
        prefer_kinds: set[str] | frozenset[str] | None = None,
    ) -> dict | None:
        """Find a direct structural child of ``parent_id`` matching ``name``."""
        candidates = self.children_by_parent_and_name.get((parent_id, name))
        if not candidates:
            return None
        if prefer_kinds is not None:
            for cand in candidates:
                if cand["kind"] in prefer_kinds:
                    return cand
        return candidates[0]


@dataclass
class _LinkerState:
    """Mutable state shared across import, inheritance, and call resolution passes."""

    index: _RepoGraphIndex
    file_id: str | None
    updates: list[dict[str, Any]] = field(default_factory=list)
    file_bindings: dict[str, dict[str, str]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    dotted_import_map: dict[str, dict[str, str]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    class_bases: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    imports_resolved: int = 0
    inherits_resolved: int = 0
    calls_resolved: int = 0
    edges_reset: int = 0
    checked_count: int = 0


def _pick_member_on_class_hierarchy(
    index: _RepoGraphIndex,
    start_class_id: str,
    member_name: str,
    class_bases: dict[str, list[str]],
    *,
    include_start: bool = True,
    prefer_kinds: set[str] | frozenset[str] | None = _CALLABLE_KINDS,
) -> str | None:
    """Breadth-first lookup of a method/attribute/nested class on a class and its bases."""
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
        child = index.find_direct_child(cls_id, member_name, prefer_kinds=prefer_kinds)
        if child is not None:
            return child["id"]
        for base_id in class_bases.get(cls_id, ()):
            if base_id not in visited:
                visited.add(base_id)
                queue.append(base_id)
    return None


def _find_enclosing_class_id(index: _RepoGraphIndex, node_id: str) -> str | None:
    """Walk structural parents upward to find the nearest enclosing class node ID."""
    visited: set[str] = set()
    cur_id: str | None = node_id
    while cur_id is not None and cur_id not in visited:
        visited.add(cur_id)
        node = index.node_by_id.get(cur_id)
        if node is None:
            return None
        if node["kind"] == "class":
            return cur_id
        cur_id = node.get("parent_id")
    return None


def _lookup_bound_prefix(
    state: _LinkerState,
    ev_file_id: str,
    prefix: str,
    is_single_part: bool,
) -> dict | None:
    """Resolve a dotted-name prefix in ``ev_file_id`` to a bound module or symbol node."""
    bound_id = (
        state.dotted_import_map.get(ev_file_id, {}).get(prefix)
        or state.file_bindings.get(ev_file_id, {}).get(prefix)
    )
    if not bound_id and is_single_part:
        ev_module = state.index.module_by_file_id.get(ev_file_id)
        if ev_module is not None:
            same_file_child = state.index.find_direct_child(ev_module["id"], prefix)
            if same_file_child is not None:
                bound_id = same_file_child["id"]
    return state.index.node_by_id.get(bound_id) if bound_id else None


def _resolve_from_bound_node(
    state: _LinkerState,
    bound_node: dict,
    remainder: list[str],
    prefer_kind: str | None,
) -> str | None:
    """Resolve trailing attribute parts starting from a bound module or class node."""
    if bound_node["kind"] == "module":
        target_qname = f"{bound_node['qualified_name']}.{'.'.join(remainder)}"
        target = state.index.find_node(target_qname, prefer_kind=prefer_kind)
        if target is not None:
            return target["id"]
        if len(remainder) == 1:
            return state.file_bindings.get(bound_node["file_id"], {}).get(remainder[0]) or None
    elif bound_node["kind"] == "class" and len(remainder) == 1:
        member_prefer = {prefer_kind} if prefer_kind else _CALLABLE_KINDS
        return _pick_member_on_class_hierarchy(
            state.index,
            bound_node["id"],
            remainder[0],
            state.class_bases,
            prefer_kinds=member_prefer,
        )
    return None


def _resolve_prefixed_target(
    state: _LinkerState,
    ev_file_id: str | None,
    dotted_name: str,
    *,
    prefer_kind: str | None = None,
) -> str | None:
    """Resolve a dotted reference like ``utils.helper`` or ``pkg.utils.Cls`` via imports."""
    if not ev_file_id or "." not in dotted_name:
        return None
    parts = dotted_name.split(".")
    for prefix_len in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:prefix_len])
        bound_node = _lookup_bound_prefix(state, ev_file_id, prefix, prefix_len == 1)
        if bound_node is None:
            continue
        resolved = _resolve_from_bound_node(
            state, bound_node, parts[prefix_len:], prefer_kind
        )
        if resolved is not None:
            return resolved
    return None


def _stage_edge_update(
    edge: dict[str, Any],
    resolved_id: str | None,
    placeholder_id: str,
    default_confidence: float,
    updates: list[dict[str, Any]],
) -> tuple[int, int]:
    """Queue an edge UPDATE if resolved or if a stale broken link needs resetting."""
    current_to = edge["to_id"]
    if resolved_id is not None:
        if current_to != resolved_id:
            updates.append(
                {
                    "b_id": edge["id"],
                    "b_to_id": resolved_id,
                    "b_confidence": _RESOLVED_CONFIDENCE,
                }
            )
            return 1, 0
        return 0, 0

    if not current_to.startswith(_UNRESOLVED_PREFIXES) and current_to != placeholder_id:
        updates.append(
            {
                "b_id": edge["id"],
                "b_to_id": placeholder_id,
                "b_confidence": default_confidence,
            }
        )
        return 0, 1
    return 0, 0


def _bind_imported_symbol(
    state: _LinkerState,
    ev_file_id: str | None,
    bound_name: str,
    resolved_id: str,
) -> None:
    """Bind an imported symbol or submodule name in ``ev_file_id``."""
    if not ev_file_id:
        return
    state.file_bindings[ev_file_id][bound_name] = resolved_id
    if state.index.node_by_id[resolved_id]["kind"] == "module":
        state.dotted_import_map[ev_file_id][bound_name] = resolved_id


def _resolve_direct_import_edge(
    state: _LinkerState,
    edge: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[str, str | None]:
    """Resolve an ``import pkg.mod [as alias]`` edge and record file bindings."""
    mod_name = payload.get("module") or ""
    placeholder_id = f"external_or_unresolved:{mod_name}"
    mod_node = state.index.find_module(mod_name)
    if mod_node is None:
        return placeholder_id, None

    mod_id = str(mod_node["id"])
    ev_file_id = edge.get("evidence_file_id")
    if ev_file_id:
        alias = payload.get("alias")
        if alias:
            state.file_bindings[ev_file_id][alias] = mod_id
            state.dotted_import_map[ev_file_id][alias] = mod_id
        else:
            state.dotted_import_map[ev_file_id][mod_name] = mod_id
            top_name = mod_name.split(".")[0]
            top_mod = state.index.find_module(top_name)
            if top_mod is not None:
                state.file_bindings[ev_file_id][top_name] = str(top_mod["id"])
    return placeholder_id, mod_id


def _extract_from_import_symbol(
    edge: dict[str, Any],
    payload: dict[str, Any],
    mod_name: str,
) -> str:
    """Extract imported symbol name from payload or fallback placeholder ``to_id``."""
    sym_name = payload.get("symbol")
    if sym_name:
        return str(sym_name)
    raw_tail = str(edge["to_id"]).removeprefix("external_or_unresolved:")
    if mod_name and raw_tail.startswith(f"{mod_name}."):
        return raw_tail[len(mod_name) + 1 :]
    return raw_tail


def _resolve_from_import_edge(
    state: _LinkerState,
    edge: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[str, str | None]:
    """Resolve a ``from mod import sym [as alias]`` edge and record file bindings."""
    mod_name = payload.get("module") or ""
    sym_name = _extract_from_import_symbol(edge, payload, mod_name)
    target = f"{mod_name}.{sym_name}" if mod_name else sym_name
    placeholder_id = f"external_or_unresolved:{target}"

    if sym_name == "*" or not sym_name:
        mod_node = state.index.find_module(mod_name)
        return placeholder_id, mod_node["id"] if mod_node is not None else None

    matched_node = state.index.find_node(target) or state.index.find_module(target)
    resolved_id = matched_node["id"] if matched_node is not None else None
    if resolved_id is not None:
        _bind_imported_symbol(
            state,
            edge.get("evidence_file_id"),
            payload.get("alias") or sym_name,
            resolved_id,
        )
    return placeholder_id, resolved_id


def _try_resolve_reexport_entry(
    state: _LinkerState,
    edge: dict[str, Any],
    payload: dict[str, Any],
) -> str | None:
    """Attempt one re-export hop for an unresolved ``from`` import edge."""
    mod_name = payload.get("module") or ""
    sym_name = payload.get("symbol") or ""
    if payload.get("import_type") != "from" or not mod_name or not sym_name or sym_name == "*":
        return None
    mod_node = state.index.find_module(mod_name)
    if mod_node is None:
        return None
    reexported_id = state.file_bindings.get(mod_node["file_id"], {}).get(sym_name)
    if not reexported_id or reexported_id not in state.index.node_by_id:
        return None
    _bind_imported_symbol(
        state,
        edge.get("evidence_file_id"),
        payload.get("alias") or sym_name,
        reexported_id,
    )
    return reexported_id


def _propagate_reexports(
    state: _LinkerState,
    cache: list[tuple[dict, dict, str, str | None]],
) -> None:
    """Propagate package ``__init__`` re-exports up to 3 hops."""
    for _ in range(3):
        progressed = False
        for idx, (edge, payload, placeholder_id, resolved_id) in enumerate(cache):
            if resolved_id is not None:
                continue
            reexported_id = _try_resolve_reexport_entry(state, edge, payload)
            if reexported_id is not None:
                cache[idx] = (edge, payload, placeholder_id, reexported_id)
                progressed = True
        if not progressed:
            break


def _resolve_import_pass(state: _LinkerState, import_edges: list[dict[str, Any]]) -> None:
    """Execute Pass 1: resolve all ``imports`` edges and populate file symbol bindings."""
    cache: list[tuple[dict, dict, str, str | None]] = []
    for edge in import_edges:
        payload = _parse_payload(edge.get("payload_json"))
        if payload.get("import_type") == "import":
            placeholder_id, resolved_id = _resolve_direct_import_edge(state, edge, payload)
        else:
            placeholder_id, resolved_id = _resolve_from_import_edge(state, edge, payload)
        cache.append((edge, payload, placeholder_id, resolved_id))

    _propagate_reexports(state, cache)

    for edge, _payload, placeholder_id, resolved_id in cache:
        if state.file_id is not None and edge.get("evidence_file_id") != state.file_id:
            continue
        state.checked_count += 1
        valid_id = (
            resolved_id
            if resolved_id is not None and resolved_id in state.index.node_by_id
            else None
        )
        r_delta, reset_delta = _stage_edge_update(
            edge, valid_id, placeholder_id, 0.8, state.updates
        )
        state.imports_resolved += r_delta
        state.edges_reset += reset_delta


def _find_base_in_enclosing_scopes(
    index: _RepoGraphIndex,
    from_id: str,
    lookup_name: str,
) -> str | None:
    """Search enclosing class/function/module scopes upward for a base class name."""
    from_node = index.node_by_id.get(from_id)
    cur_parent = from_node.get("parent_id") if from_node else None
    visited_parents: set[str] = set()
    while cur_parent is not None and cur_parent not in visited_parents:
        visited_parents.add(cur_parent)
        child = index.find_direct_child(cur_parent, lookup_name, prefer_kinds={"class"})
        if child is not None and child["id"] != from_id:
            return child["id"]
        parent_node = index.node_by_id.get(cur_parent)
        cur_parent = parent_node.get("parent_id") if parent_node else None
    return None


def _resolve_single_inherit_target(
    state: _LinkerState,
    edge: dict[str, Any],
    lookup_name: str,
) -> str | None:
    """Resolve the target class ID for one ``inherits`` edge."""
    if not lookup_name:
        return None
    ev_file_id = edge.get("evidence_file_id")
    if "." not in lookup_name:
        scoped_id = _find_base_in_enclosing_scopes(state.index, edge["from_id"], lookup_name)
        if scoped_id is not None:
            return scoped_id
        bound = state.file_bindings.get(ev_file_id, {}).get(lookup_name) if ev_file_id else None
        return bound if bound and bound in state.index.node_by_id else None

    prefixed_id = _resolve_prefixed_target(
        state, ev_file_id, lookup_name, prefer_kind="class"
    )
    if prefixed_id is None:
        node = state.index.find_node(lookup_name, prefer_kind="class")
        return node["id"] if node is not None else None
    return prefixed_id


def _resolve_inherit_pass(state: _LinkerState, inherit_edges: list[dict[str, Any]]) -> None:
    """Execute Pass 2: resolve ``inherits`` edges and build ``class_bases`` graph."""
    for edge in inherit_edges:
        payload = _parse_payload(edge.get("payload_json"))
        base_name = payload.get("base_name") or edge["to_id"].removeprefix("unresolved_base:")
        placeholder_id = f"unresolved_base:{base_name}"
        lookup_name = base_name.split("[", 1)[0].strip()
        resolved_id = _resolve_single_inherit_target(state, edge, lookup_name)

        if resolved_id is not None:
            state.class_bases[edge["from_id"]].append(resolved_id)

        ev_file_id = edge.get("evidence_file_id")
        if state.file_id is None or ev_file_id == state.file_id:
            state.checked_count += 1
            r_delta, reset_delta = _stage_edge_update(
                edge, resolved_id, placeholder_id, 0.75, state.updates
            )
            state.inherits_resolved += r_delta
            state.edges_reset += reset_delta


def _resolve_self_or_cls_call(
    state: _LinkerState,
    caller_id: str,
    target: str,
) -> str | None:
    """Resolve ``self.method()`` or ``cls.method()`` across the enclosing class hierarchy."""
    member_name = target.split(".", 1)[1] if "." in target else target
    enclosing_cls_id = _find_enclosing_class_id(state.index, caller_id)
    if enclosing_cls_id is None or "." in member_name:
        return None
    return _pick_member_on_class_hierarchy(
        state.index, enclosing_cls_id, member_name, state.class_bases
    )


def _resolve_super_call(
    state: _LinkerState,
    caller_id: str,
    target: str,
) -> str | None:
    """Resolve ``super().method()`` strictly on ancestor base classes."""
    method_name = target[len("super().") :]
    if "." in method_name:
        return None
    enclosing_cls_id = _find_enclosing_class_id(state.index, caller_id)
    if enclosing_cls_id is None:
        return None
    return _pick_member_on_class_hierarchy(
        state.index,
        enclosing_cls_id,
        method_name,
        state.class_bases,
        include_start=False,
    )


def _find_unqualified_call_in_scopes(
    index: _RepoGraphIndex,
    caller_id: str,
    target: str,
) -> str | None:
    """Search enclosing function/module scopes (skipping class bodies per Python LEGB)."""
    cur_scope_id: str | None = caller_id
    visited_scopes: set[str] = set()
    while cur_scope_id is not None and cur_scope_id not in visited_scopes:
        visited_scopes.add(cur_scope_id)
        scope_node = index.node_by_id.get(cur_scope_id)
        if scope_node is None:
            break
        if scope_node["kind"] != "class" or cur_scope_id == caller_id:
            child = index.find_direct_child(
                cur_scope_id,
                target,
                prefer_kinds=_CALLABLE_KINDS | {"class"},
            )
            if child is not None:
                return child["id"]
        cur_scope_id = scope_node.get("parent_id")
    return None


def _resolve_bare_name_call(
    state: _LinkerState,
    caller_id: str,
    ev_file_id: str | None,
    target: str,
) -> str | None:
    """Resolve an unqualified ``func()`` or ``Cls()`` call via LEGB scopes and imports."""
    scoped_id = _find_unqualified_call_in_scopes(state.index, caller_id, target)
    if scoped_id is not None:
        return scoped_id

    bound = state.file_bindings.get(ev_file_id, {}).get(target) if ev_file_id else None
    if bound and bound in state.index.node_by_id:
        return bound

    caller_node = state.index.node_by_id.get(caller_id)
    if (
        caller_node is not None
        and caller_node["name"] == target
        and caller_node["kind"] in _CALLABLE_KINDS
    ):
        return caller_id
    return None


def _resolve_single_call_target(
    state: _LinkerState,
    edge: dict[str, Any],
    target: str,
    qualifier: str | None,
) -> str | None:
    """Resolve the target symbol ID for one ``calls`` edge."""
    caller_id = edge["from_id"]
    ev_file_id = edge.get("evidence_file_id")
    if qualifier in ("self", "cls"):
        return _resolve_self_or_cls_call(state, caller_id, target)
    if qualifier == "module":
        return _resolve_prefixed_target(state, ev_file_id, target)
    if qualifier is None and target.startswith("super()."):
        return _resolve_super_call(state, caller_id, target)
    if qualifier is None and "." not in target:
        return _resolve_bare_name_call(state, caller_id, ev_file_id, target)
    return None


def _resolve_call_pass(state: _LinkerState, call_edges: list[dict[str, Any]]) -> None:
    """Execute Pass 3: resolve ``calls`` edges using scopes, imports, and class hierarchy."""
    for edge in call_edges:
        state.checked_count += 1
        payload = _parse_payload(edge.get("payload_json"))
        target = payload.get("target") or edge["to_id"].removeprefix("unresolved_call:")
        placeholder_id = f"unresolved_call:{target}"
        resolved_id = _resolve_single_call_target(
            state, edge, target, payload.get("qualifier")
        )
        r_delta, reset_delta = _stage_edge_update(
            edge, resolved_id, placeholder_id, 0.7, state.updates
        )
        state.calls_resolved += r_delta
        state.edges_reset += reset_delta


def _load_candidate_edges(conn: Connection, repo_id: str) -> list[dict[str, Any]]:
    """Fetch ``imports``, ``inherits``, and ``calls`` edges for ``repo_id``."""
    edge_rows = (
        conn.execute(
            select(
                edges_table.c.id,
                edges_table.c.kind,
                edges_table.c.from_id,
                edges_table.c.to_id,
                edges_table.c.confidence,
                edges_table.c.evidence_file_id,
                edges_table.c.payload_json,
            )
            .where(
                edges_table.c.repo_id == repo_id,
                edges_table.c.kind.in_((EDGE_KIND_IMPORTS, EDGE_KIND_INHERITS, EDGE_KIND_CALLS)),
            )
            .order_by(edges_table.c.kind, edges_table.c.id)
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in edge_rows]


def _persist_edge_updates(conn: Connection, updates: list[dict[str, Any]]) -> None:
    """Batch-persist staged edge target and confidence updates."""
    if not updates:
        return
    conn.execute(
        update(edges_table)
        .where(edges_table.c.id == bindparam("b_id"))
        .values(
            to_id=bindparam("b_to_id"),
            confidence=bindparam("b_confidence"),
        ),
        updates,
    )


def resolve_repo_edges(
    conn: Connection,
    repo_id: str,
    *,
    file_id: str | None = None,
) -> dict[str, Any]:
    """Resolve intra-repo ``imports``, ``inherits``, and ``calls`` edges against SQLite nodes."""
    state = _LinkerState(index=_RepoGraphIndex(conn, repo_id), file_id=file_id)
    all_edges = _load_candidate_edges(conn, repo_id)

    import_edges = [e for e in all_edges if e["kind"] == EDGE_KIND_IMPORTS]
    inherit_edges = [e for e in all_edges if e["kind"] == EDGE_KIND_INHERITS]
    call_edges = [
        e
        for e in all_edges
        if e["kind"] == EDGE_KIND_CALLS
        and (file_id is None or e["evidence_file_id"] == file_id)
    ]

    _resolve_import_pass(state, import_edges)
    _resolve_inherit_pass(state, inherit_edges)
    _resolve_call_pass(state, call_edges)
    _persist_edge_updates(conn, state.updates)

    return {
        "repo_id": repo_id,
        "file_id": file_id,
        "edges_checked": state.checked_count,
        "imports_resolved": state.imports_resolved,
        "inherits_resolved": state.inherits_resolved,
        "calls_resolved": state.calls_resolved,
        "total_resolved": (
            state.imports_resolved + state.inherits_resolved + state.calls_resolved
        ),
        "edges_reset": state.edges_reset,
    }
