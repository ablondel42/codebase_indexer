"""CLI JSON output, error envelope, pipe handling, and legacy wire-shape formatters."""

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli.models.output import CliErrorDetail, CliErrorEnvelope, McpCallEnvelope
from context.models.outline import FileOutline
from context.models.source import SymbolSnippet
from context.models.symbol_context import ContextEdge, FocusSymbol, SymbolContext
from core.models.base import FrozenModel
from core.models.search import CodeSearchReport
from core.utils.logging import get_logger
from engine.models.responses import FileListPage, SymbolListPage, SymbolSearchPage
from engine.services.facade import IndexerEngine
from engine.utils.projections import (
    project_code_search,
    project_risk_assessment,
    project_symbol_references,
    project_symbol_search,
    project_symbol_source,
    project_symbol_summary,
)
from graph.models.references import SymbolReferencesReport
from graph.models.risk import RiskAssessment
from indexing.models.events import (
    WatchFileProcessedEvent,
    WatchStartedEvent,
    WatchStoppedEvent,
)
from indexing.models.results import BatchIndexResult, IncrementalStatus

logger = get_logger("cli.utils.output")


def resolve_cli_db_path(args: argparse.Namespace, default_db_path: Path) -> Path:
    """Resolve explicit ``--db-path`` on ``args`` or fall back to ``default_db_path``."""
    raw = getattr(args, "db_path", None)
    if raw is not None:
        return Path(raw)
    return default_db_path


def resolve_scoped_engine(engine: IndexerEngine, args: argparse.Namespace) -> IndexerEngine:
    """Return ``engine`` scoped to ``args.db_path`` when supplied."""
    return engine.for_db(resolve_cli_db_path(args, engine.db_path))


def emit_json(
    payload: Any,
    *,
    default: Callable[[Any], Any] | None = None,
    flush: bool = False,
) -> None:
    """Write an indented JSON payload to ``sys.stdout``.

    Args:
        payload: JSON-serializable object or dictionary.
        default: Optional fallback serializer function (e.g., ``str`` for SQL rows).
        flush: Whether to flush ``sys.stdout`` immediately.
    """
    print(json.dumps(payload, indent=2, default=default), flush=flush)


def emit_cli_error(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an indented ``CliErrorEnvelope`` JSON payload to ``sys.stderr``.

    Args:
        code: Machine-readable error code identifier.
        message: Human-readable error description.
        details: Optional structured diagnostic metadata.
    """
    envelope = CliErrorEnvelope(
        ok=False,
        error=CliErrorDetail(code=code, message=message, details=details),
    )
    print(json.dumps(envelope.model_dump(), indent=2), file=sys.stderr)


def silence_broken_pipe() -> None:
    """Redirect ``stdout`` file descriptor to ``os.devnull`` after ``BrokenPipeError``."""
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, sys.stdout.fileno())
        finally:
            os.close(devnull_fd)
    except OSError as exc:
        logger.debug("Could not redirect stdout to devnull on BrokenPipeError: %s", exc)


def format_index_result_for_cli(
    result: BatchIndexResult,
    *,
    include_tests: bool,
    force: bool,
) -> dict[str, Any]:
    """Format a ``BatchIndexResult`` into the exact ``cbi index`` JSON summary shape.

    Args:
        result: Completed ``BatchIndexResult`` from ``CommandEngine.index_repository``.
        include_tests: Effective ``--include-tests`` flag value.
        force: Effective ``--force`` flag value.

    Returns:
        Dictionary matching the legacy ``cbi index`` JSON output keys.
    """
    payload: dict[str, Any] = {
        "db_path": result.db_path,
        "run_id": result.run_id,
        "repo_id": result.repo.id,
        "repo_name": result.repo.name,
        "root_path": result.repo.root_path,
        "include_tests": include_tests,
        "force": force,
        "files_scanned": result.files_scanned,
        "files_indexed": result.files_indexed,
        "files_unchanged": result.files_unchanged,
        "files_pruned": result.files_pruned,
        "files_failed": result.files_failed,
        "nodes_extracted": result.nodes_extracted,
        "edges_extracted": result.edges_extracted,
        "linker": result.link_report.model_dump(mode="json", exclude={"updates"}),
        "totals": result.graph_stats.model_dump(mode="json"),
    }
    if result.failures:
        payload["failures"] = [item.model_dump(mode="json") for item in result.failures]
    if result.lsp_summary is not None:
        payload["lsp"] = result.lsp_summary.model_dump(mode="json")
    return payload


def _format_watch_file_event(event: WatchFileProcessedEvent) -> dict[str, Any]:
    """Format a ``WatchFileProcessedEvent`` into the ``cbi watch`` JSON event shape.

    Args:
        event: Per-file incremental watch event model.

    Returns:
        Dictionary carrying ``event="file_processed"`` and status-specific counters.
    """
    if event.status == IncrementalStatus.REINDEXED:
        return {
            "event": "file_processed",
            "file_path": event.file_path,
            "status": str(event.status),
            "node_count": event.node_count,
            "edge_count": event.edge_count,
            "invalidated_reference_edge_count": event.invalidated_reference_edge_count,
            "resolved_edge_count": event.resolved_edge_count,
        }
    if event.status in (IncrementalStatus.DELETED, IncrementalStatus.NOT_TRACKED):
        deleted_payload: dict[str, Any] = {
            "event": "file_processed",
            "file_path": event.file_path,
            "status": str(event.status),
            "deleted_node_count": event.deleted_node_count,
            "deleted_edge_count": event.deleted_edge_count,
            "invalidated_target_symbol_count": event.invalidated_target_symbol_count,
        }
        if event.reset_edge_count > 0:
            deleted_payload["reset_edge_count"] = event.reset_edge_count
        return deleted_payload

    fallback: dict[str, Any] = {
        "event": "file_processed",
        "file_path": event.file_path,
        "status": str(event.status),
        "node_count": event.node_count,
        "edge_count": event.edge_count,
    }
    if event.error_message is not None:
        fallback["error_message"] = event.error_message
    return fallback


def format_watch_event_payload(event: FrozenModel) -> dict[str, Any]:
    """Convert a typed watch callback event model into its CLI JSON stream payload.

    Args:
        event: ``WatchStartedEvent``, ``WatchFileProcessedEvent``, or ``WatchStoppedEvent``.

    Returns:
        JSON-serializable event dictionary for ``cbi watch`` stdout output.
    """
    if isinstance(event, WatchStartedEvent):
        return {
            "event": "watch_started",
            "repo_root": event.repo_root,
            "db_path": event.db_path,
            "debounce_ms": event.debounce_ms,
        }
    if isinstance(event, WatchStoppedEvent):
        return {
            "event": "watch_stopped",
            "repo_root": event.repo_root,
        }
    if isinstance(event, WatchFileProcessedEvent):
        return _format_watch_file_event(event)
    return event.model_dump(mode="json", exclude_none=True)


def format_files_for_cli(page: FileListPage) -> dict[str, Any]:
    """Format a ``FileListPage`` into the ``cbi files`` JSON output shape.

    Args:
        page: Enriched ``FileListPage`` from ``QueryEngine.list_files``.

    Returns:
        Dictionary matching the legacy ``cbi files`` JSON structure.
    """
    return {
        "repo_id": page.repo_id,
        "total_count": page.total_count,
        "returned_count": page.returned_count,
        "files": [
            {
                "id": item.id,
                "file_path": item.file_path,
                "module_path": item.module_path,
                "line_count": item.line_count,
                "symbol_count": item.symbol_count,
                "size_bytes": item.size_bytes,
                "is_test": item.is_test,
                "freshness": item.freshness,
                "is_fresh": item.is_fresh,
                "stale_reason": item.stale_reason,
            }
            for item in page.files
        ],
    }


def format_outline_for_cli(outline: FileOutline) -> dict[str, Any]:
    """Format a ``FileOutline`` into the ``cbi outline`` JSON output shape.

    Args:
        outline: ``FileOutline`` model from ``QueryEngine.get_file_outline``.

    Returns:
        Dictionary matching the legacy ``cbi outline`` JSON structure.
    """
    dumped = outline.model_dump(mode="json")
    if outline.is_fresh or outline.stale_reason is None:
        dumped.pop("stale_reason", None)
    return dumped


def format_symbol_search_for_cli(page: SymbolSearchPage) -> dict[str, Any]:
    """Format a ``SymbolSearchPage`` into the ``cbi symbols search`` JSON output shape."""
    return project_symbol_search(page, compact=False)


def format_symbol_list_for_cli(page: SymbolListPage) -> dict[str, Any]:
    """Format a ``SymbolListPage`` into the ``cbi symbols list`` JSON output shape."""
    return {
        "repo_id": page.repo_id,
        "total_count": page.total_count,
        "returned_count": page.returned_count,
        "offset": page.offset,
        "limit": page.limit,
        "has_more": page.has_more,
        "symbols": [project_symbol_summary(item, compact=False) for item in page.symbols],
    }


def format_source_for_cli(snippet: SymbolSnippet) -> dict[str, Any]:
    """Format a ``SymbolSnippet`` into the ``cbi source`` JSON output shape."""
    return project_symbol_source(snippet, drop_none=False)


def _format_cli_focus_symbol(focus: FocusSymbol) -> dict[str, Any]:
    """Serialize a ``FocusSymbol`` for ``cbi symbols show`` without internal UI fields.

    Args:
        focus: Focus or parent ``FocusSymbol`` model.

    Returns:
        Dictionary matching legacy ``SymbolContext.focus_symbol`` / parent shape.
    """
    data: dict[str, Any] = {
        "id": focus.id,
        "kind": str(focus.kind),
        "name": focus.name,
        "qualified_name": focus.qualified_name,
        "location": focus.location,
        "file_path": focus.file_path,
        "module_path": focus.module_path,
        "start_line": focus.start_line,
        "end_line": focus.end_line,
        "signature": focus.signature,
        "doc_summary": focus.doc_summary,
        "parent_id": focus.parent_id,
        "scope": str(focus.scope) if focus.scope is not None else None,
        "lexical_parent_id": focus.lexical_parent_id,
        "visibility_hint": focus.visibility_hint,
        "confidence": focus.confidence,
    }
    if focus.source is not None:
        data["source"] = focus.source
    if focus.source_truncated:
        data["source_truncated"] = True
    if focus.source_error is not None:
        data["source_error"] = focus.source_error
    return data


def _format_cli_context_edge(edge: ContextEdge) -> dict[str, Any]:
    """Serialize a ``ContextEdge`` for ``cbi symbols show`` JSON output.

    Args:
        edge: ``ContextEdge`` model.

    Returns:
        Dictionary matching legacy context edge fields.
    """
    return {
        "id": edge.id,
        "kind": str(edge.kind),
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "peer_name": edge.peer_name,
        "location": edge.location,
        "file_path": edge.file_path,
        "line": edge.line,
        "confidence": edge.confidence,
        "source": edge.source,
    }


def format_symbol_context_for_cli(ctx: SymbolContext) -> dict[str, Any]:
    """Format a ``SymbolContext`` into the ``cbi symbols show`` JSON output shape.

    Args:
        ctx: Immutable ``SymbolContext`` model.

    Returns:
        Dictionary matching the legacy ``cbi symbols show`` JSON structure.
    """
    return {
        "focus_symbol": _format_cli_focus_symbol(ctx.focus_symbol),
        "structural_parent": (
            _format_cli_focus_symbol(ctx.structural_parent)
            if ctx.structural_parent is not None
            else None
        ),
        "structural_children": [item.model_dump(mode="json") for item in ctx.structural_children],
        "lexical_parent": (
            _format_cli_focus_symbol(ctx.lexical_parent) if ctx.lexical_parent is not None else None
        ),
        "lexical_children": [item.model_dump(mode="json") for item in ctx.lexical_children],
        "incoming_edges": [_format_cli_context_edge(edge) for edge in ctx.incoming_edges],
        "outgoing_edges": [_format_cli_context_edge(edge) for edge in ctx.outgoing_edges],
        "file_siblings": [item.model_dump(mode="json") for item in ctx.file_siblings],
        "structural_summary": ctx.structural_summary.model_dump(mode="json"),
        "freshness": ctx.freshness.model_dump(mode="json"),
        "confidence": ctx.confidence.model_dump(mode="json"),
        "reference_summary": ctx.reference_summary.model_dump(mode="json"),
    }


def format_references_for_cli(report: SymbolReferencesReport) -> dict[str, Any]:
    """Format a ``SymbolReferencesReport`` into the ``cbi refs`` JSON output shape."""
    return project_symbol_references(report, compact=False)


def serialize_risk_for_cli(result: RiskAssessment) -> dict[str, Any]:
    """Serialize a ``RiskAssessment`` into the deterministic ``cbi risk`` JSON shape."""
    return project_risk_assessment(result)


def format_mcp_call_envelope(tool_name: str, raw_response: str) -> tuple[McpCallEnvelope, int]:
    """Measure MCP payload byte/token metrics and build ``McpCallEnvelope`` + exit code."""
    byte_size = len(raw_response.encode("utf-8"))
    est_tokens = max(1, (byte_size + 3) // 4) if byte_size > 0 else 0
    try:
        parsed_response: Any = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        logger.debug("Non-JSON MCP tool response from %s: %s", tool_name, exc)
        parsed_response = {"raw": raw_response}

    exit_code = 1 if isinstance(parsed_response, dict) and parsed_response.get("ok") is False else 0
    envelope = McpCallEnvelope(
        tool=tool_name,
        bytes=byte_size,
        estimated_tokens=est_tokens,
        result=parsed_response,
    )
    return envelope, exit_code


def format_code_search_for_cli(report: CodeSearchReport) -> dict[str, Any]:
    """Format a ``CodeSearchReport`` into the ``cbi search`` JSON output shape."""
    return project_code_search(report)


__all__ = [
    "emit_cli_error",
    "emit_json",
    "format_code_search_for_cli",
    "format_files_for_cli",
    "format_index_result_for_cli",
    "format_mcp_call_envelope",
    "format_outline_for_cli",
    "format_references_for_cli",
    "format_source_for_cli",
    "format_symbol_context_for_cli",
    "format_symbol_list_for_cli",
    "format_symbol_search_for_cli",
    "format_watch_event_payload",
    "resolve_cli_db_path",
    "resolve_scoped_engine",
    "serialize_risk_for_cli",
    "silence_broken_pipe",
]
