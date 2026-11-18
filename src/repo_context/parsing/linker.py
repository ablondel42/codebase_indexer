"""Intra-repository AST edge linker for imports, inheritance, and call-sites."""

from collections import defaultdict, deque
import json
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
            node = dict(row)
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


def _resolve_prefixed_target(
    index: _RepoGraphIndex,
    ev_file_id: str | None,
    dotted_name: str,
    file_bindings: dict[str, dict[str, str]],
    dotted_import_map: dict[str, dict[str, str]],
    class_bases: dict[str, list[str]],
    *,
    prefer_kind: str | None = None,
) -> str | None:
    """Resolve a dotted reference like ``utils.helper`` or ``pkg.utils.Cls`` via imports."""
    if not ev_file_id or "." not in dotted_name:
        return None
    parts = dotted_name.split(".")
    file_dotted = dotted_import_map.get(ev_file_id, {})
    file_syms = file_bindings.get(ev_file_id, {})

    ev_module = index.module_by_file_id.get(ev_file_id)

    for prefix_len in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:prefix_len])
        remainder = parts[prefix_len:]
        bound_id = file_dotted.get(prefix) or file_syms.get(prefix)
        if not bound_id and prefix_len == 1 and ev_module is not None:
            same_file_child = index.find_direct_child(ev_module["id"], prefix)
            if same_file_child is not None:
                bound_id = same_file_child["id"]
        if not bound_id:
            continue
        bound_node = index.node_by_id.get(bound_id)
        if bound_node is None:
            continue

        if bound_node["kind"] == "module":
            target_qname = f"{bound_node['qualified_name']}.{'.'.join(remainder)}"
            target = index.find_node(target_qname, prefer_kind=prefer_kind)
            if target is not None:
                return target["id"]
            if len(remainder) == 1:
                reexported = file_bindings.get(bound_node["file_id"], {}).get(remainder[0])
                if reexported:
                    return reexported

        elif bound_node["kind"] == "class" and len(remainder) == 1:
            member_prefer = {prefer_kind} if prefer_kind else _CALLABLE_KINDS
            member_id = _pick_member_on_class_hierarchy(
                index,
                bound_id,
                remainder[0],
                class_bases,
                prefer_kinds=member_prefer,
            )
            if member_id is not None:
                return member_id

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

    if not current_to.startswith(_UNRESOLVED_PREFIXES):
        if current_to != placeholder_id:
            updates.append(
                {
                    "b_id": edge["id"],
                    "b_to_id": placeholder_id,
                    "b_confidence": default_confidence,
                }
            )
            return 0, 1
    return 0, 0


def resolve_repo_edges(
    conn: Connection,
    repo_id: str,
    *,
    file_id: str | None = None,
) -> dict[str, Any]:
    """Resolve intra-repo ``imports``, ``inherits``, and ``calls`` edges against SQLite nodes."""
    index = _RepoGraphIndex(conn, repo_id)

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
    all_edges = [dict(row) for row in edge_rows]

    import_edges = [e for e in all_edges if e["kind"] == EDGE_KIND_IMPORTS]
    inherit_edges = [e for e in all_edges if e["kind"] == EDGE_KIND_INHERITS]
    call_edges = [
        e
        for e in all_edges
        if e["kind"] == EDGE_KIND_CALLS
        and (file_id is None or e["evidence_file_id"] == file_id)
    ]

    updates: list[dict[str, Any]] = []
    file_bindings: dict[str, dict[str, str]] = defaultdict(dict)
    dotted_import_map: dict[str, dict[str, str]] = defaultdict(dict)
    imports_resolved = 0
    inherits_resolved = 0
    calls_resolved = 0
    edges_reset = 0
    checked_count = 0

    # --- Pass 1: Imports ---
    import_resolution_cache: list[tuple[dict, dict, str, str | None]] = []
    for edge in import_edges:
        payload = _parse_payload(edge.get("payload_json"))
        import_type = payload.get("import_type")
        mod_name = payload.get("module") or ""
        alias = payload.get("alias")
        ev_file_id = edge.get("evidence_file_id")
        resolved_id: str | None = None

        if import_type == "import":
            placeholder_id = f"external_or_unresolved:{mod_name}"
            mod_node = index.find_module(mod_name)
            if mod_node is not None:
                mod_id = str(mod_node["id"])
                resolved_id = mod_id
                if ev_file_id:
                    if alias:
                        file_bindings[ev_file_id][alias] = mod_id
                        dotted_import_map[ev_file_id][alias] = mod_id
                    else:
                        dotted_import_map[ev_file_id][mod_name] = mod_id
                        top_name = mod_name.split(".")[0]
                        top_mod = index.find_module(top_name)
                        if top_mod is not None:
                            file_bindings[ev_file_id][top_name] = str(top_mod["id"])
        else:
            sym_name = payload.get("symbol")
            if not sym_name:
                raw_tail = edge["to_id"].removeprefix("external_or_unresolved:")
                if mod_name and raw_tail.startswith(f"{mod_name}."):
                    sym_name = raw_tail[len(mod_name) + 1 :]
                else:
                    sym_name = raw_tail
            target = f"{mod_name}.{sym_name}" if mod_name else (sym_name or "")
            placeholder_id = f"external_or_unresolved:{target}"

            if sym_name == "*" or not sym_name:
                mod_node = index.find_module(mod_name)
                if mod_node is not None:
                    resolved_id = mod_node["id"]
            else:
                sym_node = index.find_node(target)
                if sym_node is not None:
                    resolved_id = sym_node["id"]
                else:
                    submod = index.find_module(target)
                    if submod is not None:
                        resolved_id = submod["id"]

                if resolved_id is not None and ev_file_id:
                    bound_name = alias or sym_name
                    file_bindings[ev_file_id][bound_name] = resolved_id
                    if index.node_by_id[resolved_id]["kind"] == "module":
                        dotted_import_map[ev_file_id][bound_name] = resolved_id

        import_resolution_cache.append((edge, payload, placeholder_id, resolved_id))

    # Resolve package __init__ re-exports (up to 3 hops) for unresolved `from` imports
    for _ in range(3):
        progressed = False
        for i, (edge, payload, placeholder_id, resolved_id) in enumerate(import_resolution_cache):
            if resolved_id is not None or payload.get("import_type") != "from":
                continue
            mod_name = payload.get("module") or ""
            sym_name = payload.get("symbol") or ""
            if not mod_name or not sym_name or sym_name == "*":
                continue
            mod_node = index.find_module(mod_name)
            if mod_node is None:
                continue
            reexported_id = file_bindings.get(mod_node["file_id"], {}).get(sym_name)
            if reexported_id and reexported_id in index.node_by_id:
                import_resolution_cache[i] = (edge, payload, placeholder_id, reexported_id)
                ev_file_id = edge.get("evidence_file_id")
                if ev_file_id:
                    bound_name = payload.get("alias") or sym_name
                    file_bindings[ev_file_id][bound_name] = reexported_id
                    if index.node_by_id[reexported_id]["kind"] == "module":
                        dotted_import_map[ev_file_id][bound_name] = reexported_id
                progressed = True
        if not progressed:
            break

    for edge, _payload, placeholder_id, resolved_id in import_resolution_cache:
        if file_id is not None and edge.get("evidence_file_id") != file_id:
            continue
        checked_count += 1
        if resolved_id is not None and resolved_id not in index.node_by_id:
            resolved_id = None
        r_delta, reset_delta = _stage_edge_update(
            edge, resolved_id, placeholder_id, 0.8, updates
        )
        imports_resolved += r_delta
        edges_reset += reset_delta

    # --- Pass 2: Inheritance ---
    class_bases: dict[str, list[str]] = defaultdict(list)
    for edge in inherit_edges:
        payload = _parse_payload(edge.get("payload_json"))
        base_name = payload.get("base_name") or edge["to_id"].removeprefix("unresolved_base:")
        placeholder_id = f"unresolved_base:{base_name}"
        lookup_name = base_name.split("[", 1)[0].strip()
        ev_file_id = edge.get("evidence_file_id")
        from_node = index.node_by_id.get(edge["from_id"])
        resolved_id = None

        if lookup_name:
            if "." not in lookup_name:
                # 1. Enclosing scopes up to the module
                cur_parent = from_node.get("parent_id") if from_node else None
                visited_parents: set[str] = set()
                while cur_parent is not None and cur_parent not in visited_parents:
                    visited_parents.add(cur_parent)
                    child = index.find_direct_child(
                        cur_parent, lookup_name, prefer_kinds={"class"}
                    )
                    if child is not None and child["id"] != edge["from_id"]:
                        resolved_id = child["id"]
                        break
                    parent_node = index.node_by_id.get(cur_parent)
                    cur_parent = parent_node.get("parent_id") if parent_node else None

                # 2. Imported symbols in file
                if resolved_id is None and ev_file_id:
                    bound = file_bindings.get(ev_file_id, {}).get(lookup_name)
                    if bound and bound in index.node_by_id:
                        resolved_id = bound
            else:
                resolved_id = _resolve_prefixed_target(
                    index,
                    ev_file_id,
                    lookup_name,
                    file_bindings,
                    dotted_import_map,
                    class_bases,
                    prefer_kind="class",
                )
                if resolved_id is None:
                    node = index.find_node(lookup_name, prefer_kind="class")
                    if node is not None:
                        resolved_id = node["id"]

        if resolved_id is not None:
            class_bases[edge["from_id"]].append(resolved_id)

        if file_id is None or ev_file_id == file_id:
            checked_count += 1
            r_delta, reset_delta = _stage_edge_update(
                edge, resolved_id, placeholder_id, 0.75, updates
            )
            inherits_resolved += r_delta
            edges_reset += reset_delta

    # --- Pass 3: Calls ---
    for edge in call_edges:
        checked_count += 1
        payload = _parse_payload(edge.get("payload_json"))
        target = payload.get("target") or edge["to_id"].removeprefix("unresolved_call:")
        qualifier = payload.get("qualifier")
        placeholder_id = f"unresolved_call:{target}"
        ev_file_id = edge.get("evidence_file_id")
        caller_id = edge["from_id"]
        resolved_id: str | None = None

        if qualifier in ("self", "cls"):
            member_name = target.split(".", 1)[1] if "." in target else target
            enclosing_cls_id = _find_enclosing_class_id(index, caller_id)
            if enclosing_cls_id is not None and "." not in member_name:
                resolved_id = _pick_member_on_class_hierarchy(
                    index, enclosing_cls_id, member_name, class_bases
                )

        elif qualifier == "module":
            resolved_id = _resolve_prefixed_target(
                index,
                ev_file_id,
                target,
                file_bindings,
                dotted_import_map,
                class_bases,
            )

        elif qualifier is None:
            if target.startswith("super()."):
                method_name = target[len("super().") :]
                if "." not in method_name:
                    enclosing_cls_id = _find_enclosing_class_id(index, caller_id)
                    if enclosing_cls_id is not None:
                        resolved_id = _pick_member_on_class_hierarchy(
                            index,
                            enclosing_cls_id,
                            method_name,
                            class_bases,
                            include_start=False,
                        )
            elif "." not in target:
                # 1. Enclosing function / module scopes (skip class bodies per Python LEGB)
                caller_node = index.node_by_id.get(caller_id)
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
                            resolved_id = child["id"]
                            break
                    cur_scope_id = scope_node.get("parent_id")

                # 2. Imported symbols in evidence file
                if resolved_id is None and ev_file_id:
                    bound = file_bindings.get(ev_file_id, {}).get(target)
                    if bound and bound in index.node_by_id:
                        resolved_id = bound
                # Also allow self-recursive module-level or class-level call check
                if (
                    resolved_id is None
                    and caller_node is not None
                    and caller_node["name"] == target
                    and caller_node["kind"] in _CALLABLE_KINDS
                ):
                    resolved_id = caller_id

        r_delta, reset_delta = _stage_edge_update(
            edge, resolved_id, placeholder_id, 0.7, updates
        )
        calls_resolved += r_delta
        edges_reset += reset_delta

    if updates:
        conn.execute(
            update(edges_table)
            .where(edges_table.c.id == bindparam("b_id"))
            .values(
                to_id=bindparam("b_to_id"),
                confidence=bindparam("b_confidence"),
            ),
            updates,
        )

    return {
        "repo_id": repo_id,
        "file_id": file_id,
        "edges_checked": checked_count,
        "imports_resolved": imports_resolved,
        "inherits_resolved": inherits_resolved,
        "calls_resolved": calls_resolved,
        "total_resolved": imports_resolved + inherits_resolved + calls_resolved,
        "edges_reset": edges_reset,
    }
