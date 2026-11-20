"""Hermetic MCP server registration, ``stdio`` handshake, and ``cbi mcp-call`` tests."""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from core.models.config import AppConfig
from engine.services.facade import IndexerEngine
from mcp_server.models.errors import McpErrorCode
from mcp_server.services.registry import McpToolRegistry
from mcp_server.services.server import McpServerRunner

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"


def _subprocess_env() -> dict[str, str]:
    """Build subprocess environment with ``src/`` prioritized on ``PYTHONPATH``."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    return env


class _FakeMcpServer:
    """Lightweight ``.tool()`` stub capturing registered MCP tool functions."""

    def __init__(self) -> None:
        """Initialize an empty tool registry map."""
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(
        self,
        name: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Bind ``fn`` into ``self.tools``."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture
def indexed_integration_db(tmp_path: Path) -> Iterator[tuple[Path, Path, str, IndexerEngine]]:
    """Create and index a two-module sample repository inside ``tmp_path``."""
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
    engine = IndexerEngine(AppConfig(db_path=db_path))
    try:
        res = engine.commands.index_repository(repo_root)
        yield db_path, repo_root, res.repo.id, engine
    finally:
        engine.close()


def test_real_mcp_server_list_and_call_tool(
    indexed_integration_db: tuple[Path, Path, str, IndexerEngine],
) -> None:
    """``McpServerRunner.create_server`` exposes all 8 tools on a real ``MCPServer``."""
    db_path, _, repo_id, engine = indexed_integration_db
    runner = McpServerRunner(engine.config, engine=engine)
    mcp_obj = runner.create_server(db_path=db_path)

    listed = asyncio.run(mcp_obj.list_tools())
    assert {tool.name for tool in listed} == set(McpToolRegistry.TOOL_NAMES)

    call_res = asyncio.run(mcp_obj.call_tool("get_repo_overview", {}))
    assert isinstance(call_res, CallToolResult)
    first_block = call_res.content[0]
    assert isinstance(first_block, TextContent)
    assert json.loads(first_block.text)["data"]["repo"]["id"] == repo_id


def test_bound_tools_stub_flow(
    indexed_integration_db: tuple[Path, Path, str, IndexerEngine],
) -> None:
    """``McpToolRegistry.bind`` wires all 8 tools and structured error envelopes cleanly."""
    db_path, _, repo_id, engine = indexed_integration_db
    fake = _FakeMcpServer()
    McpToolRegistry(engine).bind(fake, db_path=db_path)
    tools = fake.tools

    ov = json.loads(asyncio.run(tools["get_repo_overview"]()))
    assert ov["ok"] is True
    assert ov["data"]["repo"]["id"] == repo_id

    files_res = json.loads(asyncio.run(tools["list_files"]()))
    assert [f["file_path"] for f in files_res["data"]["files"]] == ["alpha.py", "beta.py"]

    outline_res = json.loads(asyncio.run(tools["get_file_outline"]("alpha.py")))
    assert [s["name"] for s in outline_res["data"]["symbols"]] == ["shared", "compute"]

    search_res = json.loads(asyncio.run(tools["search_symbols"]("compute")))
    assert search_res["data"]["count"] >= 1

    src_res = json.loads(asyncio.run(tools["get_symbol_source"]("alpha.compute")))
    assert "def compute(" in src_res["data"]["source"]

    ctx_res = json.loads(asyncio.run(tools["get_symbol_context"]("alpha.compute")))
    assert ctx_res["data"]["context"]["callers"]["total"] >= 1

    refs_res = json.loads(asyncio.run(tools["get_symbol_references"]("alpha.compute")))
    assert refs_res["data"]["total_count"] >= 1

    risk_res = json.loads(asyncio.run(tools["analyze_risk"](["alpha.compute"])))
    assert risk_res["data"]["risk"]["facts"]["cross_module_impact"] is True

    ambig = json.loads(asyncio.run(tools["get_symbol_context"]("shared")))
    assert ambig["error"]["code"] == McpErrorCode.AMBIGUOUS_SYMBOL

    missing = json.loads(asyncio.run(tools["get_symbol_source"]("does_not_exist")))
    assert missing["error"]["code"] == McpErrorCode.SYMBOL_NOT_FOUND


def test_cli_serve_stdio_initialize_handshake(tmp_path: Path) -> None:
    """``python -m cli.main serve`` completes an MCP ``initialize`` handshake cleanly on stdio."""
    unindexed_db = tmp_path / "serve_check.db"
    init_request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "integration-test", "version": "0.2.0"},
                },
            }
        )
        + "\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "cli.main", "serve", "--db-path", str(unindexed_db), "--debug"],
        input=init_request,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_subprocess_env(),
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    response = json.loads(proc.stdout.strip())
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    assert response["result"]["serverInfo"]["name"] == "codebase-indexer"
    assert not unindexed_db.exists()


def test_cli_mcp_call_all_8_tools_and_missing_db_safety(
    indexed_integration_db: tuple[Path, Path, str, IndexerEngine],
    tmp_path: Path,
) -> None:
    """``python -m cli.main mcp-call`` runs all 8 tools and creates zero files on missing DB."""
    db_path, _, _, _ = indexed_integration_db
    env = _subprocess_env()

    calls = [
        ("get_repo_overview", "{}"),
        ("list_files", "{}"),
        ("get_file_outline", '{"file_path": "alpha.py"}'),
        ("search_symbols", '{"query": "compute"}'),
        ("search_code", '{"query": "compute"}'),
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
                "cli.main",
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
            "cli.main",
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
    assert err_payload["result"]["error"]["code"] == McpErrorCode.DATABASE_NOT_FOUND
    assert not missing_db.exists()


def test_cli_mcp_call_shorthand_and_key_value_args(
    indexed_integration_db: tuple[Path, Path, str, IndexerEngine],
    tmp_path: Path,
) -> None:
    """``python -m cli.main mcp-call`` accepts shorthand args, ``key=value``, and unquoted JSON."""
    db_path, _, _, _ = indexed_integration_db
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
                "cli.main",
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
            "cli.main",
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
