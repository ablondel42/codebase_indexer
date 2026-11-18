"""Risk target normalization and loading using SQLAlchemy Core."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from repo_context.graph.risk_types import RiskTarget
from repo_context.storage.schema import files_table, nodes_table


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    """Parse a JSON dict field safely."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _derive_lines_from_range(range_json: Any) -> tuple[int | None, int | None]:
    """Derive 1-based inclusive start/end lines from a zero-based range dict."""
    rng = _parse_json_dict(range_json)
    start = rng.get("start")
    end = rng.get("end")
    if isinstance(start, dict) and isinstance(end, dict):
        start_line = start.get("line")
        end_line = end.get("line")
        if isinstance(start_line, int) and isinstance(end_line, int):
            return start_line + 1, end_line + 1
    return None, None


def load_risk_targets(conn: Connection, symbol_ids: list[str]) -> list[RiskTarget]:
    """Load and normalize risk targets from symbol IDs joined with file metadata."""
    targets: list[RiskTarget] = []
    seen_ids: set[str] = set()

    stmt_base = select(
        nodes_table,
        files_table.c.file_path.label("f_file_path"),
        files_table.c.module_path.label("f_module_path"),
        files_table.c.is_test.label("f_is_test"),
    ).select_from(
        nodes_table.outerjoin(files_table, files_table.c.id == nodes_table.c.file_id)
    )

    for symbol_id in symbol_ids:
        if symbol_id in seen_ids:
            continue
        seen_ids.add(symbol_id)

        row = conn.execute(stmt_base.where(nodes_table.c.id == symbol_id)).mappings().first()
        if row is None:
            raise ValueError(f"Symbol not found: {symbol_id}")

        payload = _parse_json_dict(row["payload_json"])
        file_path = (
            row["f_file_path"]
            if row["f_file_path"] is not None
            else payload.get("file_path", "")
        )
        module_path = (
            row["f_module_path"]
            if row["f_module_path"] is not None
            else payload.get("module_path", "")
        )
        is_test = bool(row["f_is_test"]) if row["f_is_test"] is not None else False

        start_line = row["start_line"]
        end_line = row["end_line"]
        if start_line is None or end_line is None:
            fallback_start, fallback_end = _derive_lines_from_range(row["range_json"])
            if start_line is None:
                start_line = fallback_start
            if end_line is None:
                end_line = fallback_end

        targets.append(
            RiskTarget(
                symbol_id=row["id"],
                qualified_name=row["qualified_name"],
                kind=row["kind"],
                scope=row["scope"] or "module",
                file_id=row["file_id"],
                file_path=file_path or "",
                module_path=module_path or "",
                visibility_hint=row["visibility_hint"],
                lexical_parent_id=row["lexical_parent_id"],
                is_test=is_test,
                start_line=start_line,
                end_line=end_line,
                signature=row["signature"],
                repo_id=row["repo_id"],
            )
        )

    return targets


def is_public_like(target: RiskTarget) -> bool:
    """Determine if a target is public-like."""
    if target.scope == "function":
        return False

    if target.visibility_hint == "public":
        return True

    short_name = target.qualified_name.split(".")[-1] if target.qualified_name else ""
    if short_name.startswith("__") and short_name.endswith("__"):
        return True

    if not short_name.startswith("_"):
        return True

    return False
