"""End-to-end hermetic workflow tests across Python APIs and ``rc`` CLI in ``tmp_path``."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from repo_context.config import AppConfig
from repo_context.context import build_file_outline, build_symbol_context, read_symbol_source
from repo_context.graph import (
    DECISION_HIGH_RISK,
    DECISION_REVIEW_REQUIRED,
    DECISION_SAFE_ENOUGH,
    analyze_symbol_risk,
    analyze_target_set_risk,
    get_repo_graph_stats,
)
from repo_context.mcp.tools import (
    analyze_risk,
    get_file_outline,
    get_repo_overview,
    get_symbol_context,
    get_symbol_references,
    get_symbol_source,
    list_files,
    search_symbols,
)
from repo_context.parsing.linker import resolve_repo_edges
from repo_context.parsing.pipeline import extract_file_graph
from repo_context.parsing.scanner import scan_repository
from repo_context.storage import (
    close_connection,
    get_connection,
    initialize_database,
    nodes_table,
    replace_file_graph,
    upsert_files,
    upsert_repo,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "real_workflow"


def _run_rc(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    return subprocess.run(
        [sys.executable, "-m", "repo_context.cli.main", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )


@pytest.fixture
def copied_workflow_repo(tmp_path: Path) -> Path:
    """Copy ``tests/fixtures/real_workflow`` into ``tmp_path`` so fixtures are never mutated."""
    dest = tmp_path / "real_workflow_copy"
    shutil.copytree(FIXTURES_DIR, dest)
    return dest


def test_python_api_end_to_end_workflow(copied_workflow_repo: Path, tmp_path: Path) -> None:
    """Full Python pipeline: scan -> extract -> link -> outline -> source -> context -> MCP."""
    repo_root = copied_workflow_repo / "cross_file_refs"
    db_path = tmp_path / "workflow.db"
    cfg = AppConfig()

    conn = get_connection(db_path)
    try:
        initialize_database(conn)
        repo, files = scan_repository(repo_root, cfg)
        upsert_repo(conn, repo)
        upsert_files(conn, files)
        for rec in files:
            nodes, edges, _ = extract_file_graph(repo.id, rec, repo_root, config=cfg)
            replace_file_graph(conn, rec.id, nodes, edges)
        link_stats = resolve_repo_edges(conn, repo.id)
        conn.commit()

        assert link_stats["total_resolved"] >= 2
        stats = get_repo_graph_stats(conn, repo.id)
        assert stats["file_count"] == 2
        assert stats["node_count"] >= 4

        helper_id = conn.execute(
            select(nodes_table.c.id).where(
                nodes_table.c.repo_id == repo.id,
                nodes_table.c.name == "helper",
                nodes_table.c.kind == "function",
            )
        ).scalar_one_or_none()
        assert helper_id is not None

        outline = build_file_outline(conn, repo.id, "module_b.py")
        assert [s["name"] for s in outline["symbols"]] == ["helper"]

        src = read_symbol_source(conn, helper_id)
        assert "def helper():" in src["source"]
        assert src["is_fresh"] is True

        ctx = build_symbol_context(conn, helper_id, include_source=True)
        assert ctx is not None
        assert ctx.structural_summary["relationship_counts"]["callers"] >= 1
        assert ctx.freshness["context_source"] == "ast_only"

        risk = analyze_symbol_risk(conn, helper_id)
        assert risk.decision in (DECISION_SAFE_ENOUGH, DECISION_REVIEW_REQUIRED, DECISION_HIGH_RISK)
        assert risk.facts.cross_module_impact is True

        multi = analyze_target_set_risk(conn, [helper_id])
        assert multi.facts.target_count == 1
    finally:
        close_connection(conn)

    # Exercise all 8 MCP tools against the same hermetic temp DB
    assert json.loads(asyncio.run(get_repo_overview(db_path=db_path)))["ok"] is True
    assert json.loads(asyncio.run(list_files(db_path=db_path)))["ok"] is True
    assert json.loads(asyncio.run(get_file_outline("module_a.py", db_path=db_path)))["ok"] is True
    assert json.loads(asyncio.run(search_symbols("helper", db_path=db_path)))["ok"] is True
    assert json.loads(asyncio.run(get_symbol_source(helper_id, db_path=db_path)))["ok"] is True
    assert json.loads(asyncio.run(get_symbol_context(helper_id, db_path=db_path)))["ok"] is True
    assert json.loads(asyncio.run(get_symbol_references(helper_id, db_path=db_path)))["ok"] is True
    assert json.loads(asyncio.run(analyze_risk([helper_id], db_path=db_path)))["ok"] is True


def test_cli_end_to_end_incremental_edit_and_prune_workflow(
    copied_workflow_repo: Path,
    tmp_path: Path,
) -> None:
    """Full CLI lifecycle on copied fixtures: index -> edit -> stale -> reindex -> prune."""
    repo_root = copied_workflow_repo / "simple_module"
    db_path = tmp_path / "cli_workflow.db"

    # 1. Initial index
    idx1 = _run_rc("index", str(repo_root), "--db-path", str(db_path), cwd=repo_root)
    assert idx1.returncode == 0, idx1.stderr
    assert json.loads(idx1.stdout)["files_indexed"] == 1

    # 2. Edit copied file in tmp_path (never touches tests/fixtures/)
    greeter = repo_root / "greeter.py"
    greeter.write_text(
        greeter.read_text(encoding="utf-8")
        + "\n\ndef farewell(name: str) -> str:\n    return f'Bye, {name}!'\n",
        encoding="utf-8",
    )

    files_modified = _run_rc("files", "--db-path", str(db_path), cwd=repo_root)
    assert files_modified.returncode == 0
    assert json.loads(files_modified.stdout)["files"][0]["freshness"] == "modified"

    # 3. Incremental re-index picks up farewell()
    idx2 = _run_rc("index", str(repo_root), "--db-path", str(db_path), cwd=repo_root)
    assert idx2.returncode == 0
    assert json.loads(idx2.stdout)["files_indexed"] == 1

    search_res = _run_rc(
        "symbols", "search", "farewell", "--db-path", str(db_path), cwd=repo_root
    )
    assert search_res.returncode == 0
    syms = json.loads(search_res.stdout)["symbols"]
    assert len(syms) == 1
    farewell_id = syms[0]["id"]

    # 4. Inspect via CLI & mcp-call (all output JSON objects by default)
    for inspect_args in (
        ("outline", "greeter.py"),
        ("symbols", "show", farewell_id),
        ("source", farewell_id),
        ("refs", farewell_id),
        ("risk", farewell_id),
        ("db", "status"),
        ("db", "sql", "SELECT COUNT(*) AS n FROM nodes"),
    ):
        res = _run_rc(*inspect_args, "--db-path", str(db_path), cwd=repo_root)
        assert res.returncode == 0, res.stderr
        assert isinstance(json.loads(res.stdout), dict)

    # 5. Delete copied file -> rc files shows 'deleted' -> rc index prunes it
    greeter.unlink()
    files_deleted = _run_rc("files", "--db-path", str(db_path), cwd=repo_root)
    assert json.loads(files_deleted.stdout)["files"][0]["freshness"] == "deleted"

    idx3 = _run_rc("index", str(repo_root), "--db-path", str(db_path), cwd=repo_root)
    assert idx3.returncode == 0
    assert json.loads(idx3.stdout)["files_pruned"] == 1
