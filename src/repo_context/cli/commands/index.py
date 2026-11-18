"""Handler for `rc index`."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Connection

from repo_context.cli.utils import resolve_db_path
from repo_context.config import AppConfig, get_config
from repo_context.exceptions import LspError, ParseError, PathSecurityError
from repo_context.graph import get_repo_graph_stats
from repo_context.logging_config import get_logger
from repo_context.lsp import PyrightLspClient
from repo_context.lsp.references import enrich_references_for_symbol
from repo_context.models.file import FileRecord
from repo_context.models.repo import RepoRecord
from repo_context.parsing.linker import resolve_repo_edges
from repo_context.parsing.pathing import normalize_repo_root
from repo_context.parsing.pipeline import extract_file_graph
from repo_context.parsing.scanner import scan_repository
from repo_context.storage import (
    connection_scope,
    get_repo_by_id,
    initialize_database,
    list_files_for_repo,
    list_nodes_for_repo,
    prune_stale_files,
    record_index_run_finish,
    record_index_run_start,
    replace_file_graph,
    upsert_files,
    upsert_repo,
)

logger = get_logger("cli.index")


@dataclass
class _ExtractionBatch:
    """AST extraction outputs and counters for changed files in an indexing run."""

    attempted_count: int = 0
    extracted: list[tuple[FileRecord, list[dict[str, Any]], list[dict[str, Any]]]] | None = None
    failures: list[dict[str, str]] | None = None
    nodes_extracted: int = 0
    edges_extracted: int = 0


@dataclass(frozen=True)
class _IndexRunOutcome:
    """Completed indexing run counters, linker summary, and failure records."""

    run_id: str
    repo: RepoRecord
    scanned_count: int
    indexed_count: int
    unchanged_count: int
    pruned_count: int
    failures: list[dict[str, str]]
    nodes_extracted: int
    edges_extracted: int
    link_stats: dict[str, Any]


def _enrich_repo_symbols(
    conn: Connection,
    client: PyrightLspClient,
    symbols: list[dict[str, Any]],
    files_by_id: dict[str, FileRecord],
    repo_root: str,
    summary: dict[str, Any],
) -> None:
    """Enrich LSP references for each non-module symbol using an active client."""
    for sym in symbols:
        file_rec = files_by_id.get(sym["file_id"])
        if file_rec is None:
            summary["errors"] += 1
            continue
        target_symbol = {
            "id": sym["id"],
            "repo_id": sym["repo_id"],
            "file_id": sym["file_id"],
            "file_path": file_rec.file_path,
            "uri": sym["uri"],
            "qualified_name": sym["qualified_name"],
            "kind": sym["kind"],
            "scope": sym.get("scope"),
            "range_json": sym.get("range_json"),
            "selection_range_json": sym.get("selection_range_json"),
            "repo_root": repo_root,
        }
        try:
            stats = enrich_references_for_symbol(
                conn,
                client,
                target_symbol,
                open_all_files=False,
            )
            summary["symbols_refreshed"] += 1
            summary["total_references"] += int(stats.get("reference_count", 0))
        except (LspError, PathSecurityError, ValueError, OSError) as exc:
            summary["errors"] += 1
            logger.debug("LSP enrichment skipped for %s: %s", sym.get("id"), exc)


def _run_lsp_enrichment(
    conn: Connection,
    repo: RepoRecord,
    cfg: AppConfig,
) -> dict[str, Any]:
    """Enrich LSP references for non-module symbols in ``repo``."""
    files_by_id = {
        rec.id: rec
        for rec in list_files_for_repo(conn, repo.id, include_tests=True)
    }
    symbols = [
        node
        for node in list_nodes_for_repo(conn, repo.id)
        if node.get("kind") != "module"
    ]

    summary: dict[str, Any] = {
        "available": False,
        "symbols_attempted": len(symbols),
        "symbols_refreshed": 0,
        "errors": 0,
        "total_references": 0,
        "error": None,
    }
    if not symbols:
        summary["available"] = True
        return summary

    try:
        with PyrightLspClient(config=cfg) as client:
            client.start(repo.root_path)
            summary["available"] = True
            _enrich_repo_symbols(conn, client, symbols, files_by_id, repo.root_path, summary)
    except LspError as exc:
        summary["available"] = False
        summary["error"] = str(exc)
        logger.warning("LSP enrichment unavailable: %s", exc)

    return summary


def _prepare_repo_record(conn: Connection, repo: RepoRecord) -> RepoRecord:
    """Preserve ``created_at`` if ``repo`` already exists and persist it."""
    existing_repo = get_repo_by_id(conn, repo.id)
    if existing_repo is not None:
        repo = RepoRecord(
            id=repo.id,
            root_path=repo.root_path,
            name=repo.name,
            default_language=repo.default_language,
            created_at=existing_repo.created_at,
            last_indexed_at=repo.last_indexed_at,
        )
    upsert_repo(conn, repo)
    conn.commit()
    return repo


def _select_files_to_extract(
    conn: Connection,
    repo_id: str,
    scanned_files: list[FileRecord],
    *,
    force: bool,
) -> list[FileRecord]:
    """Select all scanned files when ``force`` is True, else only new or modified files."""
    if force:
        return scanned_files
    existing_files = {
        rec.id: rec
        for rec in list_files_for_repo(conn, repo_id, include_tests=True)
    }
    return [
        rec
        for rec in scanned_files
        if rec.id not in existing_files
        or existing_files[rec.id].content_hash != rec.content_hash
    ]


def _extract_changed_files(
    repo_id: str,
    repo_root: Path,
    to_extract: list[FileRecord],
    cfg: AppConfig,
) -> _ExtractionBatch:
    """Extract AST symbol/edge graphs for ``to_extract``, recording per-file parse errors."""
    extracted: list[tuple[FileRecord, list[dict[str, Any]], list[dict[str, Any]]]] = []
    failures: list[dict[str, str]] = []
    nodes_extracted = 0
    edges_extracted = 0

    for file_record in to_extract:
        try:
            nodes, edges, _summary = extract_file_graph(
                repo_id,
                file_record,
                repo_root,
                config=cfg,
            )
            extracted.append((file_record, nodes, edges))
            nodes_extracted += len(nodes)
            edges_extracted += len(edges)
        except (ParseError, PathSecurityError, OSError) as exc:
            failures.append({"file_path": file_record.file_path, "error": str(exc)})
            logger.warning("Failed extracting %s: %s", file_record.file_path, exc)

    return _ExtractionBatch(
        attempted_count=len(to_extract),
        extracted=extracted,
        failures=failures,
        nodes_extracted=nodes_extracted,
        edges_extracted=edges_extracted,
    )


def _persist_extracted_graph(
    conn: Connection,
    repo: RepoRecord,
    scanned_files: list[FileRecord],
    extracted: list[tuple[FileRecord, list[dict[str, Any]], list[dict[str, Any]]]],
) -> tuple[int, dict[str, Any]]:
    """Persist file records, per-file graph replacements, pruning, and linking atomically."""
    try:
        upsert_files(conn, [rec for rec, _, _ in extracted])
        for rec, nodes, edges in extracted:
            replace_file_graph(conn, rec.id, nodes, edges)

        keep_file_ids = {rec.id for rec in scanned_files}
        pruned_count = prune_stale_files(conn, repo.id, keep_file_ids)
        link_stats = resolve_repo_edges(conn, repo.id)
        upsert_repo(conn, repo)
        conn.commit()
        return pruned_count, link_stats
    except Exception:
        if conn.in_transaction():
            conn.rollback()
        raise


def _execute_index_run(
    conn: Connection,
    repo: RepoRecord,
    repo_root: Path,
    scanned_files: list[FileRecord],
    cfg: AppConfig,
    *,
    force: bool,
) -> _IndexRunOutcome:
    """Run an audited indexing cycle and record completion or failure in ``index_runs``."""
    run_id = record_index_run_start(
        conn,
        repo.id,
        run_type="full" if force else "incremental",
    )
    conn.commit()

    batch = _ExtractionBatch()
    try:
        batch = _extract_changed_files(
            repo.id,
            repo_root,
            _select_files_to_extract(conn, repo.id, scanned_files, force=force),
            cfg,
        )
        extracted = batch.extracted or []
        failures = batch.failures or []
        pruned_count, link_stats = _persist_extracted_graph(conn, repo, scanned_files, extracted)
        record_index_run_finish(
            conn,
            run_id,
            "completed",
            files_scanned=len(scanned_files),
            nodes_extracted=batch.nodes_extracted,
            edges_extracted=batch.edges_extracted,
            error_message=(
                f"{len(failures)} file(s) failed AST extraction" if failures else None
            ),
        )
        conn.commit()
    except Exception as exc:
        if conn.in_transaction():
            conn.rollback()
        record_index_run_finish(
            conn,
            run_id,
            "failed",
            files_scanned=len(scanned_files),
            nodes_extracted=batch.nodes_extracted,
            edges_extracted=batch.edges_extracted,
            error_message=str(exc),
        )
        conn.commit()
        raise

    return _IndexRunOutcome(
        run_id=run_id,
        repo=repo,
        scanned_count=len(scanned_files),
        indexed_count=len(extracted),
        unchanged_count=len(scanned_files) - batch.attempted_count,
        pruned_count=pruned_count,
        failures=failures,
        nodes_extracted=batch.nodes_extracted,
        edges_extracted=batch.edges_extracted,
        link_stats=link_stats,
    )


def _build_index_payload(
    db_path: Path,
    outcome: _IndexRunOutcome,
    repo_totals: dict[str, Any],
    lsp_summary: dict[str, Any] | None,
    *,
    include_tests: bool,
    force: bool,
) -> dict[str, Any]:
    """Assemble the CLI JSON summary for an indexing run."""
    payload: dict[str, Any] = {
        "db_path": str(db_path),
        "run_id": outcome.run_id,
        "repo_id": outcome.repo.id,
        "repo_name": outcome.repo.name,
        "root_path": outcome.repo.root_path,
        "include_tests": include_tests,
        "force": force,
        "files_scanned": outcome.scanned_count,
        "files_indexed": outcome.indexed_count,
        "files_unchanged": outcome.unchanged_count,
        "files_pruned": outcome.pruned_count,
        "files_failed": len(outcome.failures),
        "nodes_extracted": outcome.nodes_extracted,
        "edges_extracted": outcome.edges_extracted,
        "linker": outcome.link_stats,
        "totals": repo_totals,
    }
    if outcome.failures:
        payload["failures"] = outcome.failures
    if lsp_summary is not None:
        payload["lsp"] = lsp_summary
    return payload


def cmd_index(args: argparse.Namespace) -> int:
    """Scan, extract, link, and optionally LSP-enrich a repository."""
    db_path = resolve_db_path(args)
    include_tests = bool(getattr(args, "include_tests", False))
    force = bool(getattr(args, "force", False))
    cfg = get_config(db_path=db_path, include_tests=include_tests)
    repo_root = normalize_repo_root(getattr(args, "repo_path", None) or Path.cwd())
    repo, scanned_files = scan_repository(repo_root, cfg)

    with connection_scope(db_path, read_only=False) as conn:
        initialize_database(conn)
        repo = _prepare_repo_record(conn, repo)
        outcome = _execute_index_run(conn, repo, repo_root, scanned_files, cfg, force=force)
        lsp_summary = (
            _run_lsp_enrichment(conn, repo, cfg) if bool(getattr(args, "lsp", False)) else None
        )
        repo_totals = get_repo_graph_stats(conn, repo.id)

    payload = _build_index_payload(
        db_path,
        outcome,
        repo_totals,
        lsp_summary,
        include_tests=include_tests,
        force=force,
    )
    print(json.dumps(payload, indent=2))
    return 0
