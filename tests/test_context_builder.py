"""SymbolContextBuilder, OutlineBuilder, and SourceReader tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from context import (
    ContextBuildOptions,
    OutlineBuilder,
    SnippetOptions,
    SourceReader,
    SymbolContextBuilder,
)
from core.models import (
    AST_DEPENDENCY_EDGE_KINDS,
    AppConfig,
    Edge,
    EdgeKind,
    IndexerError,
    PathSecurityError,
    SymbolKind,
    SymbolNotFoundError,
    ValidationError,
)
from parsing import AstExtractionPipeline, RepoScanner, SymbolLinker
from storage import SqlUnitOfWorkFactory


@pytest.fixture
def indexed_context_repo(tmp_path: Path) -> tuple[SqlUnitOfWorkFactory, Path, str]:
    """Index a multi-file repository in ``tmp_path`` and return ``(factory, root, repo_id)``."""
    repo_root = tmp_path / "ctx_repo"
    repo_root.mkdir()

    (repo_root / "helpers.py").write_text(
        '"""Shared helpers."""\n'
        "\n"
        "def helper(val: int) -> int:\n"
        '    """Double value."""\n'
        "    return val * 2\n",
        encoding="utf-8",
    )
    (repo_root / "service.py").write_text(
        '"""Service module docstring."""\n'
        "\n"
        "from helpers import helper\n"
        "\n"
        "def dec(fn):\n"
        "    return fn\n"
        "\n"
        "class Service:\n"
        '    """Main service class."""\n'
        "\n"
        "    @dec\n"
        "    def run(self, x: int) -> int:\n"
        '        """Run step."""\n'
        "        def _local(v: int) -> int:\n"
        "            return v + 1\n"
        "        return helper(_local(x))\n"
        "\n"
        "    def _private_method(self) -> None:\n"
        "        pass\n"
        "\n"
        "def outer_fn() -> int:\n"
        '    """Outer function."""\n'
        "    def inner_fn() -> int:\n"
        "        return 1\n"
        "    return inner_fn()\n",
        encoding="utf-8",
    )

    cfg = AppConfig()
    scan_report = RepoScanner(cfg).scan_repository(repo_root)
    pipeline = AstExtractionPipeline(cfg)
    linker = SymbolLinker()
    factory = SqlUnitOfWorkFactory(tmp_path / "context.db")

    with factory.open(initialize=True) as uow:
        uow.repos.upsert(scan_report.repo)
        for rec in scan_report.files:
            ext = pipeline.extract_file(repo_root, rec)
            uow.replace_file_graph(rec, ext.nodes, ext.edges)
        nodes = uow.nodes.list_for_repo(scan_report.repo.id)
        cand_edges = uow.edges.list_for_repo(scan_report.repo.id, kinds=AST_DEPENDENCY_EDGE_KINDS)
        link_res = linker.resolve(scan_report.repo.id, scan_report.repo.name, nodes, cand_edges)
        uow.edges.update_resolved_targets(link_res.updates)

    return factory, repo_root, scan_report.repo.id


def _find_node_id(
    factory: SqlUnitOfWorkFactory,
    repo_id: str,
    name: str,
    kind: SymbolKind,
) -> str:
    """Resolve a symbol ID by short name and kind inside ``repo_id``."""
    with factory.open(read_only=True) as uow:
        matches = [n for n in uow.nodes.find_by_name(repo_id, name, kind=kind) if n.name == name]
    assert matches, f"Node not found: {name} ({kind})"
    return matches[0].id


def test_build_symbol_context_separates_structural_and_lexical_no_duplicates(
    indexed_context_repo: tuple[SqlUnitOfWorkFactory, Path, str],
) -> None:
    """Class context has structural children only; method has no bogus lexical_parent."""
    factory, _, repo_id = indexed_context_repo
    builder = SymbolContextBuilder()

    cls_id = _find_node_id(factory, repo_id, "Service", SymbolKind.CLASS)
    run_id = _find_node_id(factory, repo_id, "run", SymbolKind.METHOD)
    inner_id = _find_node_id(factory, repo_id, "inner_fn", SymbolKind.LOCAL_FUNCTION)

    with factory.open(read_only=True) as uow:
        cls_ctx = builder.build_context(
            uow,
            cls_id,
            ContextBuildOptions(edge_limit=20, include_source=True),
        )
        assert cls_ctx.structural_parent is not None
        assert cls_ctx.structural_parent.kind == SymbolKind.MODULE
        assert cls_ctx.lexical_parent is None
        assert [c.name for c in cls_ctx.structural_children] == ["run", "_private_method"]
        assert cls_ctx.lexical_children == []
        assert cls_ctx.focus_symbol.source is not None
        assert cls_ctx.focus_symbol.raw_source is not None
        assert cls_ctx.freshness.is_fresh is True
        assert cls_ctx.freshness.context_source == "ast_only"

        run_ctx = builder.build_context(uow, run_id)
        assert run_ctx.structural_parent is not None
        assert run_ctx.structural_parent.id == cls_id
        # Class methods are structural class members, NOT lexical declarations
        assert run_ctx.lexical_parent is None
        assert [c.name for c in run_ctx.lexical_children] == ["_local"]
        assert run_ctx.structural_summary.is_nested_declaration is False
        assert run_ctx.structural_summary.relationship_counts.callees >= 2

        inner_ctx = builder.build_context(uow, inner_id)
        assert inner_ctx.lexical_parent is not None
        assert inner_ctx.lexical_parent.name == "outer_fn"
        assert inner_ctx.structural_summary.is_local_declaration is True
        assert inner_ctx.structural_summary.is_nested_declaration is True


def test_build_symbol_context_edge_limit_and_missing(
    indexed_context_repo: tuple[SqlUnitOfWorkFactory, Path, str],
) -> None:
    """ContextBuildOptions rejects negative limits and build_context raises SymbolNotFoundError."""
    factory, _, _ = indexed_context_repo
    builder = SymbolContextBuilder()

    with pytest.raises(PydanticValidationError):
        ContextBuildOptions(edge_limit=-1)

    with pytest.raises(SymbolNotFoundError), factory.open(read_only=True) as uow:
        builder.build_context(uow, "node:nonexistent")


def test_build_file_outline_structure_and_filters(
    indexed_context_repo: tuple[SqlUnitOfWorkFactory, Path, str],
) -> None:
    """OutlineBuilder returns start_line-ordered symbols and deduplicated imports."""
    factory, _, repo_id = indexed_context_repo
    outline_builder = OutlineBuilder()

    with factory.open(read_only=True) as uow:
        outline = outline_builder.build_outline(uow, repo_id, "service.py")
        assert outline.file_path == "service.py"
        assert outline.module_path == "service"
        assert outline.is_fresh is True
        assert outline.doc == "Service module docstring."
        assert outline.imports == ["helpers.helper"]
        assert [s.name for s in outline.symbols] == ["dec", "Service", "outer_fn"]

        svc_entry = outline.symbols[1]
        assert [c.name for c in svc_entry.children] == ["run", "_private_method"]

        # include_private=False drops _private_method and _local
        public_outline = outline_builder.build_outline(
            uow, repo_id, "service.py", include_private=False
        )
        public_svc = public_outline.symbols[1]
        assert [c.name for c in public_svc.children] == ["run"]
        assert public_svc.children[0].children == []

        # max_depth=1 omits method children
        shallow_outline = outline_builder.build_outline(uow, repo_id, "service.py", max_depth=1)
        assert shallow_outline.symbols[1].children == []

        with pytest.raises(ValidationError):
            outline_builder.build_outline(uow, repo_id, "service.py", max_depth=-1)

        with pytest.raises(IndexerError) as missing_exc:
            outline_builder.build_outline(uow, repo_id, "missing.py")
        assert missing_exc.value.error_code == "file_not_found"


def test_read_symbol_source_pagination_batch_staleness_and_path_security(
    indexed_context_repo: tuple[SqlUnitOfWorkFactory, Path, str],
    tmp_path: Path,
) -> None:
    """SourceReader slices lines, paginates, batches, detects staleness, and blocks escapes."""
    factory, repo_root, repo_id = indexed_context_repo
    reader = SourceReader()
    run_id = _find_node_id(factory, repo_id, "run", SymbolKind.METHOD)
    helper_id = _find_node_id(factory, repo_id, "helper", SymbolKind.FUNCTION)

    with factory.open(read_only=True) as uow:
        page1 = reader.read_symbol_snippet(uow, run_id, SnippetOptions(max_lines=3, offset=0))
        assert page1.is_fresh is True
        assert page1.truncated is True
        assert page1.total_lines == 6  # lines 11..16 inclusive of @dec
        assert page1.returned_lines == 3
        assert page1.next_offset == 3
        assert "11:     @dec" in page1.numbered_source
        assert page1.raw_source.startswith("    @dec")
        assert "11:" not in page1.raw_source

        page2 = reader.read_symbol_snippet(
            uow, run_id, SnippetOptions(max_lines=10, offset=page1.next_offset)
        )
        assert page2.truncated is False
        assert page2.next_offset is None
        assert "16:         return helper(_local(x))" in page2.numbered_source

        batched = reader.read_symbol_snippets(
            uow, [run_id, helper_id], SnippetOptions(padding_lines=1)
        )
        assert [s.symbol_id for s in batched] == [run_id, helper_id]
        assert batched[0].window_start_line == page1.start_line - 1

    # Path security and line bound validation on read_file_snippet
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    (repo_root / "escape.py").symlink_to(outside)

    with pytest.raises(PathSecurityError):
        reader.read_file_snippet(repo_root, "escape.py", 1, 1)
    with pytest.raises(PathSecurityError):
        reader.read_file_snippet(repo_root, "../secret.py", 1, 1)
    with pytest.raises(ValidationError):
        reader.read_file_snippet(repo_root, "service.py", 0, 1)

    # Disk staleness detection
    service_file = repo_root / "service.py"
    service_file.write_text(
        service_file.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8"
    )
    with factory.open(read_only=True) as uow:
        stale_src = reader.read_symbol_snippet(uow, run_id)
        assert stale_src.is_fresh is False
        assert stale_src.stale_reason == "content_hash_mismatch"


def test_build_symbol_context_filters_unresolved_placeholders(
    indexed_context_repo: tuple[SqlUnitOfWorkFactory, Path, str],
) -> None:
    """Unresolved placeholder edges stay hidden from context edges while flagged in confidence."""
    factory, _, repo_id = indexed_context_repo
    helper_id = _find_node_id(factory, repo_id, "helper", SymbolKind.FUNCTION)
    builder = SymbolContextBuilder()

    with factory.open() as uow:
        helper_node = uow.nodes.get_by_id(helper_id)
        assert helper_node is not None
        uow.edges.upsert(
            Edge(
                id="edge:unres:ext",
                repo_id=repo_id,
                kind=EdgeKind.CALLS,
                from_id=helper_id,
                to_id="unresolved_call:ext_fn",
                confidence=0.7,
                evidence_file_id=helper_node.file_id,
                evidence_uri=helper_node.uri,
            )
        )
        ctx = builder.build_context(uow, helper_id)

    assert all(not e.to_id.startswith("unresolved_call:") for e in ctx.outgoing_edges)
    assert ctx.structural_summary.relationship_counts.callees == 0
    assert ctx.confidence.contains_placeholder_targets is True
