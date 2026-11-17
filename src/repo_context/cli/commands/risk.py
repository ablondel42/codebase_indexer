"""Handler for `rc risk`."""

from __future__ import annotations

import argparse
from typing import Any

from repo_context.graph import RiskResult, analyze_target_set_risk
from repo_context.storage import connection_scope, initialize_database
from repo_context.cli.formatting import (
    format_ascii_table,
    format_key_value_section,
    format_location,
    print_json,
)
from repo_context.cli.utils import resolve_db_path, resolve_symbol_targets


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
    if getattr(args, "json", False):
        print_json(serialized)
        return 0

    facts = result.facts
    summary_section = format_key_value_section(
        "Risk Assessment",
        [
            ("Risk Score", f"{result.risk_score}/100"),
            ("Decision", result.decision),
            ("Targets", facts.target_count),
            ("Touches Public Surface", facts.touches_public_surface),
            ("Local Scope Only", facts.touches_local_scope_only),
            ("Targets Multi-File", facts.target_spans_multiple_files),
            ("Targets Multi-Module", facts.target_spans_multiple_modules),
            ("Cross-File Impact", facts.cross_file_impact),
            ("Cross-Module Impact", facts.cross_module_impact),
            ("Inheritance Involved", facts.inheritance_involved),
            ("Stale Symbols", len(facts.stale_symbols)),
            ("Low-Confidence Symbols", len(facts.low_confidence_symbols)),
            (
                "Low-Confidence Edges",
                facts.extra.get("low_confidence_edge_count", len(facts.low_confidence_edges)),
            ),
        ],
    )

    targets_table = format_ascii_table(
        ["ID", "Kind", "Location", "Qualified Name", "Signature", "Refs"],
        [
            [
                t.symbol_id,
                t.kind,
                format_location(t.file_path, start_line=t.start_line, end_line=t.end_line),
                t.qualified_name,
                t.signature or t.qualified_name,
                facts.reference_counts.get(t.symbol_id, 0),
            ]
            for t in result.targets
        ],
        max_col_widths=[None, 18, None, 44, 56, 6],
        alignments=["left", "left", "left", "left", "left", "right"],
    )

    issues_table = format_ascii_table(
        ["#", "Issue Code"],
        [[idx + 1, issue] for idx, issue in enumerate(result.issues)],
        alignments=["right", "left"],
        empty_message="(no risk issues detected)",
    )

    print(
        f"{summary_section}\n\n"
        f"Targets\n-------\n{targets_table}\n\n"
        f"Issues ({len(result.issues)})\n------\n{issues_table}"
    )
    return 0
