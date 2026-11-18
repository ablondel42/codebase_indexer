"""Handler for `rc index`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from repo_context.config import get_config
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
from repo_context.cli.utils import resolve_db_path

logger = get_logger("cli.index")


def _run_lsp_enrichment(
    conn: Any,
    repo: RepoRecord,
    cfg: Any,
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
                    "repo_root": repo.root_path,
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
                    logger.debug(
                        "LSP enrichment skipped for %s: %s",
                        sym.get("id"),
                        exc,
                    )
    except LspError as exc:
        summary["available"] = False
        summary["error"] = str(exc)
        logger.warning("LSP enrichment unavailable: %s", exc)

    return summary


def cmd_index(args: argparse.Namespace) -> int:
    """Scan, extract, link, and optionally LSP-enrich a repository."""
    db_path = resolve_db_path(args)
    include_tests = bool(getattr(args, "include_tests", False))
    run_lsp = bool(getattr(args, "lsp", False))
    force = bool(getattr(args, "force", False))
    repo_arg: Path = getattr(args, "repo_path", None) or Path.cwd()

    cfg = get_config(db_path=db_path, include_tests=include_tests)
    repo_root = normalize_repo_root(repo_arg)
    repo, scanned_files = scan_repository(repo_root, cfg)

    failures: list[dict[str, str]] = []
    extracted: list[tuple[FileRecord, list[dict], list[dict]]] = []
    nodes_extracted = 0
    edges_extracted = 0
    unchanged_count = 0
    pruned_count = 0
    link_stats: dict[str, Any] = {}
    lsp_summary: dict[str, Any] | None = None

    with connection_scope(db_path, read_only=False) as conn:
        initialize_database(conn)

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

        run_id = record_index_run_start(
            conn,
            repo.id,
            run_type="full" if force else "incremental",
        )
        conn.commit()

        try:
            existing_files = {
                rec.id: rec
                for rec in list_files_for_repo(conn, repo.id, include_tests=True)
            }

            if force:
                to_extract = scanned_files
            else:
                to_extract = [
                    rec
                    for rec in scanned_files
                    if rec.id not in existing_files
                    or existing_files[rec.id].content_hash != rec.content_hash
                ]
            unchanged_count = len(scanned_files) - len(to_extract)

            for file_record in to_extract:
                try:
                    nodes, edges, _summary = extract_file_graph(
                        repo.id,
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

            # Persist file records, per-file graph replacements, pruning, and linking atomically.
            conn.execute("BEGIN")
            try:
                upsert_files(conn, [rec for rec, _, _ in extracted])
                for rec, nodes, edges in extracted:
                    replace_file_graph(conn, rec.id, nodes, edges)

                keep_file_ids = {rec.id for rec in scanned_files}
                pruned_count = prune_stale_files(conn, repo.id, keep_file_ids)
                link_stats = resolve_repo_edges(conn, repo.id)
                upsert_repo(conn, repo)
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise

            error_summary = (
                f"{len(failures)} file(s) failed AST extraction" if failures else None
            )
            record_index_run_finish(
                conn,
                run_id,
                "completed",
                files_scanned=len(scanned_files),
                nodes_extracted=nodes_extracted,
                edges_extracted=edges_extracted,
                error_message=error_summary,
            )
            conn.commit()
        except Exception as exc:
            if conn.in_transaction:
                conn.rollback()
            record_index_run_finish(
                conn,
                run_id,
                "failed",
                files_scanned=len(scanned_files),
                nodes_extracted=nodes_extracted,
                edges_extracted=edges_extracted,
                error_message=str(exc),
            )
            conn.commit()
            raise

        if run_lsp:
            lsp_summary = _run_lsp_enrichment(conn, repo, cfg)

        repo_totals = get_repo_graph_stats(conn, repo.id)

    payload: dict[str, Any] = {
        "db_path": str(db_path),
        "run_id": run_id,
        "repo_id": repo.id,
        "repo_name": repo.name,
        "root_path": repo.root_path,
        "include_tests": include_tests,
        "force": force,
        "files_scanned": len(scanned_files),
        "files_indexed": len(extracted),
        "files_unchanged": unchanged_count,
        "files_pruned": pruned_count,
        "files_failed": len(failures),
        "nodes_extracted": nodes_extracted,
        "edges_extracted": edges_extracted,
        "linker": link_stats,
        "totals": repo_totals,
    }
    if failures:
        payload["failures"] = failures
    if lsp_summary is not None:
        payload["lsp"] = lsp_summary

    print(json.dumps(payload, indent=2))
    return 0
