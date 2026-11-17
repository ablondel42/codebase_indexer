"""Tests for the 8 token-lean MCP tools, schemas, adapters, errors, and budgets."""

import asyncio
from collections.abc import Iterator
import json
import os
from pathlib import Path

import pytest

from repo_context.config import AppConfig
from repo_context.context import build_symbol_context
from repo_context.exceptions import (
    DatabaseNotFoundError,
    LspError,
    ParseError,
    PathSecurityError,
    SymbolNotFoundError,
    ValidationError,
)
from repo_context.mcp.adapters import (
    adapt_context,
    adapt_edge,
    adapt_node,
    adapt_outline,
    adapt_risk_result,
)
from repo_context.mcp.errors import (
    ERROR_AMBIGUOUS_SYMBOL,
    ERROR_DATABASE_NOT_FOUND,
    ERROR_INTERNAL_ERROR,
    ERROR_INVALID_INPUT,
    ERROR_LSP_FAILURE,
    ERROR_PARSE_ERROR,
    ERROR_PATH_SECURITY,
    ERROR_SYMBOL_NOT_FOUND,
    error_result,
    map_exception_to_error_result,
    success_result,
)
from repo_context.mcp.schemas import (
    AnalyzeRiskInput,
    GetFileOutlineInput,
    GetRepoOverviewInput,
    GetSymbolContextInput,
    GetSymbolReferencesInput,
    GetSymbolSourceInput,
    ListFilesInput,
    SearchSymbolsInput,
)
from repo_context.mcp.server import create_server
from repo_context.mcp.tools import (
    analyze_risk,
    get_file_outline,
    get_repo_overview,
    get_symbol_context,
    get_symbol_references,
    get_symbol_source,
    list_files,
    register_tools,
    search_symbols,
)
from repo_context.parsing.linker import resolve_repo_edges
from repo_context.parsing.pipeline import extract_file_graph
from repo_context.parsing.scanner import scan_repository
from repo_context.storage import (
    close_connection,
    get_connection,
    initialize_database,
    record_index_run_finish,
    record_index_run_start,
    replace_file_graph,
    upsert_files,
    upsert_repo,
)


@pytest.fixture
def indexed_mcp_repo(tmp_path: Path) -> Iterator[tuple[Path, Path, str]]:
    """Index a multi-file repository into a temp DB and yield ``(db_path, repo_root, repo_id)``."""
    repo_root = tmp_path / "mcp_repo"
    (repo_root / "pkg").mkdir(parents=True)
    (repo_root / "tests").mkdir()

    (repo_root / "pkg" / "__init__.py").write_text(
        '"""Package root."""\nfrom .core import Worker\n',
        encoding="utf-8",
    )
    (repo_root / "pkg" / "core.py").write_text(
        '"""Core module."""\n\n'
        "class Base:\n"
        '    """Base class."""\n'
        "    def ping(self) -> str:\n"
        "        return 'pong'\n\n"
        "class Worker(Base):\n"
        '    """Worker service."""\n\n'
        "    def run(self, x: int) -> int:\n"
        '        """Execute work."""\n'
        "        self.ping()\n"
        "        return helper(x)\n\n"
        "def helper(v: int) -> int:\n"
        '    """Double v."""\n'
        "    return v * 2\n",
        encoding="utf-8",
    )
    (repo_root / "pkg" / "client.py").write_text(
        '"""Client module."""\n'
        "from pkg.core import Worker\n\n"
        "def run(name: str) -> str:\n"
        '    """Ambiguous short name run."""\n'
        "    w = Worker()\n"
        "    return str(w.run(1)) + name\n",
        encoding="utf-8",
    )
    (repo_root / "tests" / "test_core.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )

    db_path = tmp_path / "mcp.db"
    conn = get_connection(db_path)
    try:
        initialize_database(conn)
        cfg = AppConfig(include_tests=True)
        repo, files = scan_repository(repo_root, cfg)
        upsert_repo(conn, repo)
        run_id = record_index_run_start(conn, repo.id, run_type="full")
        upsert_files(conn, files)
        total_nodes = 0
        total_edges = 0
        for rec in files:
            nodes, edges, _ = extract_file_graph(repo.id, rec, repo_root, config=cfg)
            replace_file_graph(conn, rec.id, nodes, edges)
            total_nodes += len(nodes)
            total_edges += len(edges)
        resolve_repo_edges(conn, repo.id)
        record_index_run_finish(
            conn,
            run_id,
            "completed",
            files_scanned=len(files),
            nodes_extracted=total_nodes,
            edges_extracted=total_edges,
        )
        conn.commit()
    finally:
        close_connection(conn)

    yield db_path, repo_root, repo.id


class _FakeFastMCP:
    """Lightweight duck-typed FastMCP stub for testing ``register_tools`` without ``mcp``."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str | None = None):
        def decorator(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator


def test_error_envelopes_and_exception_mapping(capsys: pytest.CaptureFixture[str]) -> None:
    """Error mapping produces safe structured payloads and logs internal errors to stderr only."""
    assert error_result("code", "msg") == {
        "ok": False,
        "error": {"code": "code", "message": "msg", "details": None},
        "data": None,
    }
    assert success_result({"x": 1}) == {"ok": True, "data": {"x": 1}, "error": None}

    assert map_exception_to_error_result(DatabaseNotFoundError("x"), operation="op")["error"]["code"] == ERROR_DATABASE_NOT_FOUND
    assert map_exception_to_error_result(SymbolNotFoundError("x"), operation="op")["error"]["code"] == ERROR_SYMBOL_NOT_FOUND
    sec = map_exception_to_error_result(PathSecurityError("/secret/abs/path"), operation="op")
    assert sec["error"]["code"] == ERROR_PATH_SECURITY
    assert "/secret/abs/path" not in sec["error"]["message"]
    assert map_exception_to_error_result(LspError("x"), operation="op")["error"]["code"] == ERROR_LSP_FAILURE
    assert map_exception_to_error_result(ParseError("x"), operation="op")["error"]["code"] == ERROR_PARSE_ERROR
    assert map_exception_to_error_result(ValidationError("x"), operation="op")["error"]["code"] == ERROR_INVALID_INPUT

    internal = map_exception_to_error_result(RuntimeError("/private/secret/traceback"), operation="op")
    assert internal["error"]["code"] == ERROR_INTERNAL_ERROR
    assert "/private/secret/traceback" not in internal["error"]["message"]
    captured = capsys.readouterr()
    assert captured.out == ""


def test_schemas_validation_and_aliases() -> None:
    """Pydantic input schemas enforce bounds and accept legacy aliases cleanly."""
    assert GetRepoOverviewInput(repo_id=None).repo_id is None
    with pytest.raises(Exception):
        GetRepoOverviewInput(repo_id="   ")

    assert ListFilesInput(limit=50, offset=0).limit == 50
    with pytest.raises(Exception):
        ListFilesInput(limit=201)

    assert GetFileOutlineInput(file_path="a.py").max_depth == 3
    assert SearchSymbolsInput(query="Worker", kind="class").kind == "class"

    src_in = GetSymbolSourceInput(symbol="Worker", max_lines=100)
    assert src_in.symbol_id_or_name == "Worker"
    assert GetSymbolSourceInput(symbol_id_or_name="Worker").symbol_id_or_name == "Worker"

    assert GetSymbolContextInput(symbol="Worker").edge_limit == 20
    assert GetSymbolReferencesInput(symbol="Worker", direction="both").direction == "both"

    risk_in = AnalyzeRiskInput(symbol_ids=["a", "b"])
    assert risk_in.symbols == ["a", "b"]
    with pytest.raises(Exception):
        AnalyzeRiskInput(symbols=["x"] * 51)
    with pytest.raises(Exception):
        AnalyzeRiskInput(symbols=[])


def test_adapters_omit_nulls_and_suppress_docs_above_10_children() -> None:
    """Adapters strip None noise and drop child docstrings when >10 children are present."""
    adapted = adapt_node(
        {"id": "sym:1", "kind": "function", "name": "f", "qualified_name": "m.f", "doc_summary": None}
    )
    assert "doc_summary" not in adapted

    edge_adapted = adapt_edge({"kind": "calls", "from_id": "a", "to_id": "b", "confidence": 0.9})
    assert "preview" not in edge_adapted

    class DummyCtx:
        focus_symbol = {"id": "sym:c", "kind": "class", "name": "C", "qualified_name": "m.C", "start_line": 1, "end_line": 50}
        structural_parent = None
        structural_children = [
            {"id": f"sym:m{i}", "kind": "method", "name": f"m{i}", "lines": f"{i}-{i}", "doc_summary": f"doc {i}"}
            for i in range(12)
        ]
        lexical_parent = None
        lexical_children = []
        incoming_edges = []
        outgoing_edges = []
        structural_summary = {"scope": "module", "structural_child_count": 12}
        reference_summary = {}
        freshness = {"is_fresh": True}
        confidence = {"focus_symbol_confidence": 1.0}

    compact_ctx = adapt_context(DummyCtx())
    assert len(compact_ctx["structural_children"]) == 12
    assert all("doc_summary" not in c for c in compact_ctx["structural_children"])
    assert "structural_parent" not in compact_ctx

    out = adapt_outline({"file_path": "a.py", "module_path": "a", "symbols": [{"id": "s", "kind": "function", "name": "f", "children": []}]})
    assert "children" not in out["symbols"][0]

    class DummyRisk:
        risk_score = 10
        decision = "safe_enough"
        issues = []
        targets = []
        class facts:
            target_count = 0
            symbol_ids = []
            symbol_kinds = set()
            reference_counts = {}
            reference_availability = {}
            referencing_file_counts = {}
            referencing_module_counts = {}
            touches_public_surface = False
            touches_local_scope_only = False
            target_spans_multiple_files = False
            target_spans_multiple_modules = False
            cross_file_impact = False
            cross_module_impact = False
            inheritance_involved = False
            stale_symbols = []
            low_confidence_symbols = []
            low_confidence_edges = []
            extra = {}

    assert adapt_risk_result(DummyRisk())["decision"] == "safe_enough"


def test_all_8_mcp_tools_end_to_end(indexed_mcp_repo: tuple[Path, Path, str]) -> None:
    """All 8 MCP tools return valid token-lean JSON envelopes against a temp DB."""
    db_path, repo_root, repo_id = indexed_mcp_repo

    # 1. get_repo_overview
    ov = json.loads(asyncio.run(get_repo_overview(db_path=db_path)))
    assert ov["ok"] is True
    assert ov["data"]["repo"]["id"] == repo_id
    assert ov["data"]["stats"]["file_count"] == 4
    assert ov["data"]["stats"]["test_file_count"] == 1

    # 2. list_files
    lf_no_tests = json.loads(asyncio.run(list_files(path_prefix="pkg", include_tests=False, db_path=db_path)))
    assert lf_no_tests["ok"] is True
    assert lf_no_tests["data"]["total_count"] == 3
    lf_tests = json.loads(asyncio.run(list_files(include_tests=True, limit=2, offset=1, db_path=db_path)))
    assert lf_tests["data"]["total_count"] == 4
    assert lf_tests["data"]["returned_count"] == 2
    assert lf_tests["data"]["has_more"] is True

    # 3. get_file_outline
    outline = json.loads(asyncio.run(get_file_outline("pkg/core.py", db_path=db_path)))
    assert outline["ok"] is True
    assert outline["data"]["module_path"] == "pkg.core"
    assert [s["name"] for s in outline["data"]["symbols"]] == ["Base", "Worker", "helper"]

    # 4. search_symbols
    search = json.loads(asyncio.run(search_symbols("Worker", kind="class", db_path=db_path)))
    assert search["ok"] is True
    assert search["data"]["count"] == 1
    assert search["data"]["symbols"][0]["qualified_name"] == "pkg.core.Worker"

    # 5. get_symbol_source
    src = json.loads(asyncio.run(get_symbol_source("pkg.core.Worker.run", max_lines=2, offset=0, db_path=db_path)))
    assert src["ok"] is True
    assert src["data"]["truncated"] is True
    assert "def run(" in src["data"]["source"]

    # 6. get_symbol_context + ambiguity handling
    ctx = json.loads(asyncio.run(get_symbol_context("pkg.core.Worker", include_source=True, db_path=db_path)))
    assert ctx["ok"] is True
    assert ctx["data"]["context"]["focus_symbol"]["qualified_name"] == "pkg.core.Worker"
    assert "bases" in ctx["data"]["context"]

    ambig = json.loads(asyncio.run(get_symbol_context("run", db_path=db_path)))
    assert ambig["ok"] is False
    assert ambig["error"]["code"] == ERROR_AMBIGUOUS_SYMBOL
    assert len(ambig["error"]["details"]["candidates"]) == 2

    # 7. get_symbol_references (AST fallback & 1-line preview)
    refs = json.loads(asyncio.run(get_symbol_references("pkg.core.helper", direction="incoming", db_path=db_path)))
    assert refs["ok"] is True
    assert refs["data"]["reference_summary"]["reference_source"] == "ast"
    assert refs["data"]["total_count"] >= 1
    assert any("helper(x)" in (item.get("preview") or "") for item in refs["data"]["references"])

    # 8. analyze_risk
    risk = json.loads(asyncio.run(analyze_risk(["pkg.core.Base", "pkg.client.run"], db_path=db_path)))
    assert risk["ok"] is True
    assert risk["data"]["risk"]["facts"]["inheritance_involved"] is True
    assert risk["data"]["risk"]["facts"]["target_spans_multiple_modules"] is True

    # Missing file symlink security error via get_symbol_source after replacing file with symlink
    outside = repo_root.parent / "secret.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    target_file = repo_root / "pkg" / "client.py"
    target_file.unlink()
    target_file.symlink_to(outside)
    sec_res = json.loads(asyncio.run(get_symbol_source("pkg.client.run", db_path=db_path)))
    assert sec_res["ok"] is False
    assert sec_res["error"]["code"] == ERROR_PATH_SECURITY


def test_get_symbol_references_refresh_lsp_and_error_handling(
    indexed_mcp_repo: tuple[Path, Path, str],
) -> None:
    """get_symbol_references supports refresh_lsp=True with mocked LspSessionPool and catches LspError."""
    db_path, repo_root, _ = indexed_mcp_repo

    class _FakeClient:
        is_running = True

        def start(self, root: str) -> None:
            pass

        def did_open_or_change(self, uri: str, text: str) -> None:
            pass

        def find_references(self, uri: str, line: int, character: int, include_declaration: bool = False):
            return [
                {
                    "uri": (repo_root / "pkg" / "core.py").resolve().as_uri(),
                    "range": {"start": {"line": 13, "character": 15}, "end": {"line": 13, "character": 21}},
                }
            ]

    class _FakePool:
        def get_or_start(self, root: str):
            return _FakeClient()

        def close_repo(self, root: str) -> None:
            pass

    res = json.loads(
        asyncio.run(
            get_symbol_references(
                "pkg.core.helper",
                refresh_lsp=True,
                db_path=db_path,
                lsp_pool=_FakePool(),  # type: ignore[arg-type]
            )
        )
    )
    assert res["ok"] is True
    assert res["data"]["reference_summary"]["available"] is True
    assert res["data"]["reference_summary"]["reference_source"] == "lsp"

    class _FailingPool:
        def get_or_start(self, root: str):
            raise LspError("pyright unavailable")

        def close_repo(self, root: str) -> None:
            pass

    err = json.loads(
        asyncio.run(
            get_symbol_references(
                "pkg.core.helper",
                refresh_lsp=True,
                db_path=db_path,
                lsp_pool=_FailingPool(),  # type: ignore[arg-type]
            )
        )
    )
    assert err["ok"] is False
    assert err["error"]["code"] == ERROR_LSP_FAILURE


def test_read_only_db_safety_and_missing_db_creates_zero_files(
    indexed_mcp_repo: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    """Non-refresh MCP tools work on read-only DBs and never create missing DB files."""
    missing_db = tmp_path / "never_created.db"
    res = json.loads(asyncio.run(get_repo_overview(db_path=missing_db)))
    assert res["ok"] is False
    assert res["error"]["code"] == ERROR_DATABASE_NOT_FOUND
    assert not missing_db.exists()

    db_path, _, _ = indexed_mcp_repo
    os.chmod(db_path, 0o444)
    try:
        ov = json.loads(asyncio.run(get_repo_overview(db_path=db_path)))
        assert ov["ok"] is True
        ctx = json.loads(asyncio.run(get_symbol_context("pkg.core.Worker", db_path=db_path)))
        assert ctx["ok"] is True
    finally:
        os.chmod(db_path, 0o644)


def test_register_tools_and_create_server(indexed_mcp_repo: tuple[Path, Path, str]) -> None:
    """register_tools registers all 8 tools cleanly with a FastMCP stub or real FastMCP."""
    db_path, _, _ = indexed_mcp_repo
    fake_mcp = _FakeFastMCP()
    register_tools(fake_mcp, db_path=db_path)

    expected = {
        "get_repo_overview",
        "list_files",
        "get_file_outline",
        "search_symbols",
        "get_symbol_source",
        "get_symbol_context",
        "get_symbol_references",
        "analyze_risk",
    }
    assert set(fake_mcp.tools.keys()) == expected
    ov_res = json.loads(asyncio.run(fake_mcp.tools["get_repo_overview"]()))  # type: ignore[operator]
    assert ov_res["ok"] is True

    server = create_server(db_path=db_path)
    assert server is not None


def test_40_method_class_token_budget(tmp_path: Path) -> None:
    """Indexing a 40-method class keeps get_symbol_context < 1,500 tokens (~6,000 chars) and get_file_outline < 1,000 tokens."""
    repo_root = tmp_path / "budget_repo"
    repo_root.mkdir()
    lines = ['"""Large service module."""', "", "class BigService:", '    """Service with 40 methods."""']
    for i in range(40):
        lines.append(f"    def method_{i:02d}(self, value: int) -> int:")
        lines.append(f'        """Handle operation {i}."""')
        lines.append("        return value + 1")
    (repo_root / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    db_path = tmp_path / "budget.db"
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
        conn.commit()
    finally:
        close_connection(conn)

    ctx_raw = asyncio.run(get_symbol_context("BigService", db_path=db_path))
    ctx_data = json.loads(ctx_raw)
    assert ctx_data["ok"] is True
    assert len(ctx_raw) < 6000
    assert (len(ctx_raw) / 4) < 1500

    outline_raw = asyncio.run(get_file_outline("big.py", db_path=db_path))
    outline_data = json.loads(outline_raw)
    assert outline_data["ok"] is True
    assert len(outline_raw) < 4000
    assert (len(outline_raw) / 4) < 1000


def test_list_files_preserves_dot_prefix_directory(tmp_path: Path) -> None:
    """list_files normalizes ./ without stripping leading dots from .github paths."""
    repo_root = tmp_path / "dot_repo"
    (repo_root / ".github").mkdir(parents=True)
    (repo_root / "github").mkdir()
    (repo_root / ".github" / "ci.py").write_text(
        "def check_ci() -> int:\n    return 1\n", encoding="utf-8"
    )
    (repo_root / "github" / "ci.py").write_text(
        "def check_non_dot() -> int:\n    return 2\n", encoding="utf-8"
    )

    db_path = tmp_path / "dot.db"
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
        conn.commit()
    finally:
        close_connection(conn)

    for prefix in (".github", "./.github/", "./.github/ci.py"):
        res = json.loads(asyncio.run(list_files(path_prefix=prefix, db_path=db_path)))
        assert res["ok"] is True
        assert [f["file_path"] for f in res["data"]["files"]] == [".github/ci.py"]


def test_adapt_context_dedups_lexical_children_and_sanitizes_symlink_source_error(
    tmp_path: Path,
) -> None:
    """adapt_context drops duplicate lexical_children/parent and source_error hides OS paths."""

    class SameChildrenCtx:
        focus_symbol = {
            "id": "sym:outer",
            "kind": "function",
            "name": "outer",
            "qualified_name": "mod.outer",
            "start_line": 1,
            "end_line": 6,
        }
        structural_parent = {"id": "sym:mod", "kind": "module", "name": "mod"}
        structural_children = [
            {"id": "sym:inner", "kind": "local_function", "name": "inner", "lines": "2-4"}
        ]
        lexical_parent = {"id": "sym:mod", "kind": "module", "name": "mod"}
        lexical_children = [
            {"id": "sym:inner", "kind": "local_function", "name": "inner", "lines": "2-4"}
        ]
        incoming_edges = []
        outgoing_edges = []
        structural_summary = {"scope": "module", "structural_child_count": 1}
        reference_summary = {}
        freshness = {"is_fresh": True}
        confidence = {"focus_symbol_confidence": 1.0}

    deduped = adapt_context(SameChildrenCtx())
    assert len(deduped["structural_children"]) == 1
    assert "lexical_children" not in deduped
    assert "lexical_parent" not in deduped

    class DistinctChildrenCtx(SameChildrenCtx):
        lexical_children = [
            {"id": "sym:local_only", "kind": "local_function", "name": "loc", "lines": "3-4"}
        ]

    distinct = adapt_context(DistinctChildrenCtx())
    assert "lexical_children" in distinct
    assert [c["id"] for c in distinct["lexical_children"]] == ["sym:local_only"]

    # End-to-end check on indexed local function + symlinked file source_error sanitization
    repo_root = tmp_path / "ctx_sec_repo"
    repo_root.mkdir()
    mod_file = repo_root / "mod.py"
    mod_file.write_text(
        "def outer() -> int:\n"
        "    def inner() -> int:\n"
        "        return 1\n"
        "    return inner()\n\n"
        "def victim() -> int:\n"
        "    return 2\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "ctx_sec.db"
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
        conn.commit()
        outer_id = conn.execute(
            "SELECT id FROM nodes WHERE name = 'outer' AND kind = 'function'"
        ).fetchone()["id"]
        victim_id = conn.execute(
            "SELECT id FROM nodes WHERE name = 'victim' AND kind = 'function'"
        ).fetchone()["id"]
    finally:
        close_connection(conn)

    outer_res = json.loads(asyncio.run(get_symbol_context(outer_id, db_path=db_path)))
    assert outer_res["ok"] is True
    outer_ctx = outer_res["data"]["context"]
    assert [c["name"] for c in outer_ctx["structural_children"]] == ["inner"]
    assert "lexical_children" not in outer_ctx

    outside_secret = tmp_path / "outside_secret_target.py"
    outside_secret.write_text("SECRET_TOKEN = 'xyz'\n", encoding="utf-8")
    mod_file.unlink()
    mod_file.symlink_to(outside_secret)

    verify_conn = get_connection(db_path, read_only=True)
    try:
        direct_ctx = build_symbol_context(verify_conn, victim_id, include_source=True)
        assert direct_ctx is not None
        assert "source" not in direct_ctx.focus_symbol
        direct_err = direct_ctx.focus_symbol.get("source_error", "")
        assert "Access denied" in direct_err
        assert str(outside_secret) not in direct_err
        assert "outside_secret_target.py" not in direct_err
        assert str(repo_root) not in direct_err
    finally:
        close_connection(verify_conn)

    mcp_res = json.loads(
        asyncio.run(get_symbol_context(victim_id, include_source=True, db_path=db_path))
    )
    assert mcp_res["ok"] is True
    mcp_focus = mcp_res["data"]["context"]["focus_symbol"]
    assert "source" not in mcp_focus
    assert "Access denied" in mcp_focus["source_error"]
    assert str(outside_secret) not in mcp_focus["source_error"]
    assert "outside_secret_target.py" not in mcp_focus["source_error"]
