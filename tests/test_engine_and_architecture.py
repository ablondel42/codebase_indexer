"""Headless ``IndexerEngine`` UI query tests and AST architectural boundary gate."""

import ast
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest

import context.services.source as context_source_mod
import engine.services.facade as engine_facade_mod
from context.models.source import SymbolSnippet
from core.models.config import AppConfig
from core.models.edge import Edge
from core.models.enums import EdgeKind
from core.models.exceptions import SymbolNotFoundError
from core.models.file import VerifiedFileText
from core.models.node import SymbolSummary
from core.models.range import Range
from core.utils.file_io import read_verified_file_text
from engine import (
    IndexerEngine,
    SubgraphQuery,
    SymbolMembersQuery,
    SymbolSnippetsQuery,
)
from graph.models.subgraph import SubgraphSlice
from storage.services.uow import SqlUnitOfWorkFactory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"

EXPECTED_DOMAINS: frozenset[str] = frozenset(
    {
        "core",
        "storage",
        "lsp",
        "parsing",
        "graph",
        "context",
        "indexing",
        "engine",
        "mcp_server",
        "cli",
    }
)

DOMAIN_SERVICE_PACKAGES: frozenset[str] = frozenset(
    {"storage", "parsing", "lsp", "graph", "context", "indexing", "engine"}
)

STDLIB_NON_SQLITE: frozenset[str] = frozenset(sys.stdlib_module_names) - {"sqlite3"}

ALLOWED_IMPORTS_BY_DOMAIN: dict[str, frozenset[str]] = {
    "core": frozenset({"core", "pydantic"}),
    "storage": frozenset({"core", "storage", "pydantic", "sqlalchemy", "sqlite3"}),
    "lsp": frozenset({"core", "lsp", "pydantic"}),
    "parsing": frozenset({"core", "parsing", "pydantic"}),
    "graph": frozenset({"core", "graph", "pydantic"}),
    "context": frozenset({"core", "graph", "context", "pydantic"}),
    "indexing": frozenset({"core", "parsing", "lsp", "indexing", "pydantic", "watchdog"}),
    "engine": frozenset(
        {
            "core",
            "storage",
            "lsp",
            "parsing",
            "graph",
            "context",
            "indexing",
            "engine",
            "pydantic",
        }
    ),
    "mcp_server": frozenset(
        {
            "core",
            "engine",
            "graph.models",
            "context.models",
            "indexing.models",
            "mcp_server",
            "pydantic",
            "mcp",
        }
    ),
    "cli": frozenset(
        {
            "core",
            "engine",
            "graph.models",
            "context.models",
            "indexing.models",
            "cli",
            "pydantic",
        }
    ),
}

ALLOWED_PROTOCOL_DICT_RETURNS: frozenset[str] = frozenset(
    {
        "dict[str, FileRecord]",
        "dict[str, SymbolNode]",
        "dict[str, SymbolSummary]",
    }
)

DOCUMENTED_BROAD_EXCEPT_FILES: frozenset[str] = frozenset(
    {
        "cli/services/app.py",
        "indexing/services/batch.py",
        "indexing/services/incremental.py",
        "indexing/services/scheduler.py",
        "lsp/services/transport.py",
        "mcp_server/services/registry.py",
        "storage/services/uow.py",
    }
)

LOG_METHODS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical"}
)


@pytest.fixture
def indexed_ui_engine(tmp_path: Path) -> Iterator[tuple[IndexerEngine, Path, Path]]:
    """Create and index a multi-module Python codebase for headless UI engine tests."""
    repo_root = tmp_path / "ui_repo"
    (repo_root / "pkg").mkdir(parents=True)
    (repo_root / "pkg" / "__init__.py").write_text('"""UI sample package."""\n', encoding="utf-8")
    (repo_root / "pkg" / "models.py").write_text(
        '"""Domain entity models."""\n\n'
        "def format_label(prefix: str, value: str) -> str:\n"
        '    """Format a prefixed label."""\n'
        '    return f"{prefix}:{value}"\n\n'
        "def _private_helper() -> int:\n"
        "    return 7\n\n"
        "class BaseEntity:\n"
        '    """Root entity base class."""\n\n'
        "    def ident(self) -> str:\n"
        '        return "base"\n\n'
        "    def _secret_tag(self) -> str:\n"
        '        return "hidden"\n\n'
        "class UserEntity(BaseEntity):\n"
        '    """Concrete user entity."""\n\n'
        '    role: str = "member"\n\n'
        "    def display(self) -> str:\n"
        "        return format_label(self.role, self.ident())\n\n"
        "    def _audit(self) -> None:\n"
        "        _private_helper()\n\n"
        "def duplicate_name() -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (repo_root / "pkg" / "service.py").write_text(
        '"""Workflow orchestration module."""\n'
        "from pkg.models import UserEntity, format_label\n\n"
        "def step_three(text: str) -> str:\n"
        '    return format_label("done", text)\n\n'
        "def step_two(text: str) -> str:\n"
        "    return step_three(text)\n\n"
        "def run_workflow(user: UserEntity) -> str:\n"
        '    """Execute user display workflow."""\n'
        "    rendered = user.display()\n"
        "    return step_two(rendered)\n\n"
        "def duplicate_name() -> int:\n"
        "    return 2\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "ui_engine.db"
    engine = IndexerEngine(AppConfig(db_path=db_path))
    try:
        engine.commands.index_repository(repo_root)
        yield engine, db_path, repo_root
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Suite 1: Headless & UI-Ready IndexerEngine Integration Tests
# ---------------------------------------------------------------------------


class TestHeadlessIndexerEngineQueries:
    """Integration suite verifying ``IndexerEngine`` UI queries and ``for_db`` scoping."""

    def test_get_symbol_selectors_and_ambiguous_disambiguation(
        self,
        indexed_ui_engine: tuple[IndexerEngine, Path, Path],
    ) -> None:
        """``get_symbol`` resolves IDs, qualified/short/file:symbol selectors, and ambiguities."""
        engine, _, _ = indexed_ui_engine

        by_qname = engine.queries.get_symbol("pkg.models.UserEntity")
        assert isinstance(by_qname, SymbolSummary)
        assert by_qname.file_path == "pkg/models.py"
        assert by_qname.module_path == "pkg.models"

        by_id = engine.queries.get_symbol(by_qname.id)
        by_short = engine.queries.get_symbol("UserEntity")
        assert by_id.id == by_qname.id
        assert by_short.id == by_qname.id

        dup_models = engine.queries.get_symbol("pkg/models.py:duplicate_name")
        dup_service = engine.queries.get_symbol("pkg/service.py:duplicate_name")
        assert dup_models.id != dup_service.id
        assert dup_models.qualified_name == "pkg.models.duplicate_name"
        assert dup_service.qualified_name == "pkg.service.duplicate_name"

        with pytest.raises(SymbolNotFoundError) as ambig_exc:
            engine.queries.get_symbol("duplicate_name")
        assert ambig_exc.value.error_code == "ambiguous_symbol"
        assert ambig_exc.value.context is not None
        candidates = ambig_exc.value.context["candidates"]
        assert len(candidates) == 2
        assert {item["qualified_name"] for item in candidates} == {
            "pkg.models.duplicate_name",
            "pkg.service.duplicate_name",
        }

        with pytest.raises(SymbolNotFoundError) as missing_exc:
            engine.queries.get_symbol("NoSuchSymbolAnywhere")
        assert missing_exc.value.error_code == "symbol_not_found"

    def test_get_symbol_members_on_class_and_module_with_privacy_filter(
        self,
        indexed_ui_engine: tuple[IndexerEngine, Path, Path],
    ) -> None:
        """``get_symbol_members`` lists class and module members respecting ``include_private``."""
        engine, _, _ = indexed_ui_engine

        all_cls_members = engine.queries.get_symbol_members(
            SymbolMembersQuery(symbol="pkg.models.UserEntity", include_private=True)
        )
        assert [m.name for m in all_cls_members] == ["role", "display", "_audit"]

        public_cls_members = engine.queries.get_symbol_members(
            SymbolMembersQuery(symbol="pkg.models.UserEntity", include_private=False)
        )
        assert [m.name for m in public_cls_members] == ["role", "display"]

        all_mod_members = engine.queries.get_symbol_members(
            SymbolMembersQuery(symbol="pkg.models", include_private=True)
        )
        mod_all_names = [m.name for m in all_mod_members]
        assert "_private_helper" in mod_all_names
        assert "UserEntity" in mod_all_names

        public_mod_members = engine.queries.get_symbol_members(
            SymbolMembersQuery(symbol="pkg.models", include_private=False)
        )
        mod_pub_names = [m.name for m in public_mod_members]
        assert "_private_helper" not in mod_pub_names
        assert mod_pub_names == ["format_label", "BaseEntity", "UserEntity", "duplicate_name"]

    def test_get_symbol_snippets_raw_vs_numbered_padding_ranges_and_single_disk_read(
        self,
        indexed_ui_engine: tuple[IndexerEngine, Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``get_symbol_snippets`` returns UI/CLI fields and reads each file at most once."""
        engine, _, _ = indexed_ui_engine
        file_read_counts: Counter[str] = Counter()

        def spy_read_verified(
            repo_root: Path | str,
            rel_file_path: str,
            *,
            expected_content_hash: str | None = None,
            max_file_size_bytes: int = 2 * 1024 * 1024,
        ) -> VerifiedFileText:
            file_read_counts[rel_file_path] += 1
            return read_verified_file_text(
                repo_root,
                rel_file_path,
                expected_content_hash=expected_content_hash,
                max_file_size_bytes=max_file_size_bytes,
            )

        monkeypatch.setattr(context_source_mod, "read_verified_file_text", spy_read_verified)

        snippets = engine.queries.get_symbol_snippets(
            SymbolSnippetsQuery(
                symbols=[
                    "pkg.models.BaseEntity",
                    "pkg.models.UserEntity",
                    "pkg.models.format_label",
                    "UserEntity",
                    "pkg.service.run_workflow",
                ],
                padding_lines=2,
            )
        )

        # Duplicate "UserEntity" is deduplicated; pkg/models.py read ONCE for 3 symbols!
        assert len(snippets) == 4
        assert file_read_counts["pkg/models.py"] == 1
        assert file_read_counts["pkg/service.py"] == 1

        user_snip = next(s for s in snippets if s.qualified_name == "pkg.models.UserEntity")
        assert isinstance(user_snip, SymbolSnippet)
        assert "class UserEntity(BaseEntity):" in user_snip.raw_source
        assert f"{user_snip.start_line}: " not in user_snip.raw_source
        assert f"{user_snip.window_start_line}: " in user_snip.numbered_source
        assert f"{user_snip.start_line}: class UserEntity(BaseEntity):" in user_snip.numbered_source

        assert user_snip.window_start_line == max(1, user_snip.start_line - 2)
        assert user_snip.window_end_line == user_snip.end_line + 2
        assert isinstance(user_snip.decl_range, Range)
        assert isinstance(user_snip.selection_range, Range)
        assert user_snip.decl_range.start.line == user_snip.start_line - 1

    def test_get_symbol_subgraph_closure_deduplication_and_truncation(
        self,
        indexed_ui_engine: tuple[IndexerEngine, Path, Path],
    ) -> None:
        """``get_symbol_subgraph`` returns deduplicated nodes, closed edges, and ``truncated``."""
        engine, _, _ = indexed_ui_engine

        full_slice = engine.queries.get_symbol_subgraph(
            SubgraphQuery(
                symbols=["pkg.service.run_workflow"],
                depth=2,
                max_nodes=50,
                kinds=(EdgeKind.CALLS, EdgeKind.INHERITS),
            )
        )
        assert isinstance(full_slice, SubgraphSlice)
        assert full_slice.depth == 2
        assert full_slice.truncated is False

        node_ids = [n.id for n in full_slice.nodes]
        assert len(node_ids) == len(set(node_ids))
        node_id_set = set(node_ids)
        qnames = {n.qualified_name for n in full_slice.nodes}
        assert "pkg.service.run_workflow" in qnames
        assert "pkg.service.step_two" in qnames
        assert "pkg.service.step_three" in qnames

        assert full_slice.edges
        for edge in full_slice.edges:
            assert isinstance(edge, Edge)
            assert edge.from_id in node_id_set
            assert edge.to_id in node_id_set

        bounded_slice = engine.queries.get_symbol_subgraph(
            SubgraphQuery(
                symbols=["pkg.service.run_workflow"],
                depth=2,
                max_nodes=2,
            )
        )
        assert len(bounded_slice.nodes) == 2
        assert bounded_slice.truncated is True
        bounded_ids = {n.id for n in bounded_slice.nodes}
        assert all(e.from_id in bounded_ids and e.to_id in bounded_ids for e in bounded_slice.edges)

    def test_for_db_reuses_self_caches_uow_factories_and_shares_lsp_pool(
        self,
        indexed_ui_engine: tuple[IndexerEngine, Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``engine.for_db`` returns ``self`` when unchanged, caches UoW factories, & shares LSP."""
        engine, db_path, _ = indexed_ui_engine
        assert engine.for_db(None) is engine
        assert engine.for_db(db_path) is engine
        assert engine.for_db(str(db_path)) is engine

        created_db_paths: list[Path] = []

        def spy_uow_factory(target: Path | str) -> SqlUnitOfWorkFactory:
            created_db_paths.append(Path(target))
            return SqlUnitOfWorkFactory(target)

        monkeypatch.setattr(engine_facade_mod, "SqlUnitOfWorkFactory", spy_uow_factory)

        second_repo = tmp_path / "second_repo"
        second_repo.mkdir()
        (second_repo / "solo.py").write_text(
            "def only_in_second() -> int:\n    return 99\n",
            encoding="utf-8",
        )
        db2 = tmp_path / "second.db"

        scoped_a = engine.for_db(db2)
        scoped_b = engine.for_db(str(db2))

        assert scoped_a is not engine
        assert scoped_a.db_path == db2
        assert scoped_a.lsp_pool is engine.lsp_pool
        assert scoped_b.lsp_pool is engine.lsp_pool
        assert len(created_db_paths) == 1

        scoped_a.commands.index_repository(second_repo)
        assert scoped_b.queries.get_symbol("only_in_second").name == "only_in_second"
        assert engine.queries.get_symbol("UserEntity").name == "UserEntity"


# ---------------------------------------------------------------------------
# Suite 2: Automated AST Architectural Boundary Gate over src/
# ---------------------------------------------------------------------------


def _iter_src_python_files() -> list[Path]:
    """Return sorted ``.py`` files inside ``src/`` excluding packaging metadata."""
    return sorted(
        path
        for path in SRC_DIR.rglob("*.py")
        if not any(part.endswith(".egg-info") for part in path.parts)
    )


def _is_module_allowed(domain: str, rel_posix: str, module_name: str) -> bool:
    """Check whether ``module_name`` is permitted inside ``domain`` / ``rel_posix``."""
    top = module_name.split(".")[0]
    if top in STDLIB_NON_SQLITE:
        return True
    allowed = set(ALLOWED_IMPORTS_BY_DOMAIN[domain])
    if rel_posix == "cli/services/server.py":
        allowed.add("mcp_server")
    if top in allowed:
        return True
    parts = module_name.split(".")
    return len(parts) >= 2 and f"{parts[0]}.{parts[1]}" in allowed


def _check_import_from_node(
    domain: str,
    rel_posix: str,
    node: ast.ImportFrom,
    top_body_ids: set[int],
) -> list[str]:
    """Return architectural violations found on one ``ast.ImportFrom`` statement."""
    errors: list[str] = []
    if id(node) not in top_body_ids:
        errors.append(f"{rel_posix}:{node.lineno}: non-top-level from-import")
    if node.level > 0:
        errors.append(f"{rel_posix}:{node.lineno}: relative import forbidden")
        return errors
    if not node.module:
        return errors
    if node.module == "contextlib" and any(alias.name == "suppress" for alias in node.names):
        errors.append(f"{rel_posix}:{node.lineno}: contextlib.suppress is banned")
    if node.module in ("graph", "context", "indexing") and domain in ("mcp_server", "cli"):
        bad = [alias.name for alias in node.names if alias.name != "models"]
        if bad:
            errors.append(
                f"{rel_posix}:{node.lineno}: forbidden subpackage import from {node.module}: {bad}"
            )
        return errors
    if not _is_module_allowed(domain, rel_posix, node.module):
        errors.append(f"{rel_posix}:{node.lineno}: disallowed from-import '{node.module}'")
    return errors


def _check_import_node(
    domain: str,
    rel_posix: str,
    node: ast.Import,
    top_body_ids: set[int],
) -> list[str]:
    """Return architectural violations found on one ``ast.Import`` statement."""
    errors: list[str] = []
    if id(node) not in top_body_ids:
        errors.append(f"{rel_posix}:{node.lineno}: non-top-level import")
    for alias in node.names:
        if not _is_module_allowed(domain, rel_posix, alias.name):
            errors.append(f"{rel_posix}:{node.lineno}: disallowed import '{alias.name}'")
    return errors


def _check_file_imports(path: Path, tree: ast.Module, raw_text: str) -> list[str]:
    """Validate unconditional top-level imports, Allowed-Import Matrix, and zero shims."""
    rel_posix = path.relative_to(SRC_DIR).as_posix()
    domain = rel_posix.split("/")[0]
    top_body_ids = {id(stmt) for stmt in tree.body}
    errors: list[str] = []

    if "# noqa: F401" in raw_text:
        errors.append(f"{rel_posix}: contains forbidden '# noqa: F401'")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            errors.extend(_check_import_node(domain, rel_posix, node, top_body_ids))
        elif isinstance(node, ast.ImportFrom):
            errors.extend(_check_import_from_node(domain, rel_posix, node, top_body_ids))
    return errors


def _contains_forbidden_dataclass_or_typeddict(tree: ast.Module) -> bool:
    """Return ``True`` if ``tree`` uses ``dataclass`` or ``TypedDict``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("dataclasses", "typing"):
            if any(alias.name in ("dataclass", "TypedDict") for alias in node.names):
                return True
        elif isinstance(node, ast.ClassDef):
            bases = {ast.unparse(b) for b in node.bases}
            decs = {ast.unparse(d).split("(")[0] for d in node.decorator_list}
            if "TypedDict" in bases or "dataclass" in decs or "dataclasses.dataclass" in decs:
                return True
    return False


def _check_models_layer_classes(rel_posix: str, tree: ast.Module) -> list[str]:
    """Ensure ``models/`` classes inherit from ``FrozenModel``, ``MutableModel``, or ``StrEnum``."""
    if rel_posix in ("core/models/base.py", "core/models/exceptions.py"):
        return []
    allowed_bases = {"FrozenModel", "MutableModel", "StrEnum"}
    errors: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = {ast.unparse(b) for b in node.bases}
            if not (bases & allowed_bases):
                errors.append(
                    f"{rel_posix}:{node.lineno}: model '{node.name}' bases {sorted(bases)} "
                    "must inherit FrozenModel, MutableModel, or StrEnum"
                )
    return errors


def _check_service_layer_file(domain: str, rel_posix: str, tree: ast.Module) -> list[str]:
    """Enforce class-only ``services/`` and zero unauthorized ``-> dict`` public returns."""
    errors: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            errors.append(
                f"{rel_posix}:{stmt.lineno}: standalone function '{stmt.name}' in services/"
            )
        elif isinstance(stmt, ast.ClassDef) and domain in DOMAIN_SERVICE_PACKAGES:
            errors.extend(_check_public_method_returns(rel_posix, stmt))
    return errors


def _check_public_method_returns(rel_posix: str, cls_node: ast.ClassDef) -> list[str]:
    """Check public methods on ``cls_node`` for unauthorized ``dict`` return types."""
    errors: list[str] = []
    for member in cls_node.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if member.name.startswith("_") or member.returns is None:
            continue
        ret_str = ast.unparse(member.returns)
        is_dict_ret = ret_str == "dict" or ret_str.startswith("dict[")
        if is_dict_ret and ret_str not in ALLOWED_PROTOCOL_DICT_RETURNS:
            errors.append(
                f"{rel_posix}:{member.lineno}: {cls_node.name}.{member.name} "
                f"returns unauthorized '{ret_str}'"
            )
    return errors


def _handler_raises_or_logs(handler: ast.ExceptHandler) -> bool:
    """Return ``True`` when ``handler`` contains an ``ast.Raise`` or ``*logger.<level>()`` call."""
    for stmt in handler.body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Raise):
                return True
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id.endswith("logger")
                and sub.func.attr in LOG_METHODS
            ):
                return True
    return False


def _is_broad_exception_type(exc_type: ast.expr | None) -> bool:
    """Return ``True`` if ``exc_type`` catches ``Exception``, ``BaseException``, or import shims."""
    if exc_type is None:
        return True
    unparsed = ast.unparse(exc_type)
    tokens = {tok.strip("() ") for tok in unparsed.split(",")}
    return bool(tokens & {"Exception", "BaseException", "ImportError"})


def _inspect_except_handler(
    rel_posix: str,
    node: ast.ExceptHandler,
    broad_catch_files: set[str],
) -> list[str]:
    """Inspect a single ``ast.ExceptHandler`` for silent swallowing or broad catch rules."""
    errors: list[str] = []
    if node.type is None:
        errors.append(f"{rel_posix}:{node.lineno}: bare 'except:' forbidden")
    if not _handler_raises_or_logs(node):
        errors.append(
            f"{rel_posix}:{node.lineno}: silent except handler neither raises nor logs"
        )
    if _is_broad_exception_type(node.type):
        if node.type is not None and "ImportError" in ast.unparse(node.type):
            errors.append(f"{rel_posix}:{node.lineno}: try/except ImportError")
        broad_catch_files.add(rel_posix)
    return errors


_CONTEXT_MANAGER_RETURN_TYPES: dict[str, str] = {
    "contextmanager": "Generator",
    "asynccontextmanager": "AsyncGenerator",
}


def _terminal_name(node: ast.expr) -> str | None:
    """Return the trailing identifier of a ``Name``, ``Attribute``, or ``Subscript`` node."""
    if isinstance(node, ast.Subscript):
        return _terminal_name(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _check_context_manager_returns(rel_posix: str, tree: ast.Module) -> list[str]:
    """Flag ``@contextmanager``/``@asynccontextmanager`` functions not returning a generator."""
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            expected = _CONTEXT_MANAGER_RETURN_TYPES.get(_terminal_name(decorator) or "")
            if expected is None:
                continue
            actual = _terminal_name(node.returns) if node.returns is not None else None
            if actual != expected:
                errors.append(
                    f"{rel_posix}:{node.lineno}: {node.name} must return {expected}[...], "
                    f"got {actual}"
                )
    return errors


class TestHexagonalArchitectureBoundaryGate:
    """AST static analysis gate enforcing Hexagonal package, import, model, and error rules."""

    def test_domain_package_layout_and_zero_service_suffixes(self) -> None:
        """``src/`` contains only the 10 Hexagonal packages with ``{services,models,utils}``."""
        assert not (SRC_DIR / "repo_context").exists()

        top_entries = {
            entry.name
            for entry in SRC_DIR.iterdir()
            if entry.name != "__pycache__"
            and not entry.name.startswith(".")
            and not entry.name.endswith(".egg-info")
        }
        assert top_entries == EXPECTED_DOMAINS

        for domain in EXPECTED_DOMAINS:
            pkg_dir = SRC_DIR / domain
            assert (pkg_dir / "__init__.py").is_file()
            child_dirs = {
                d.name
                for d in pkg_dir.iterdir()
                if d.is_dir() and d.name != "__pycache__"
            }
            expected_layers = (
                {"ports", "models", "utils"}
                if domain == "core"
                else {"services", "models", "utils"}
            )
            assert child_dirs == expected_layers, f"{domain} subpackages mismatch: {child_dirs}"

            py_files_at_root = {f.name for f in pkg_dir.glob("*.py")}
            expected_root_files = {"__init__.py", "main.py"} if domain == "cli" else {"__init__.py"}
            assert py_files_at_root == expected_root_files

        service_suffixed = [
            p.name for p in _iter_src_python_files() if p.name.endswith("_service.py")
        ]
        assert service_suffixed == []

    def test_allowed_import_matrix_and_unconditional_top_level_imports(self) -> None:
        """Every ``import`` in ``src/`` is top-level and obeys the Strict Allowed-Import Matrix."""
        violations: list[str] = []
        for path in _iter_src_python_files():
            raw_text = path.read_text(encoding="utf-8")
            tree = ast.parse(raw_text, filename=str(path))
            violations.extend(_check_file_imports(path, tree, raw_text))
        assert violations == []

    def test_pydantic_models_stateless_utils_and_typed_services(self) -> None:
        """Zero ``@dataclass``/``TypedDict``, pure ``utils/``, & typed class-only ``services/``."""
        violations: list[str] = []
        for path in _iter_src_python_files():
            rel_posix = path.relative_to(SRC_DIR).as_posix()
            parts = rel_posix.split("/")
            domain = parts[0]
            layer = parts[1] if len(parts) > 1 else ""
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

            if _contains_forbidden_dataclass_or_typeddict(tree):
                violations.append(f"{rel_posix}: uses forbidden dataclass or TypedDict")
            if layer == "models":
                violations.extend(_check_models_layer_classes(rel_posix, tree))
            elif layer == "utils":
                allowed = {"JsonText"} if rel_posix == "storage/utils/schema.py" else set()
                util_classes = [
                    n.name
                    for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name not in allowed
                ]
                if util_classes:
                    violations.append(
                        f"{rel_posix}: unexpected class(es) in utils/: {util_classes}"
                    )
            elif layer == "services":
                violations.extend(_check_service_layer_file(domain, rel_posix, tree))
        assert violations == []

    def test_zero_silent_errors_and_quarantined_broad_except_boundaries(self) -> None:
        """Zero ``contextlib.suppress``, zero silent ``except``, & quarantined broad catches."""
        violations: list[str] = []
        broad_catch_files: set[str] = set()

        for path in _iter_src_python_files():
            rel_posix = path.relative_to(SRC_DIR).as_posix()
            raw_text = path.read_text(encoding="utf-8")
            if "contextlib.suppress" in raw_text:
                violations.append(f"{rel_posix}: contains forbidden contextlib.suppress")

            tree = ast.parse(raw_text, filename=str(path))
            handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
            for handler in handlers:
                violations.extend(_inspect_except_handler(rel_posix, handler, broad_catch_files))

        unauthorized_broad = sorted(broad_catch_files - DOCUMENTED_BROAD_EXCEPT_FILES)
        assert unauthorized_broad == []
        assert violations == []

    def test_context_managers_annotate_generator_return_types(self) -> None:
        """``@contextmanager`` returns ``Generator[...]`` (``Iterator[...]`` is deprecated)."""
        tests_dir = PROJECT_ROOT / "tests"
        candidates = [
            *_iter_src_python_files(),
            *(p for p in sorted(tests_dir.rglob("*.py")) if "fixtures" not in p.parts),
        ]
        violations: list[str] = []
        for path in candidates:
            rel_posix = path.relative_to(PROJECT_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(_check_context_manager_returns(rel_posix, tree))
        assert violations == []
