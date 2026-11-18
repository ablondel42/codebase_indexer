"""Context builder, file outline, and source reader tests (v0.2.0)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection

from repo_context.config import AppConfig
from repo_context.context import (
    build_file_outline,
    build_symbol_context,
    read_file_snippet,
    read_symbol_source,
)
from repo_context.exceptions import PathSecurityError, RepoContextError, ValidationError
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


@pytest.fixture
def indexed_context_repo(tmp_path: Path) -> Iterator[tuple[Connection, Path, str]]:
    """Index a realistic multi-file repository in ``tmp_path`` and yield ``(conn, root, repo_id)``."""
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

    conn = get_connection(tmp_path / "context.db")
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
        yield conn, repo_root, repo.id
    finally:
        close_connection(conn)


def _find_node_id(conn: Connection, name: str, kind: str | None = None) -> str:
    stmt = select(nodes_table.c.id).where(nodes_table.c.name == name)
    if kind is not None:
        stmt = stmt.where(nodes_table.c.kind == kind)
    node_id = conn.execute(stmt.limit(1)).scalar_one_or_none()
    assert node_id is not None, f"Node not found: {name} ({kind})"
    return str(node_id)


def test_build_symbol_context_separates_structural_and_lexical_no_duplicates(
    indexed_context_repo: tuple[Connection, Path, str],
) -> None:
    """Class context has structural children only; method has no bogus lexical_parent."""
    conn, _, _ = indexed_context_repo

    cls_id = _find_node_id(conn, "Service", "class")
    cls_ctx = build_symbol_context(conn, cls_id, edge_limit=20, include_source=True)
    assert cls_ctx is not None
    assert cls_ctx.structural_parent["kind"] == "module"
    assert cls_ctx.lexical_parent is None
    assert [c["name"] for c in cls_ctx.structural_children] == ["run", "_private_method"]
    assert cls_ctx.lexical_children == []
    assert "source" in cls_ctx.focus_symbol
    assert cls_ctx.freshness["is_fresh"] is True
    assert cls_ctx.freshness["context_source"] == "ast_only"

    run_id = _find_node_id(conn, "run", "method")
    run_ctx = build_symbol_context(conn, run_id)
    assert run_ctx is not None
    assert run_ctx.structural_parent["id"] == cls_id
    # Class methods are structural class members, NOT lexical declarations
    assert run_ctx.lexical_parent is None
    assert [c["name"] for c in run_ctx.lexical_children] == ["_local"]
    assert run_ctx.structural_summary["is_nested_declaration"] is False
    assert run_ctx.structural_summary["relationship_counts"]["callees"] >= 2

    inner_id = _find_node_id(conn, "inner_fn", "local_function")
    inner_ctx = build_symbol_context(conn, inner_id)
    assert inner_ctx is not None
    assert inner_ctx.lexical_parent["name"] == "outer_fn"
    assert inner_ctx.structural_summary["is_local_declaration"] is True
    assert inner_ctx.structural_summary["is_nested_declaration"] is True


def test_build_symbol_context_edge_limit_and_missing(
    indexed_context_repo: tuple[Connection, Path, str],
) -> None:
    """build_symbol_context respects edge_limit, validates negative limits, and returns None when missing."""
    conn, _, _ = indexed_context_repo
    assert build_symbol_context(conn, "sym:nonexistent") is None
    with pytest.raises(ValidationError):
        build_symbol_context(conn, _find_node_id(conn, "Service", "class"), edge_limit=-1)


def test_build_file_outline_structure_and_filters(
    indexed_context_repo: tuple[Connection, Path, str],
) -> None:
    """build_file_outline returns hierarchical start_line-ordered symbols and deduplicated imports."""
    conn, _, repo_id = indexed_context_repo

    outline = build_file_outline(conn, repo_id, "service.py")
    assert outline["file_path"] == "service.py"
    assert outline["module_path"] == "service"
    assert outline["is_fresh"] is True
    assert outline["doc"] == "Service module docstring."
    assert outline["imports"] == ["helpers.helper"]
    assert [s["name"] for s in outline["symbols"]] == ["dec", "Service", "outer_fn"]

    svc_entry = outline["symbols"][1]
    assert [c["name"] for c in svc_entry["children"]] == ["run", "_private_method"]

    # include_private=False drops _private_method and _local
    public_outline = build_file_outline(conn, repo_id, "service.py", include_private=False)
    public_svc = public_outline["symbols"][1]
    assert [c["name"] for c in public_svc["children"]] == ["run"]
    assert public_svc["children"][0]["children"] == []

    # max_depth=1 omits method children
    shallow_outline = build_file_outline(conn, repo_id, "service.py", max_depth=1)
    assert shallow_outline["symbols"][1]["children"] == []

    with pytest.raises(RepoContextError) as missing_exc:
        build_file_outline(conn, repo_id, "missing.py")
    assert missing_exc.value.error_code == "file_not_found"


def test_read_symbol_source_pagination_staleness_and_path_security(
    indexed_context_repo: tuple[Connection, Path, str],
    tmp_path: Path,
) -> None:
    """read_symbol_source slices 1-based lines, paginates, detects staleness, and rejects escapes."""
    conn, repo_root, _ = indexed_context_repo
    run_id = _find_node_id(conn, "run", "method")

    page1 = read_symbol_source(conn, run_id, max_lines=3, offset=0)
    assert page1["is_fresh"] is True
    assert page1["truncated"] is True
    assert page1["total_lines"] == 6  # lines 11..16 inclusive of @dec
    assert page1["returned_lines"] == 3
    assert page1["next_offset"] == 3
    assert "11:     @dec" in page1["source"]

    page2 = read_symbol_source(conn, run_id, max_lines=10, offset=page1["next_offset"])
    assert page2["truncated"] is False
    assert page2["next_offset"] is None
    assert "16:         return helper(_local(x))" in page2["source"]

    # Path security: symlink / traversal rejected by read_file_snippet
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    (repo_root / "escape.py").symlink_to(outside)

    with pytest.raises(PathSecurityError):
        read_file_snippet(repo_root, "escape.py", 1, 1)
    with pytest.raises(PathSecurityError):
        read_file_snippet(repo_root, "../secret.py", 1, 1)

    # Disk staleness detection
    service_file = repo_root / "service.py"
    service_file.write_text(service_file.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    stale_src = read_symbol_source(conn, run_id)
    assert stale_src["is_fresh"] is False
    assert stale_src["stale_reason"] == "content_hash_mismatch"
