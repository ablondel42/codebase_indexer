"""Tests for the 8 token-lean MCP tools, schemas, compact adapters, errors, and budgets."""

import asyncio
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from context.models.outline import FileOutline, OutlineNode
from context.models.symbol_context import (
    CompactSymbol,
    FocusSymbol,
    StructuralSummary,
    SymbolContext,
)
from core.models.config import AppConfig
from core.models.enums import SymbolKind, SymbolScope
from core.models.exceptions import (
    DatabaseNotFoundError,
    LspError,
    ParseError,
    PathSecurityError,
    SymbolNotFoundError,
    ValidationError,
)
from core.models.node import SymbolSummary
from core.models.range import Position
from core.ports.lsp import LspClientProtocol, LspPoolProtocol
from engine.models.queries import ContextQuery
from engine.services.facade import IndexerEngine
from graph.models.references import ReferenceDirection
from graph.models.risk import RiskAssessment, RiskDecision, RiskFacts
from indexing.models.results import BatchIndexOptions
from mcp_server.models.errors import McpErrorCode, McpErrorDetail
from mcp_server.models.inputs import (
    AnalyzeRiskInput,
    GetFileOutlineInput,
    GetRepoOverviewInput,
    GetSymbolContextInput,
    GetSymbolReferencesInput,
    GetSymbolSourceInput,
    ListFilesInput,
    SearchSymbolsInput,
)
from mcp_server.services.registry import McpToolRegistry
from mcp_server.services.repo_handlers import RepoMcpHandlers
from mcp_server.services.server import McpServerRunner
from mcp_server.services.symbol_handlers import SymbolMcpHandlers
from mcp_server.utils.compact import (
    adapt_outline,
    adapt_risk_assessment,
    adapt_symbol_context,
    adapt_symbol_summary,
    omit_none,
)
from mcp_server.utils.errors import (
    error_result,
    map_exception_to_error_result,
    success_result,
)


class _FakeMcpServer:
    """Lightweight ``.tool()`` stub capturing registered MCP tool functions."""

    def __init__(self) -> None:
        """Initialize an empty tool map."""
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(
        self,
        name: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register ``fn`` under ``name`` or ``fn.__name__``."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name or fn.__name__] = fn
            return fn

        return decorator


class _FakeLspClient(LspClientProtocol):
    """In-memory ``LspClientProtocol`` double returning a deterministic reference location."""

    def __init__(self, target_uri: str) -> None:
        """Bind the fake reference target URI."""
        self._target_uri = target_uri

    @property
    def is_running(self) -> bool:
        """Report the fake client as active."""
        return True

    def start(self, repo_root: Path | str) -> None:
        """No-op startup hook."""
        _ = repo_root

    def did_open_or_change(
        self,
        uri: str,
        text: str,
        language_id: str = "python",
    ) -> None:
        """No-op document sync hook."""
        _ = (uri, text, language_id)

    def find_references(
        self,
        uri: str,
        position: Position,
        *,
        include_declaration: bool = False,
    ) -> list[dict[str, Any]]:
        """Return one synthetic LSP reference inside ``pkg/core.py``."""
        _ = (uri, position, include_declaration)
        return [
            {
                "uri": self._target_uri,
                "range": {
                    "start": {"line": 10, "character": 15},
                    "end": {"line": 10, "character": 21},
                },
            }
        ]

    def close(self) -> None:
        """No-op shutdown hook."""


class _FakeLspPool(LspPoolProtocol):
    """In-memory ``LspPoolProtocol`` returning ``_FakeLspClient``."""

    def __init__(self, target_uri: str) -> None:
        """Bind ``target_uri`` for returned fake clients."""
        self._client = _FakeLspClient(target_uri)

    def acquire(self, repo_root: Path) -> LspClientProtocol:
        """Return the pre-configured ``_FakeLspClient``."""
        _ = repo_root
        return self._client

    def evict(self, repo_root: Path) -> None:
        """No-op eviction hook."""
        _ = repo_root

    def close_all(self) -> None:
        """No-op pool shutdown hook."""


class _FailingLspPool(LspPoolProtocol):
    """In-memory ``LspPoolProtocol`` raising ``LspError`` on ``acquire``."""

    def acquire(self, repo_root: Path) -> LspClientProtocol:
        """Raise ``LspError`` simulating unavailable ``pyright-langserver``."""
        _ = repo_root
        raise LspError("pyright unavailable")

    def evict(self, repo_root: Path) -> None:
        """No-op eviction hook."""
        _ = repo_root

    def close_all(self) -> None:
        """No-op pool shutdown hook."""


@pytest.fixture
def indexed_mcp_repo(tmp_path: Path) -> Iterator[tuple[Path, Path, str, IndexerEngine]]:
    """Index a multi-file repo via ``IndexerEngine`` and yield ``(db_path, root, id, eng)``."""
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
        "def test_ok() -> None:\n    assert True\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "mcp.db"
    engine = IndexerEngine(AppConfig(db_path=db_path, include_tests=True))
    try:
        res = engine.commands.index_repository(
            repo_root,
            BatchIndexOptions(include_tests=True),
        )
        yield db_path, repo_root, res.repo.id, engine
    finally:
        engine.close()


def test_error_envelopes_and_exception_mapping(capsys: pytest.CaptureFixture[str]) -> None:
    """Error mapping produces safe structured envelopes and logs internal errors to stderr only."""
    detail = McpErrorDetail(code="code", message="msg")
    assert detail.details is None
    assert error_result("code", "msg") == {
        "ok": False,
        "error": {"code": "code", "message": "msg", "details": None},
        "data": None,
    }
    assert success_result({"x": 1}) == {"ok": True, "data": {"x": 1}, "error": None}

    cases: list[tuple[Exception, McpErrorCode]] = [
        (DatabaseNotFoundError("x"), McpErrorCode.DATABASE_NOT_FOUND),
        (SymbolNotFoundError("x"), McpErrorCode.SYMBOL_NOT_FOUND),
        (LspError("x"), McpErrorCode.LSP_FAILURE),
        (ParseError("x"), McpErrorCode.PARSE_ERROR),
        (ValidationError("x"), McpErrorCode.INVALID_INPUT),
    ]
    for exc, expected_code in cases:
        mapped = map_exception_to_error_result(exc, operation="op")
        assert mapped["error"]["code"] == expected_code

    sec = map_exception_to_error_result(PathSecurityError("/secret/abs/path"), operation="op")
    assert sec["error"]["code"] == McpErrorCode.PATH_SECURITY
    assert "/secret/abs/path" not in sec["error"]["message"]

    internal = map_exception_to_error_result(
        RuntimeError("/private/secret/traceback"),
        operation="op",
    )
    assert internal["error"]["code"] == McpErrorCode.INTERNAL_ERROR
    assert "/private/secret/traceback" not in internal["error"]["message"]
    assert capsys.readouterr().out == ""


def test_schemas_validation_and_aliases(tmp_path: Path) -> None:
    """Pydantic input models enforce bounds, Path coercion, enums, and legacy aliases."""
    sample_db = tmp_path / "override.db"
    assert GetRepoOverviewInput(repo_id=None, db_path=str(sample_db)).db_path == str(sample_db)
    assert GetRepoOverviewInput.model_validate({"db_path": sample_db}).db_path == str(sample_db)
    with pytest.raises(PydanticValidationError):
        GetRepoOverviewInput(repo_id="   ")

    assert ListFilesInput(limit=50, offset=0).limit == 50
    with pytest.raises(PydanticValidationError):
        ListFilesInput(limit=201)

    assert GetFileOutlineInput(file_path="a.py").max_depth == 3
    assert SearchSymbolsInput(query="Worker", kind=SymbolKind.CLASS).kind == SymbolKind.CLASS

    src_in = GetSymbolSourceInput.model_validate({"symbol": "Worker", "max_lines": 100})
    assert src_in.symbol_id_or_name == "Worker"
    assert GetSymbolSourceInput(symbol_id_or_name="Worker").symbol_id_or_name == "Worker"

    assert GetSymbolContextInput.model_validate({"symbol": "Worker"}).edge_limit == 20
    refs_in = GetSymbolReferencesInput.model_validate({"symbol": "Worker", "direction": "both"})
    assert refs_in.direction == ReferenceDirection.BOTH

    risk_in = AnalyzeRiskInput.model_validate({"symbol_ids": ["a", "b"]})
    assert risk_in.symbols == ["a", "b"]
    with pytest.raises(PydanticValidationError):
        AnalyzeRiskInput(symbols=["x"] * 51)
    with pytest.raises(PydanticValidationError):
        AnalyzeRiskInput(symbols=[])


def test_adapters_omit_nulls_and_suppress_docs_above_10_children() -> None:
    """Compact adapters strip ``None`` fields and omit child docstrings when >10 children exist."""
    assert omit_none({"a": 1, "b": None}) == {"a": 1}

    adapted_sym = adapt_symbol_summary(
        SymbolSummary(
            id="sym:1",
            repo_id="repo:1",
            file_id="file:1",
            kind=SymbolKind.FUNCTION,
            name="f",
            qualified_name="m.f",
            uri="file:///m.py",
            doc_summary=None,
        )
    )
    assert "doc_summary" not in adapted_sym

    wide_ctx = SymbolContext(
        focus_symbol=FocusSymbol(
            id="sym:c",
            kind=SymbolKind.CLASS,
            name="C",
            qualified_name="m.C",
            start_line=1,
            end_line=50,
        ),
        structural_children=[
            CompactSymbol(
                id=f"sym:m{i}",
                kind=SymbolKind.METHOD,
                name=f"m{i}",
                lines=f"{i}-{i}",
                doc_summary=f"doc {i}",
            )
            for i in range(12)
        ],
        structural_summary=StructuralSummary(
            scope=SymbolScope.MODULE,
            structural_child_count=12,
        ),
    )
    compact_ctx = adapt_symbol_context(wide_ctx)
    assert len(compact_ctx["structural_children"]) == 12
    assert all("doc_summary" not in c for c in compact_ctx["structural_children"])
    assert "structural_parent" not in compact_ctx

    out = adapt_outline(
        FileOutline(
            file_path="a.py",
            module_path="a",
            line_count=10,
            is_fresh=True,
            symbols=[OutlineNode(id="sym:s", kind=SymbolKind.FUNCTION, name="f", children=[])],
        )
    )
    assert "children" not in out["symbols"][0]

    risk = RiskAssessment(
        risk_score=10,
        decision=RiskDecision.SAFE_ENOUGH,
        facts=RiskFacts(symbol_kinds=["function"]),
    )
    assert adapt_risk_assessment(risk)["decision"] == "safe_enough"


def test_all_8_mcp_tools_end_to_end_and_db_override(
    indexed_mcp_repo: tuple[Path, Path, str, IndexerEngine],
    tmp_path: Path,
) -> None:
    """All 8 MCP tools and per-call ``db_path`` override work via ``McpToolRegistry``."""
    db_path, repo_root, repo_id, _ = indexed_mcp_repo

    # Engine initialized on an empty default path overrides to db_path via engine.for_db(db_path)
    default_db = tmp_path / "unused_default.db"
    with IndexerEngine(AppConfig(db_path=default_db)) as engine:
        repo_handlers = RepoMcpHandlers(engine)
        symbol_handlers = SymbolMcpHandlers(engine)
        registry = McpToolRegistry(
            engine,
            repo_handlers=repo_handlers,
            symbol_handlers=symbol_handlers,
        )

        with pytest.raises(ValidationError):
            asyncio.run(registry.call_tool("nonexistent_tool", {}))

        ov = json.loads(
            asyncio.run(registry.call_tool("get_repo_overview", {"db_path": str(db_path)}))
        )
        assert ov["ok"] is True
        assert ov["data"]["repo"]["id"] == repo_id
        assert ov["data"]["stats"]["file_count"] == 4
        assert ov["data"]["stats"]["test_file_count"] == 1
        assert not default_db.exists()

        lf_no_tests = json.loads(
            asyncio.run(
                registry.call_tool(
                    "list_files",
                    {"path_prefix": "pkg", "include_tests": False, "db_path": str(db_path)},
                )
            )
        )
        assert lf_no_tests["data"]["total_count"] == 3

        lf_tests = json.loads(
            asyncio.run(
                registry.call_tool(
                    "list_files",
                    {"include_tests": True, "limit": 2, "offset": 1, "db_path": str(db_path)},
                )
            )
        )
        assert lf_tests["data"]["total_count"] == 4
        assert lf_tests["data"]["returned_count"] == 2
        assert lf_tests["data"]["has_more"] is True

        outline = json.loads(
            asyncio.run(
                registry.call_tool(
                    "get_file_outline",
                    {"file_path": "pkg/core.py", "db_path": str(db_path)},
                )
            )
        )
        assert outline["data"]["module_path"] == "pkg.core"
        assert [s["name"] for s in outline["data"]["symbols"]] == ["Base", "Worker", "helper"]

        search = json.loads(
            asyncio.run(
                registry.call_tool(
                    "search_symbols",
                    {"query": "Worker", "kind": "class", "db_path": str(db_path)},
                )
            )
        )
        assert search["data"]["count"] == 1
        assert search["data"]["symbols"][0]["qualified_name"] == "pkg.core.Worker"

        src = json.loads(
            asyncio.run(
                registry.call_tool(
                    "get_symbol_source",
                    {
                        "symbol": "pkg.core.Worker.run",
                        "max_lines": 2,
                        "offset": 0,
                        "db_path": str(db_path),
                    },
                )
            )
        )
        assert src["data"]["truncated"] is True
        assert "def run(" in src["data"]["source"]

        ctx = json.loads(
            asyncio.run(
                registry.call_tool(
                    "get_symbol_context",
                    {"symbol": "pkg.core.Worker", "include_source": True, "db_path": str(db_path)},
                )
            )
        )
        assert ctx["data"]["context"]["focus_symbol"]["qualified_name"] == "pkg.core.Worker"
        assert "bases" in ctx["data"]["context"]

        ambig = json.loads(
            asyncio.run(
                registry.call_tool(
                    "get_symbol_context",
                    {"symbol": "run", "db_path": str(db_path)},
                )
            )
        )
        assert ambig["ok"] is False
        assert ambig["error"]["code"] == McpErrorCode.AMBIGUOUS_SYMBOL
        assert len(ambig["error"]["details"]["candidates"]) == 2

        refs = json.loads(
            asyncio.run(
                registry.call_tool(
                    "get_symbol_references",
                    {
                        "symbol": "pkg.core.helper",
                        "direction": "incoming",
                        "db_path": str(db_path),
                    },
                )
            )
        )
        assert refs["data"]["reference_summary"]["reference_source"] == "ast"
        assert refs["data"]["total_count"] >= 1
        assert any(
            "helper(x)" in (item.get("preview") or "") for item in refs["data"]["references"]
        )

        risk = json.loads(
            asyncio.run(
                registry.call_tool(
                    "analyze_risk",
                    {"symbols": ["pkg.core.Base", "pkg.client.run"], "db_path": str(db_path)},
                )
            )
        )
        assert risk["data"]["risk"]["facts"]["inheritance_involved"] is True
        assert risk["data"]["risk"]["facts"]["target_spans_multiple_modules"] is True

        outside = repo_root.parent / "secret.py"
        outside.write_text("SECRET = 1\n", encoding="utf-8")
        target_file = repo_root / "pkg" / "client.py"
        target_file.unlink()
        target_file.symlink_to(outside)
        sec_res = json.loads(
            asyncio.run(
                registry.call_tool(
                    "get_symbol_source",
                    {"symbol": "pkg.client.run", "db_path": str(db_path)},
                )
            )
        )
        assert sec_res["ok"] is False
        assert sec_res["error"]["code"] == McpErrorCode.PATH_SECURITY


def test_get_symbol_references_refresh_lsp_and_error_handling(
    indexed_mcp_repo: tuple[Path, Path, str, IndexerEngine],
) -> None:
    """``get_symbol_references`` supports ``refresh_lsp=True`` and translates ``LspError``."""
    db_path, repo_root, _, _ = indexed_mcp_repo
    core_uri = (repo_root / "pkg" / "core.py").resolve().as_uri()

    with IndexerEngine(AppConfig(db_path=db_path), lsp_pool=_FakeLspPool(core_uri)) as ok_engine:
        ok_reg = McpToolRegistry(ok_engine)
        res = json.loads(
            asyncio.run(
                ok_reg.call_tool(
                    "get_symbol_references",
                    {"symbol": "pkg.core.helper", "refresh_lsp": True},
                )
            )
        )
        assert res["ok"] is True
        assert res["data"]["reference_summary"]["available"] is True
        assert res["data"]["reference_summary"]["reference_source"] == "lsp"

    with IndexerEngine(AppConfig(db_path=db_path), lsp_pool=_FailingLspPool()) as bad_engine:
        bad_reg = McpToolRegistry(bad_engine)
        err = json.loads(
            asyncio.run(
                bad_reg.call_tool(
                    "get_symbol_references",
                    {"symbol": "pkg.core.helper", "refresh_lsp": True},
                )
            )
        )
        assert err["ok"] is False
        assert err["error"]["code"] == McpErrorCode.LSP_FAILURE


def test_read_only_db_safety_and_missing_db_creates_zero_files(
    indexed_mcp_repo: tuple[Path, Path, str, IndexerEngine],
    tmp_path: Path,
) -> None:
    """Read-only MCP tools work on ``0o444`` DBs and never create missing DB files."""
    missing_db = tmp_path / "never_created.db"
    with IndexerEngine(AppConfig(db_path=missing_db)) as missing_engine:
        missing_reg = McpToolRegistry(missing_engine)
        res = json.loads(asyncio.run(missing_reg.call_tool("get_repo_overview", {})))
        assert res["ok"] is False
        assert res["error"]["code"] == McpErrorCode.DATABASE_NOT_FOUND
        assert not missing_db.exists()

    db_path, _, _, engine = indexed_mcp_repo
    reg = McpToolRegistry(engine)
    os.chmod(db_path, 0o444)
    try:
        ov = json.loads(asyncio.run(reg.call_tool("get_repo_overview", {})))
        assert ov["ok"] is True
        ctx = json.loads(
            asyncio.run(reg.call_tool("get_symbol_context", {"symbol": "pkg.core.Worker"}))
        )
        assert ctx["ok"] is True
    finally:
        os.chmod(db_path, 0o644)


def test_register_tools_and_create_server(
    indexed_mcp_repo: tuple[Path, Path, str, IndexerEngine],
) -> None:
    """``McpToolRegistry.bind`` and ``McpServerRunner.create_server`` register all 8 tools."""
    db_path, _, _, engine = indexed_mcp_repo
    fake_mcp = _FakeMcpServer()
    McpToolRegistry(engine).bind(fake_mcp, db_path=db_path)

    assert set(fake_mcp.tools.keys()) == set(McpToolRegistry.TOOL_NAMES)
    ov_res = json.loads(asyncio.run(fake_mcp.tools["get_repo_overview"]()))
    assert ov_res["ok"] is True

    runner = McpServerRunner(engine.config, engine=engine)
    server = runner.create_server(db_path=db_path, debug=True)
    assert server is not None


def test_40_method_class_token_budget(tmp_path: Path) -> None:
    """Indexing a 40-method class keeps context < 1,500 tokens and outline < 1,000 tokens."""
    repo_root = tmp_path / "budget_repo"
    repo_root.mkdir()
    lines = [
        '"""Large service module."""',
        "",
        "class BigService:",
        '    """Service with 40 methods."""',
    ]
    for i in range(40):
        lines.append(f"    def method_{i:02d}(self, value: int) -> int:")
        lines.append(f'        """Handle operation {i}."""')
        lines.append("        return value + 1")
    (repo_root / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    db_path = tmp_path / "budget.db"
    with IndexerEngine(AppConfig(db_path=db_path)) as engine:
        engine.commands.index_repository(repo_root)
        reg = McpToolRegistry(engine)

        ctx_raw = asyncio.run(reg.call_tool("get_symbol_context", {"symbol": "BigService"}))
        assert json.loads(ctx_raw)["ok"] is True
        assert len(ctx_raw) < 6000
        assert (len(ctx_raw) / 4) < 1500

        outline_raw = asyncio.run(reg.call_tool("get_file_outline", {"file_path": "big.py"}))
        assert json.loads(outline_raw)["ok"] is True
        assert len(outline_raw) < 4000
        assert (len(outline_raw) / 4) < 1000


def test_list_files_preserves_dot_prefix_directory(tmp_path: Path) -> None:
    """``list_files`` normalizes ``./`` without stripping leading dots from ``.github`` paths."""
    repo_root = tmp_path / "dot_repo"
    (repo_root / ".github").mkdir(parents=True)
    (repo_root / "github").mkdir()
    (repo_root / ".github" / "ci.py").write_text(
        "def check_ci() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (repo_root / "github" / "ci.py").write_text(
        "def check_non_dot() -> int:\n    return 2\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "dot.db"
    with IndexerEngine(AppConfig(db_path=db_path)) as engine:
        engine.commands.index_repository(repo_root)
        reg = McpToolRegistry(engine)
        for prefix in (".github", "./.github/", "./.github/ci.py"):
            res = json.loads(asyncio.run(reg.call_tool("list_files", {"path_prefix": prefix})))
            assert res["ok"] is True
            assert [f["file_path"] for f in res["data"]["files"]] == [".github/ci.py"]


def test_adapt_context_dedups_lexical_children_and_sanitizes_symlink_source_error(
    tmp_path: Path,
) -> None:
    """``adapt_symbol_context`` deduplicates lexical fields and hides OS paths on symlink error."""
    mod_parent = FocusSymbol(
        id="sym:mod",
        kind=SymbolKind.MODULE,
        name="mod",
        qualified_name="mod",
    )
    inner_child = CompactSymbol(
        id="sym:inner",
        kind=SymbolKind.LOCAL_FUNCTION,
        name="inner",
        lines="2-4",
    )
    same_ctx = SymbolContext(
        focus_symbol=FocusSymbol(
            id="sym:outer",
            kind=SymbolKind.FUNCTION,
            name="outer",
            qualified_name="mod.outer",
            start_line=1,
            end_line=6,
        ),
        structural_parent=mod_parent,
        structural_children=[inner_child],
        lexical_parent=mod_parent,
        lexical_children=[inner_child],
        structural_summary=StructuralSummary(
            scope=SymbolScope.MODULE,
            structural_child_count=1,
        ),
    )
    deduped = adapt_symbol_context(same_ctx)
    assert len(deduped["structural_children"]) == 1
    assert "lexical_children" not in deduped
    assert "lexical_parent" not in deduped

    distinct_ctx = same_ctx.model_copy(
        update={
            "lexical_children": [
                CompactSymbol(
                    id="sym:local_only",
                    kind=SymbolKind.LOCAL_FUNCTION,
                    name="loc",
                    lines="3-4",
                )
            ]
        }
    )
    distinct = adapt_symbol_context(distinct_ctx)
    assert [c["id"] for c in distinct["lexical_children"]] == ["sym:local_only"]

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
    with IndexerEngine(AppConfig(db_path=db_path)) as engine:
        engine.commands.index_repository(repo_root)
        reg = McpToolRegistry(engine)
        outer_id = engine.queries.get_symbol("outer").id
        victim_id = engine.queries.get_symbol("victim").id

        outer_res = json.loads(
            asyncio.run(reg.call_tool("get_symbol_context", {"symbol": outer_id}))
        )
        outer_ctx = outer_res["data"]["context"]
        assert [c["name"] for c in outer_ctx["structural_children"]] == ["inner"]
        assert "lexical_children" not in outer_ctx

        outside_secret = tmp_path / "outside_secret_target.py"
        outside_secret.write_text("SECRET_TOKEN = 'xyz'\n", encoding="utf-8")
        mod_file.unlink()
        mod_file.symlink_to(outside_secret)

        direct_ctx = engine.queries.get_symbol_context(
            ContextQuery(symbol=victim_id, include_source=True)
        )
        assert direct_ctx.focus_symbol.source is None
        direct_err = direct_ctx.focus_symbol.source_error or ""
        assert "Access denied" in direct_err
        assert str(outside_secret) not in direct_err
        assert "outside_secret_target.py" not in direct_err

        mcp_res = json.loads(
            asyncio.run(
                reg.call_tool(
                    "get_symbol_context",
                    {"symbol": victim_id, "include_source": True},
                )
            )
        )
        mcp_focus = mcp_res["data"]["context"]["focus_symbol"]
        assert "source" not in mcp_focus
        assert "Access denied" in mcp_focus["source_error"]
        assert str(outside_secret) not in mcp_focus["source_error"]
