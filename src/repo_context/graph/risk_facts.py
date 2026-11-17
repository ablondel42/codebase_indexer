"""Risk fact extraction from stored graph and disk state."""

import sqlite3

from repo_context.constants import EDGE_KIND_INHERITS, EDGE_KIND_REFERENCES
from repo_context.graph.queries import check_file_disk_freshness
from repo_context.graph.references import (
    AST_DEPENDENCY_EDGE_KINDS,
    build_reference_stats,
)
from repo_context.graph.risk_targets import is_public_like
from repo_context.graph.risk_types import RiskFacts, RiskTarget
from repo_context.storage.reference_refresh import get_reference_refresh_state

LOW_CONFIDENCE_EDGE_LIMIT = 25


def get_reference_count(conn: sqlite3.Connection, target_id: str) -> int:
    """Get the number of stored LSP ``references`` edges targeting ``target_id``."""
    row = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = ? AND to_id = ?",
        (EDGE_KIND_REFERENCES, target_id),
    ).fetchone()
    return int(row[0]) if row else 0


def get_reference_availability(conn: sqlite3.Connection, target_id: str) -> bool:
    """Return True if LSP references were successfully refreshed for ``target_id``."""
    return get_reference_refresh_state(conn, target_id)["available"]


def get_referencing_file_count(conn: sqlite3.Connection, target_id: str) -> int:
    """Get the number of distinct files referencing ``target_id``."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT COALESCE(e.evidence_file_id, n.file_id))
        FROM edges e
        LEFT JOIN nodes n ON e.from_id = n.id
        WHERE e.kind = ? AND e.to_id = ?
        """,
        (EDGE_KIND_REFERENCES, target_id),
    ).fetchone()
    return int(row[0]) if row else 0


def get_referencing_module_count(conn: sqlite3.Connection, target_id: str) -> int:
    """Get the number of distinct ``files.module_path`` values referencing ``target_id``."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT COALESCE(f.module_path, e.evidence_file_id, n.file_id))
        FROM edges e
        LEFT JOIN nodes n ON e.from_id = n.id
        LEFT JOIN files f ON f.id = COALESCE(e.evidence_file_id, n.file_id)
        WHERE e.kind = ? AND e.to_id = ?
        """,
        (EDGE_KIND_REFERENCES, target_id),
    ).fetchone()
    return int(row[0]) if row else 0


def target_has_inheritance_risk(conn: sqlite3.Connection, target: RiskTarget) -> bool:
    """Check if a target participates in inheritance (as subclass or base class)."""
    if target.scope == "function":
        return False

    if target.kind == "class":
        row = conn.execute(
            """
            SELECT 1
            FROM edges
            WHERE kind = ? AND (from_id = ? OR to_id = ?)
            LIMIT 1
            """,
            (EDGE_KIND_INHERITS, target.symbol_id, target.symbol_id),
        ).fetchone()
        return row is not None

    if target.kind in ("method", "async_method", "attribute"):
        parent_row = conn.execute(
            "SELECT parent_id FROM nodes WHERE id = ?",
            (target.symbol_id,),
        ).fetchone()
        if parent_row and parent_row["parent_id"]:
            parent_id = parent_row["parent_id"]
            edge_row = conn.execute(
                """
                SELECT 1
                FROM edges
                WHERE kind = ? AND (from_id = ? OR to_id = ?)
                LIMIT 1
                """,
                (EDGE_KIND_INHERITS, parent_id, parent_id),
            ).fetchone()
            return edge_row is not None

    return False


def collect_stale_symbols(conn: sqlite3.Connection, targets: list[RiskTarget]) -> list[str]:
    """Collect stale symbol IDs via ``last_indexed_at`` and confined disk hash checks."""
    stale: list[str] = []
    file_fresh_cache: dict[str, bool] = {}

    for target in targets:
        row = conn.execute(
            "SELECT file_id, last_indexed_at FROM nodes WHERE id = ?",
            (target.symbol_id,),
        ).fetchone()
        if row is None or row["last_indexed_at"] is None:
            stale.append(target.symbol_id)
            continue

        file_id = row["file_id"]
        if file_id not in file_fresh_cache:
            is_fresh, _reason = check_file_disk_freshness(conn, file_id)
            file_fresh_cache[file_id] = is_fresh

        if not file_fresh_cache[file_id]:
            stale.append(target.symbol_id)

    return stale


def collect_low_confidence_symbols(
    conn: sqlite3.Connection,
    targets: list[RiskTarget],
    threshold: float = 0.8,
) -> list[str]:
    """Collect symbol IDs whose confidence falls below ``threshold``."""
    low_conf: list[str] = []
    for target in targets:
        row = conn.execute(
            "SELECT confidence FROM nodes WHERE id = ?",
            (target.symbol_id,),
        ).fetchone()
        if row and row["confidence"] is not None and row["confidence"] < threshold:
            low_conf.append(target.symbol_id)
    return low_conf


def _query_low_confidence_edges(
    conn: sqlite3.Connection,
    targets: list[RiskTarget],
    threshold: float = 0.8,
    *,
    limit: int = LOW_CONFIDENCE_EDGE_LIMIT,
) -> tuple[list[str], int]:
    """Return capped low-confidence edge IDs and the total matching count."""
    if not targets or limit <= 0:
        return [], 0

    target_ids = [t.symbol_id for t in targets]
    placeholders = ",".join("?" * len(target_ids))

    count_row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM edges
        WHERE kind = ?
          AND to_id IN ({placeholders})
          AND confidence IS NOT NULL
          AND confidence < ?
        """,
        (EDGE_KIND_REFERENCES, *target_ids, threshold),
    ).fetchone()
    total_count = int(count_row[0]) if count_row else 0

    rows = conn.execute(
        f"""
        SELECT id
        FROM edges
        WHERE kind = ?
          AND to_id IN ({placeholders})
          AND confidence IS NOT NULL
          AND confidence < ?
        ORDER BY id
        LIMIT ?
        """,
        (EDGE_KIND_REFERENCES, *target_ids, threshold, int(limit)),
    ).fetchall()
    return [row["id"] for row in rows], total_count


def collect_low_confidence_edges(
    conn: sqlite3.Connection,
    targets: list[RiskTarget],
    threshold: float = 0.8,
    *,
    limit: int = LOW_CONFIDENCE_EDGE_LIMIT,
) -> list[str]:
    """Collect capped low-confidence reference edge IDs targeting ``targets``."""
    edge_ids, _total_count = _query_low_confidence_edges(
        conn, targets, threshold=threshold, limit=limit
    )
    return edge_ids


def _get_referencing_sites(
    conn: sqlite3.Connection,
    target_id: str,
    kinds: tuple[str, ...],
) -> list[tuple[str | None, str | None]]:
    """Return distinct ``(file_id, module_path)`` pairs for edges targeting ``target_id``."""
    placeholders = ",".join("?" * len(kinds))
    rows = conn.execute(
        f"""
        SELECT DISTINCT
               COALESCE(e.evidence_file_id, n.file_id) AS ref_file_id,
               f.module_path AS ref_module_path
        FROM edges e
        LEFT JOIN nodes n ON e.from_id = n.id
        LEFT JOIN files f ON f.id = COALESCE(e.evidence_file_id, n.file_id)
        WHERE e.kind IN ({placeholders}) AND e.to_id = ?
        """,
        (*kinds, target_id),
    ).fetchall()
    return [(row["ref_file_id"], row["ref_module_path"]) for row in rows]


def build_risk_facts(conn: sqlite3.Connection, targets: list[RiskTarget]) -> RiskFacts:
    """Build deterministic risk facts for a normalized list of targets."""
    if not targets:
        return RiskFacts()

    facts = RiskFacts(
        target_count=len(targets),
        symbol_ids=[t.symbol_id for t in targets],
        symbol_kinds={t.kind for t in targets},
    )

    all_file_ids: set[str] = set()
    all_module_paths: set[str] = set()
    reference_sources: dict[str, str] = {}
    any_public = False
    all_function_scope = True
    any_inheritance = False

    for target in targets:
        stats = build_reference_stats(conn, target.symbol_id)
        facts.reference_counts[target.symbol_id] = stats["reference_count"]
        facts.reference_availability[target.symbol_id] = stats["available"]
        facts.referencing_file_counts[target.symbol_id] = stats["referencing_file_count"]
        facts.referencing_module_counts[target.symbol_id] = stats["referencing_module_count"]
        reference_sources[target.symbol_id] = stats["reference_source"]

        if target.file_id:
            all_file_ids.add(target.file_id)
        if target.module_path:
            all_module_paths.add(target.module_path)
        elif target.file_id:
            all_module_paths.add(target.file_id)

        if is_public_like(target):
            any_public = True
        if target.scope != "function":
            all_function_scope = False
        if target_has_inheritance_risk(conn, target):
            any_inheritance = True

        edge_kinds = (
            (EDGE_KIND_REFERENCES,)
            if stats["reference_source"] == "lsp"
            else AST_DEPENDENCY_EDGE_KINDS
        )
        sites = _get_referencing_sites(conn, target.symbol_id, edge_kinds)
        if sites:
            ref_files = {fid for fid, _ in sites if fid}
            ref_modules = {mod for _, mod in sites if mod}

            if any(fid != target.file_id for fid in ref_files):
                facts.cross_file_impact = True

            if target.module_path:
                if any(mod != target.module_path for mod in ref_modules):
                    facts.cross_module_impact = True
            elif len(ref_modules) > 1:
                facts.cross_module_impact = True

    facts.touches_public_surface = any_public
    facts.touches_local_scope_only = all_function_scope and len(targets) > 0
    facts.target_spans_multiple_files = len(all_file_ids) > 1
    facts.target_spans_multiple_modules = len(all_module_paths) > 1
    facts.inheritance_involved = any_inheritance

    facts.stale_symbols = collect_stale_symbols(conn, targets)
    facts.low_confidence_symbols = collect_low_confidence_symbols(conn, targets)
    low_edge_ids, total_low_edges = _query_low_confidence_edges(conn, targets)
    facts.low_confidence_edges = low_edge_ids

    facts.extra["all_references_available"] = all(facts.reference_availability.values())
    facts.extra["any_references_unavailable"] = not all(
        facts.reference_availability.get(tid, False) for tid in facts.symbol_ids
    )
    facts.extra["reference_sources"] = reference_sources
    facts.extra["low_confidence_edge_count"] = total_low_edges
    facts.extra["low_confidence_edges_truncated"] = total_low_edges > len(low_edge_ids)

    return facts
