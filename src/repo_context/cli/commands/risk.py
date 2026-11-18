"""Handler for `rc risk`."""

from __future__ import annotations

import argparse
import json
from typing import Any

from repo_context.cli.utils import resolve_db_path, resolve_symbol_targets
from repo_context.graph import RiskResult, analyze_target_set_risk
from repo_context.storage import connection_scope, initialize_database


def _serialize_risk_result(result: RiskResult) -> dict[str, Any]:
    """Serialize ``RiskResult`` into a deterministic JSON-compatible dict."""
    targets = []
    for target in result.targets:
        lines = (
            f"{target.start_line}-{target.end_line}"
            if target.start_line is not None and target.end_line is not None
            else None
        )
        entry = {
            "symbol_id": target.symbol_id,
            "qualified_name": target.qualified_name,
            "kind": target.kind,
            "scope": target.scope,
            "file_path": target.file_path,
            "module_path": target.module_path,
            "lines": lines,
            "signature": target.signature,
            "is_test": target.is_test,
        }
        targets.append({k: v for k, v in entry.items() if v is not None})

    facts = result.facts
    return {
        "risk_score": result.risk_score,
        "decision": result.decision,
        "issues": list(result.issues),
        "targets": targets,
        "facts": {
            "target_count": facts.target_count,
            "symbol_ids": list(facts.symbol_ids),
            "symbol_kinds": sorted(facts.symbol_kinds),
            "reference_counts": dict(facts.reference_counts),
            "reference_availability": dict(facts.reference_availability),
            "referencing_file_counts": dict(facts.referencing_file_counts),
            "referencing_module_counts": dict(facts.referencing_module_counts),
            "touches_public_surface": facts.touches_public_surface,
            "touches_local_scope_only": facts.touches_local_scope_only,
            "target_spans_multiple_files": facts.target_spans_multiple_files,
            "target_spans_multiple_modules": facts.target_spans_multiple_modules,
            "cross_file_impact": facts.cross_file_impact,
            "cross_module_impact": facts.cross_module_impact,
            "inheritance_involved": facts.inheritance_involved,
            "stale_symbols": list(facts.stale_symbols),
            "low_confidence_symbols": list(facts.low_confidence_symbols),
            "low_confidence_edges": list(facts.low_confidence_edges),
            "extra": dict(facts.extra),
        },
    }


def cmd_risk(args: argparse.Namespace) -> int:
    """Analyze change impact and risk for one or more symbols."""
    db_path = resolve_db_path(args)
    repo_arg: str | None = getattr(args, "repo", None)
    symbol_refs: list[str] = args.id_or_names

    with connection_scope(db_path, read_only=True) as conn:
        initialize_database(conn)
        _repo_id, targets = resolve_symbol_targets(conn, repo_arg, symbol_refs)
        result = analyze_target_set_risk(conn, [t["id"] for t in targets])

    serialized = _serialize_risk_result(result)
    print(json.dumps(serialized, indent=2))
    return 0
