"""Tests for context freshness metadata and live disk hash checks."""

from pathlib import Path

from repo_context.context.freshness import build_freshness
from repo_context.models import SymbolContext
from repo_context.parsing.hashing import sha256_file


def test_build_freshness_ast_only_and_edge_snapshot() -> None:
    """build_freshness reports ast_only by default and computes latest edge timestamp."""
    focus_node = {"id": "sym1", "last_indexed_at": "2026-01-01T00:00:00Z"}
    raw_edges = [
        {"from_id": "other", "to_id": "sym1", "kind": "calls", "last_indexed_at": "2026-01-01T01:00:00Z"},
        {"from_id": "sym1", "to_id": "dest", "kind": "calls", "last_indexed_at": "2026-01-02T03:00:00Z"},
    ]
    context = SymbolContext(
        focus_symbol={"id": "sym1"},
        incoming_edges=[raw_edges[0]],
        outgoing_edges=[raw_edges[1]],
    )

    freshness = build_freshness(context, focus_node, raw_edges=raw_edges)
    assert freshness["node_last_indexed_at"] == "2026-01-01T00:00:00Z"
    assert freshness["edge_snapshot_last_indexed_at"] == "2026-01-02T03:00:00Z"
    assert freshness["has_incoming_edges"] is True
    assert freshness["has_outgoing_edges"] is True
    assert freshness["context_source"] == "ast_only"
    assert freshness["is_fresh"] is True


def test_build_freshness_lsp_plus_ast() -> None:
    """build_freshness reports lsp+ast when LSP reference edges or summary are present."""
    focus_node = {"id": "sym1", "last_indexed_at": "2026-01-01T00:00:00Z"}
    context = SymbolContext(
        focus_symbol={"id": "sym1"},
        reference_summary={"available": True, "reference_source": "lsp"},
    )
    freshness = build_freshness(context, focus_node, raw_edges=[])
    assert freshness["context_source"] == "lsp+ast"
    assert freshness["edge_snapshot_last_indexed_at"] is None


def test_build_freshness_missing_timestamp_and_disk_staleness(tmp_path: Path) -> None:
    """build_freshness marks missing timestamp or mismatched disk hash as stale."""
    context = SymbolContext(focus_symbol={"id": "sym1"})
    missing_ts = build_freshness(context, {})
    assert missing_ts["node_last_indexed_at"] is None
    assert missing_ts["is_fresh"] is False
    assert missing_ts["stale_reason"] == "missing_last_indexed_at"

    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    file_rec = {"id": "file:1", "repo_id": "repo:1", "file_path": "mod.py", "content_hash": sha256_file(src)}
    focus = {"id": "sym1", "file_id": "file:1", "last_indexed_at": "2026-01-01T00:00:00Z"}

    fresh = build_freshness(context, focus, file_record=file_rec, repo_root=tmp_path)
    assert fresh["is_fresh"] is True
    assert fresh["stale_reason"] is None

    src.write_text("x = 2\n", encoding="utf-8")
    stale = build_freshness(context, focus, file_record=file_rec, repo_root=tmp_path)
    assert stale["is_fresh"] is False
    assert stale["stale_reason"] == "content_hash_mismatch"
