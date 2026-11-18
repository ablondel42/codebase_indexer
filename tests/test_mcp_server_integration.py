"""Hermetic MCP server registration and CLI ``rc mcp-call`` integration tests."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from repo_context.config import AppConfig
from repo_context.mcp.errors import (
    ERROR_AMBIGUOUS_SYMBOL,
    ERROR_DATABASE_NOT_FOUND,
    ERROR_SYMBOL_NOT_FOUND,
)
from repo_context.mcp.server import create_server
from repo_context.mcp.tools import register_tools
from repo_context.parsing.linker import resolve_repo_edges
from repo_context.parsing.pipeline import extract_file_graph
from repo_context.parsing.scanner import scan_repository
from repo_context.storage import (
    close_connection,
    get_connection,
    initialize_database,
    replace_file_graph,
    upsert_files,
    upsert_repo,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    return env


class _FakeFastMCP:
    """Lightweight FastMCP stub capturing tool registrations for integration tests."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str | None = None):
        def decorator(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture
def indexed_integration_db(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create and index a sample repository in ``tmp_path``."""
    repo_root = tmp_path / "integration_repo"
    repo_root.mkdir()
    (repo_root / "alpha.py").write_text(
        '"""Alpha module."""\n\n'
        "def shared() -> int:\n"
        '    """Alpha shared."""\n'
        "    return 1\n\n"
        "def compute(x: int) -> int:\n"
        '    """Compute x."""\n'
        "    return shared() + x\n",
        encoding="utf-8",
    )
    (repo_root / "beta.py").write_text(
        '"""Beta module."""\n'
        "from alpha import compute\n\n"
        "def shared() -> int:\n"
        '    """Beta shared."""\n'
        "    return 2\n\n"
        "def run() -> int:\n"
        "    return compute(5)\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "integration.db"
    conn = get_connection(db_path)
    try:
        initialize_database(conn)
        cfg = AppConfig()
        repo, files = scan_repository(repo_root, cfg)
        upsert_repo(conn, repo)
        upsert_files(conn, files)
        for rec in files:
            nodes, edges, _ = extract_file_graph(repo.id, rec, repo_root, config=cfg)
            replace_file_graph(conn, rec.id, nodes, edges)
        resolve_repo_edges(conn, repo.id)
        conn.commit()
    finally:
        close_connection(conn)

    return db_path, repo_root, repo.id


def test_registered_tools_integration_flow(indexed_integration_db: tuple[Path, Path, str]) -> None:
    """Registered MCP tools work end-to-end via real MCP server (if installed) and stub."""
    db_path, _, repo_id = indexed_integration_db
    if importlib.util.find_spec("mcp") is not None:
        mcp_obj = create_server(db_path=db_path)
        if hasattr(mcp_obj, "list_tools"):
            listed = asyncio.run(mcp_obj.list_tools())
            assert {t.name for t in listed} == {
                "get_repo_overview",
                "list_files",
                "get_file_outline",
                "search_symbols",
                "get_symbol_source",
                "get_symbol_context",
                "get_symbol_references",
                "analyze_risk",
            }
        if hasattr(mcp_obj, "call_tool"):
            call_res = asyncio.run(mcp_obj.call_tool("get_repo_overview", {}))
            first_text = call_res.content[0].text
            assert json.loads(first_text)["data"]["repo"]["id"] == repo_id

    fake = _FakeFastMCP()
    register_tools(fake, db_path=db_path)
    tools = fake.tools

    ov = json.loads(asyncio.run(tools["get_repo_overview"]()))  # type: ignore[operator]
    assert ov["ok"] is True
    assert ov["data"]["repo"]["id"] == repo_id

    files_res = json.loads(asyncio.run(tools["list_files"]()))  # type: ignore[operator]
    assert [f["file_path"] for f in files_res["data"]["files"]] == ["alpha.py", "beta.py"]

    outline_res = json.loads(asyncio.run(tools["get_file_outline"]("alpha.py")))  # type: ignore[operator]
    assert [s["name"] for s in outline_res["data"]["symbols"]] == ["shared", "compute"]

    search_res = json.loads(asyncio.run(tools["search_symbols"]("compute")))  # type: ignore[operator]
    assert search_res["data"]["count"] >= 1

    src_res = json.loads(asyncio.run(tools["get_symbol_source"]("alpha.compute")))  # type: ignore[operator]
    assert "def compute(" in src_res["data"]["source"]

    ctx_res = json.loads(asyncio.run(tools["get_symbol_context"]("alpha.compute")))  # type: ignore[operator]
    assert ctx_res["data"]["context"]["callers"]["total"] >= 1

    refs_res = json.loads(asyncio.run(tools["get_symbol_references"]("alpha.compute")))  # type: ignore[operator]
    assert refs_res["data"]["total_count"] >= 1

    risk_res = json.loads(asyncio.run(tools["analyze_risk"](["alpha.compute"])))  # type: ignore[operator]
    assert risk_res["data"]["risk"]["facts"]["cross_module_impact"] is True

    ambig = json.loads(asyncio.run(tools["get_symbol_context"]("shared")))  # type: ignore[operator]
    assert ambig["error"]["code"] == ERROR_AMBIGUOUS_SYMBOL

    missing = json.loads(asyncio.run(tools["get_symbol_source"]("does_not_exist")))  # type: ignore[operator]
    assert missing["error"]["code"] == ERROR_SYMBOL_NOT_FOUND


def test_cli_mcp_call_all_8_tools_and_missing_db_safety(
    indexed_integration_db: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    """`rc mcp-call` executes all 8 MCP tools and missing --db-path creates zero files."""
    db_path, _, _ = indexed_integration_db
    env = _subprocess_env()

    calls = [
        ("get_repo_overview", "{}"),
        ("list_files", "{}"),
        ("get_file_outline", '{"file_path": "alpha.py"}'),
        ("search_symbols", '{"query": "compute"}'),
        ("get_symbol_source", '{"symbol": "alpha.compute"}'),
        ("get_symbol_context", '{"symbol": "alpha.compute"}'),
        ("get_symbol_references", '{"symbol": "alpha.compute"}'),
        ("analyze_risk", '{"symbols": ["alpha.compute"]}'),
    ]
    for tool_name, json_args in calls:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "repo_context.cli.main",
                "mcp-call",
                tool_name,
                json_args,
                "--db-path",
                str(db_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env=env,
        )
        assert proc.returncode == 0, f"{tool_name} failed: {proc.stderr}"
        payload = json.loads(proc.stdout)
        assert payload["tool"] == tool_name
        assert payload["bytes"] > 0
        assert payload["estimated_tokens"] > 0
        assert payload["result"]["ok"] is True

    missing_db = tmp_path / "missing_mcp.db"
    err_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "repo_context.cli.main",
            "mcp-call",
            "get_repo_overview",
            "--db-path",
            str(missing_db),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert err_proc.returncode == 1
    err_payload = json.loads(err_proc.stdout)
    assert err_payload["result"]["error"]["code"] == ERROR_DATABASE_NOT_FOUND
    assert not missing_db.exists()


def test_cli_mcp_call_shorthand_and_key_value_args(
    indexed_integration_db: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    """`rc mcp-call` accepts shorthand symbol/file/query args, key=value pairs, and unquoted JSON."""
    db_path, _, _ = indexed_integration_db
    env = _subprocess_env()

    shorthand_cases = [
        (["get_symbol_context", "alpha.compute"], "alpha.compute"),
        (["get_symbol_context", "alpha.compute", "include_source=true"], "return shared() + x"),
        (["get_file_outline", "alpha.py", "max_depth=2"], "alpha.py"),
        (["search_symbols", "compute", "limit=5"], "alpha.compute"),
        (["get_symbol_source", "alpha.compute", "max_lines=2"], "def compute"),
        (["get_symbol_references", "alpha.compute"], "beta.run"),
        (["analyze_risk", "alpha.compute", "beta.run"], "cross_module_impact"),
        (["get_symbol_context", '{"symbol":', '"alpha.compute"}'], "alpha.compute"),
    ]
    for extra_args, expected_snippet in shorthand_cases:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "repo_context.cli.main",
                "mcp-call",
                *extra_args,
                "--db-path",
                str(db_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env=env,
        )
        assert proc.returncode == 0, f"{extra_args} failed: {proc.stderr}"
        payload = json.loads(proc.stdout)
        assert payload["result"]["ok"] is True
        assert expected_snippet in json.dumps(payload["result"])

    bad_json = subprocess.run(
        [
            sys.executable,
            "-m",
            "repo_context.cli.main",
            "mcp-call",
            "get_symbol_context",
            "{bad_json",
            "--db-path",
            str(db_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert bad_json.returncode == 1
    bad_err = json.loads(bad_json.stderr)
    assert bad_err["ok"] is False
    assert bad_err["error"]["code"] == "validation_error"
    assert "Invalid JSON arguments" in bad_err["error"]["message"]
