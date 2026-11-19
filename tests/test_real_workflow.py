"""End-to-end hermetic workflow tests across ``IndexerEngine`` and ``rc`` CLI in ``tmp_path``."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.models.config import AppConfig
from engine.models.queries import ContextQuery, OutlineQuery, SourceQuery
from engine.services.facade import IndexerEngine
from graph.models.risk import RiskDecision
from mcp_server.services.registry import McpToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "real_workflow"


def _run_rc(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m cli.main`` inside ``cwd`` with ``src/`` on ``PYTHONPATH``."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    return subprocess.run(
        [sys.executable, "-m", "cli.main", *args],
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
    """Full ``IndexerEngine`` pipeline: index -> outline -> source -> context -> risk -> MCP."""
    repo_root = copied_workflow_repo / "cross_file_refs"
    db_path = tmp_path / "workflow.db"

    with IndexerEngine(AppConfig(db_path=db_path)) as engine:
        idx_res = engine.commands.index_repository(repo_root)
        assert idx_res.link_report.total_resolved >= 2
        assert idx_res.graph_stats.file_count == 2
        assert idx_res.graph_stats.node_count >= 4

        helper = engine.queries.get_symbol("helper")
        helper_id = helper.id

        outline = engine.queries.get_file_outline(OutlineQuery(file_path="module_b.py"))
        assert [s.name for s in outline.symbols] == ["helper"]

        src = engine.queries.get_symbol_source(SourceQuery(symbol=helper_id))
        assert "def helper():" in src.numbered_source
        assert "def helper():" in src.raw_source
        assert src.is_fresh is True

        ctx = engine.queries.get_symbol_context(
            ContextQuery(symbol=helper_id, include_source=True)
        )
        assert ctx.structural_summary.relationship_counts.callers >= 1
        assert ctx.freshness.context_source == "ast_only"

        risk = engine.queries.analyze_risk([helper_id])
        assert risk.decision in (
            RiskDecision.SAFE_ENOUGH,
            RiskDecision.REVIEW_REQUIRED,
            RiskDecision.HIGH_RISK,
        )
        assert risk.facts.cross_module_impact is True
        assert risk.facts.target_count == 1

        registry = McpToolRegistry(engine)
        mcp_calls: list[tuple[str, dict[str, object]]] = [
            ("get_repo_overview", {}),
            ("list_files", {}),
            ("get_file_outline", {"file_path": "module_a.py"}),
            ("search_symbols", {"query": "helper"}),
            ("get_symbol_source", {"symbol": helper_id}),
            ("get_symbol_context", {"symbol": helper_id}),
            ("get_symbol_references", {"symbol": helper_id}),
            ("analyze_risk", {"symbols": [helper_id]}),
        ]
        for tool_name, kwargs in mcp_calls:
            raw = asyncio.run(registry.call_tool(tool_name, kwargs))
            assert json.loads(raw)["ok"] is True


def test_cli_end_to_end_incremental_edit_and_prune_workflow(
    copied_workflow_repo: Path,
    tmp_path: Path,
) -> None:
    """Full CLI lifecycle on copied fixtures: index -> edit -> stale -> reindex -> prune."""
    repo_root = copied_workflow_repo / "simple_module"
    db_path = tmp_path / "cli_workflow.db"

    idx1 = _run_rc("index", str(repo_root), "--db-path", str(db_path), cwd=repo_root)
    assert idx1.returncode == 0, idx1.stderr
    assert json.loads(idx1.stdout)["files_indexed"] == 1

    greeter = repo_root / "greeter.py"
    greeter.write_text(
        greeter.read_text(encoding="utf-8")
        + "\n\ndef farewell(name: str) -> str:\n    return f'Bye, {name}!'\n",
        encoding="utf-8",
    )

    files_modified = _run_rc("files", "--db-path", str(db_path), cwd=repo_root)
    assert files_modified.returncode == 0
    assert json.loads(files_modified.stdout)["files"][0]["freshness"] == "modified"

    idx2 = _run_rc("index", str(repo_root), "--db-path", str(db_path), cwd=repo_root)
    assert idx2.returncode == 0
    assert json.loads(idx2.stdout)["files_indexed"] == 1

    search_res = _run_rc(
        "symbols",
        "search",
        "farewell",
        "--db-path",
        str(db_path),
        cwd=repo_root,
    )
    assert search_res.returncode == 0
    syms = json.loads(search_res.stdout)["symbols"]
    assert len(syms) == 1
    farewell_id = syms[0]["id"]

    inspect_invocations = (
        ("outline", "greeter.py"),
        ("symbols", "show", farewell_id),
        ("source", farewell_id),
        ("refs", farewell_id),
        ("risk", farewell_id),
        ("db", "status"),
        ("db", "sql", "SELECT COUNT(*) AS n FROM nodes"),
    )
    for inspect_args in inspect_invocations:
        res = _run_rc(*inspect_args, "--db-path", str(db_path), cwd=repo_root)
        assert res.returncode == 0, res.stderr
        assert isinstance(json.loads(res.stdout), dict)

    greeter.unlink()
    files_deleted = _run_rc("files", "--db-path", str(db_path), cwd=repo_root)
    assert json.loads(files_deleted.stdout)["files"][0]["freshness"] == "deleted"

    idx3 = _run_rc("index", str(repo_root), "--db-path", str(db_path), cwd=repo_root)
    assert idx3.returncode == 0
    assert json.loads(idx3.stdout)["files_pruned"] == 1
