"""Shared pure domain-to-dictionary JSON projections for CLI and MCP wire payloads."""

from collections.abc import Mapping
from typing import Any

from context.models.source import SymbolSnippet
from core.models.node import SymbolSummary
from core.models.search import CodeMatchItem, CodeSearchReport
from core.utils.file_io import format_line_span
from engine.models.responses import SymbolSearchPage
from graph.models.references import (
    ReferenceSiteEntry,
    ReferenceTargetSummary,
    SymbolReferencesReport,
)
from graph.models.risk import RiskAssessment, RiskTarget
from graph.utils.predicates import derive_target_lines


def omit_none(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a shallow dictionary copy excluding entries whose value is ``None``."""
    return {key: val for key, val in data.items() if val is not None}


def derive_symbol_summary_lines(symbol: SymbolSummary) -> str | None:
    """Derive a ``start-end`` line span string from ``SymbolSummary`` bounds."""
    return format_line_span(
        *derive_target_lines(symbol.start_line, symbol.end_line, symbol.decl_range)
    )


def project_symbol_summary(
    symbol: SymbolSummary,
    *,
    compact: bool = False,
) -> dict[str, Any]:
    """Project a ``SymbolSummary`` into CLI (``compact=False``) or MCP (``compact=True``) shape."""
    base: dict[str, Any] = {
        "id": symbol.id,
        "kind": str(symbol.kind),
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "location": symbol.location,
        "file_path": symbol.file_path or None,
        "module_path": symbol.module_path or None,
    }
    if not compact:
        return {
            **base,
            "start_line": symbol.start_line,
            "end_line": symbol.end_line,
            "signature": symbol.signature,
            "doc_summary": symbol.doc_summary,
        }
    return omit_none({
        **base,
        "lines": derive_symbol_summary_lines(symbol),
        "signature": symbol.signature,
        "doc_summary": symbol.doc_summary,
        "scope": str(symbol.scope) if symbol.scope is not None else None,
        "visibility_hint": symbol.visibility_hint,
    })


def project_symbol_search(
    page: SymbolSearchPage,
    *,
    compact: bool = False,
) -> dict[str, Any]:
    """Project a ``SymbolSearchPage`` into CLI or compact MCP wire dictionary shape."""
    return {
        "repo_id": page.repo_id,
        "query": page.query,
        "count": page.count,
        "symbols": [project_symbol_summary(item, compact=compact) for item in page.symbols],
    }


def project_symbol_source(
    snippet: SymbolSnippet,
    *,
    drop_none: bool = False,
) -> dict[str, Any]:
    """Project a ``SymbolSnippet`` onto ``source`` for CLI and MCP wire parity."""
    raw: dict[str, Any] = {
        "symbol_id": snippet.symbol_id,
        "qualified_name": snippet.qualified_name,
        "kind": str(snippet.kind),
        "location": snippet.location,
        "module_path": snippet.module_path,
        "lines": snippet.lines,
        "signature": snippet.signature,
        "file_path": snippet.file_path,
        "start_line": snippet.start_line,
        "end_line": snippet.end_line,
        "source": snippet.numbered_source,
        "truncated": snippet.truncated,
        "total_lines": snippet.total_lines,
        "returned_lines": snippet.returned_lines,
        "next_offset": snippet.next_offset,
        "is_fresh": snippet.is_fresh,
        "stale_reason": snippet.stale_reason,
    }
    return omit_none(raw) if drop_none else raw


def _project_reference_target(
    target: ReferenceTargetSummary,
    *,
    compact: bool,
) -> dict[str, Any]:
    """Project a ``ReferenceTargetSummary`` for CLI or compact MCP output."""
    if not compact:
        return {
            "id": target.id,
            "kind": str(target.kind),
            "qualified_name": target.qualified_name,
            "location": target.location,
            "file_path": target.file_path,
            "lines": target.lines,
            "signature": target.signature,
        }
    return omit_none({
        "id": target.id,
        "kind": str(target.kind),
        "name": target.name,
        "qualified_name": target.qualified_name,
        "location": target.location,
        "file_path": target.file_path,
        "module_path": target.module_path,
        "lines": target.lines,
        "signature": target.signature,
        "doc_summary": target.doc_summary,
        "scope": str(target.scope) if target.scope is not None else None,
        "visibility_hint": target.visibility_hint,
    })


def project_reference_site(
    site: ReferenceSiteEntry,
    *,
    compact: bool = False,
) -> dict[str, Any]:
    """Project one ``ReferenceSiteEntry`` for CLI or compact MCP output."""
    if not compact:
        return {
            "id": site.id,
            "direction": site.direction,
            "kind": str(site.kind),
            "from_id": site.from_id,
            "to_id": site.to_id,
            "peer_id": site.peer_id,
            "peer_name": site.peer_name,
            "location": site.location,
            "file_path": site.file_path,
            "line": site.line,
            "column": site.column,
            "confidence": site.confidence,
            "source": site.source,
            "preview": site.preview,
        }
    return omit_none({
        "id": site.id or None,
        "kind": str(site.kind),
        "direction": site.direction,
        "from_id": site.from_id,
        "from_name": site.from_name,
        "to_id": site.to_id,
        "to_name": site.to_name,
        "location": site.location,
        "file_path": site.file_path,
        "line": site.line,
        "column": site.column,
        "confidence": site.confidence,
        "source": site.source,
        "preview": site.preview,
    })


def project_symbol_references(
    report: SymbolReferencesReport,
    *,
    compact: bool = False,
) -> dict[str, Any]:
    """Project a ``SymbolReferencesReport`` into CLI or compact MCP wire dictionary shape."""
    summary_dict = report.stats.model_dump(mode="json")
    return {
        "symbol": _project_reference_target(report.symbol, compact=compact),
        "direction": str(report.direction),
        "total_count": report.total_count,
        "returned_count": report.returned_count,
        "offset": report.offset,
        "limit": report.limit,
        "has_more": report.has_more,
        "reference_summary": omit_none(summary_dict) if compact else summary_dict,
        "references": [project_reference_site(site, compact=compact) for site in report.references],
    }


def project_risk_target(target: RiskTarget) -> dict[str, Any]:
    """Project one ``RiskTarget`` omitting ``None`` entries."""
    return omit_none({
        "symbol_id": target.symbol_id,
        "qualified_name": target.qualified_name,
        "kind": str(target.kind),
        "scope": target.scope,
        "location": target.location,
        "file_path": target.file_path,
        "module_path": target.module_path,
        "lines": format_line_span(target.start_line, target.end_line),
        "signature": target.signature,
        "is_test": target.is_test,
    })


def project_risk_assessment(result: RiskAssessment) -> dict[str, Any]:
    """Project a ``RiskAssessment`` into the deterministic CLI/MCP dictionary shape."""
    facts = result.facts
    return {
        "risk_score": result.risk_score,
        "decision": str(result.decision),
        "issues": list(result.issues),
        "targets": [project_risk_target(target) for target in result.targets],
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


def project_code_match(item: CodeMatchItem) -> dict[str, Any]:
    """Project a single ``CodeMatchItem`` omitting empty context tuples and null fields."""
    return omit_none({
        "location": item.location,
        "file_path": item.file_path,
        "language": item.language,
        "line": item.line,
        "column": item.column,
        "preview": item.preview,
        "context_before": list(item.context_before) if item.context_before else None,
        "context_after": list(item.context_after) if item.context_after else None,
        "symbol_id": item.symbol_id,
        "qualified_name": item.qualified_name,
        "symbol_kind": item.symbol_kind,
        "symbol_lines": item.symbol_lines,
        "symbol_location": item.symbol_location,
    })


def project_code_search(report: CodeSearchReport) -> dict[str, Any]:
    """Project a ``CodeSearchReport`` into the deterministic CLI/MCP dictionary shape."""
    return {
        "repo_id": report.repo_id,
        "query": report.query,
        "regex": report.regex,
        "case_sensitive": report.case_sensitive,
        "total_matches": report.total_matches,
        "matched_file_count": report.matched_file_count,
        "returned_count": report.returned_count,
        "offset": report.offset,
        "limit": report.limit,
        "has_more": report.has_more,
        "file_counts": [fc.model_dump() for fc in report.file_counts],
        "matches": [project_code_match(item) for item in report.matches],
        "stats": report.stats.model_dump(),
    }


__all__ = [
    "derive_symbol_summary_lines",
    "omit_none",
    "project_code_match",
    "project_code_search",
    "project_reference_site",
    "project_risk_assessment",
    "project_risk_target",
    "project_symbol_references",
    "project_symbol_search",
    "project_symbol_source",
    "project_symbol_summary",
]
