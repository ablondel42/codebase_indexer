"""Tests for LSP client, session pool, UTF-16 mapping, and reference enrichment (v0.2.0)."""

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection

import repo_context.lsp.client as lsp_client_mod
from repo_context.config import AppConfig
from repo_context.constants import EDGE_KIND_REFERENCES
from repo_context.exceptions import LspError, PathSecurityError
from repo_context.graph.references import build_reference_stats, list_symbol_reference_edges
from repo_context.lsp import LspSessionPool, PyrightLspClient
from repo_context.lsp.mapper import (
    codepoint_offset_to_utf16,
    find_module_node_for_file,
    normalize_location,
    pick_smallest_containing_symbol,
    position_codepoint_to_utf16,
    position_utf16_to_codepoint,
    range_contains,
    range_utf16_to_codepoint,
    utf16_offset_to_codepoint,
)
from repo_context.lsp.references import (
    build_reference_edge,
    enrich_references_for_symbol,
    replace_reference_edges_for_target,
)
from repo_context.lsp.resolver import get_reference_query_position, resolve_file_by_uri
from repo_context.models.file import FileRecord
from repo_context.models.repo import RepoRecord
from repo_context.parsing.hashing import sha256_file
from repo_context.parsing.linker import resolve_repo_edges
from repo_context.parsing.pipeline import extract_file_graph
from repo_context.parsing.scanner import scan_repository
from repo_context.storage import (
    close_connection,
    files_table,
    get_connection,
    initialize_database,
    nodes_table,
    replace_file_graph,
    upsert_files,
    upsert_nodes,
    upsert_repo,
)


@pytest.fixture
def lsp_db(tmp_path: Path) -> Iterator[tuple[Connection, Path]]:
    """Yield an initialized v2 SQLite connection and temp directory."""
    conn = get_connection(tmp_path / "lsp.db")
    try:
        initialize_database(conn)
        yield conn, tmp_path
    finally:
        close_connection(conn)


def _write_fake_lsp_server(path: Path, body: str) -> Path:
    """Write a self-contained JSON-RPC stdio server script for subprocess testing."""
    preamble = (
        "import json, sys\n"
        "def read_msg():\n"
        "    headers = {}\n"
        "    while True:\n"
        "        line = sys.stdin.buffer.readline()\n"
        "        if not line:\n"
        "            return None\n"
        "        if line in (b'\\r\\n', b'\\n'):\n"
        "            break\n"
        "        k, v = line.decode('ascii').split(':', 1)\n"
        "        headers[k.strip().lower()] = v.strip()\n"
        "    length = int(headers['content-length'])\n"
        "    return json.loads(sys.stdin.buffer.read(length).decode('utf-8'))\n"
        "def send_msg(obj):\n"
        "    data = json.dumps(obj).encode('utf-8')\n"
        "    hdr = f'Content-Length: {len(data)}\\r\\n\\r\\n'.encode('ascii')\n"
        "    sys.stdout.buffer.write(hdr + data)\n"
        "    sys.stdout.buffer.flush()\n"
    )
    path.write_text(preamble + body, encoding="utf-8")
    return path


class TestMapperAndUtf16Conversions:
    """Tests for UTF-16 <-> Python codepoint mapping and range containment."""

    def test_utf16_codepoint_roundtrip_with_supplementary_emoji(self) -> None:
        line = "msg = '😀'; target()"
        cp_target = line.index("target")
        utf16_target = codepoint_offset_to_utf16(line, cp_target)

        assert cp_target == 11
        # '😀' is 1 Python codepoint but 2 UTF-16 code units
        assert utf16_target == 12
        assert utf16_offset_to_codepoint(line, utf16_target) == cp_target

        pos = position_codepoint_to_utf16([line], {"line": 0, "character": cp_target})
        assert pos == {"line": 0, "character": 12}
        assert position_utf16_to_codepoint([line], pos) == {"line": 0, "character": cp_target}

        rng = range_utf16_to_codepoint(
            [line],
            {"start": {"line": 0, "character": 12}, "end": {"line": 0, "character": 18}},
        )
        assert line[rng["start"]["character"] : rng["end"]["character"]] == "target"

    def test_range_contains_and_pick_smallest_prefers_non_module_on_tie(self) -> None:
        outer = {"start": {"line": 0, "character": 0}, "end": {"line": 10, "character": 0}}
        inner = {"start": {"line": 5, "character": 0}, "end": {"line": 6, "character": 0}}
        assert range_contains(outer, inner) is True
        assert range_contains(inner, outer) is False

        symbols = [
            {"id": "sym:mod", "kind": "module", "range_json": outer},
            {"id": "sym:fn", "kind": "function", "range_json": outer},
        ]
        picked = pick_smallest_containing_symbol(symbols, outer)
        assert picked is not None and picked["id"] == "sym:fn"
        mod_node = find_module_node_for_file(symbols)
        assert mod_node is not None and mod_node["id"] == "sym:mod"

    def test_normalize_location_location_and_location_link(self) -> None:
        loc = {
            "uri": "file:///a.py",
            "range": {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 5}},
        }
        assert normalize_location(loc) == loc

        link = {
            "targetUri": "file:///b.py",
            "targetSelectionRange": {
                "start": {"line": 3, "character": 4},
                "end": {"line": 3, "character": 9},
            },
        }
        assert normalize_location(link)["uri"] == "file:///b.py"
        assert normalize_location(link)["range"]["start"] == {"line": 3, "character": 4}


class TestResolverAndPosition:
    """Tests for ``get_reference_query_position`` and ``resolve_file_by_uri``."""

    def test_query_position_preference_and_missing(self) -> None:
        sym = {
            "id": "sym:1",
            "selection_range_json": {
                "start": {"line": 2, "character": 4},
                "end": {"line": 2, "character": 8},
            },
            "range_json": {
                "start": {"line": 1, "character": 0},
                "end": {"line": 5, "character": 0},
            },
        }
        assert get_reference_query_position(sym) == {"line": 2, "character": 4}
        sym["selection_range_json"] = None
        assert get_reference_query_position(sym) == {"line": 1, "character": 0}
        sym["range_json"] = None
        with pytest.raises(ValueError, match="no stored range"):
            get_reference_query_position(sym)

    def test_resolve_file_by_uri_percent_decoding_and_symlinked_root(
        self,
        lsp_db: tuple[Connection, Path],
    ) -> None:
        conn, tmp_path = lsp_db
        real_root = tmp_path / "real_repo"
        real_root.mkdir()
        target = real_root / "my file.py"
        target.write_text("x = 1\n", encoding="utf-8")
        symlink_root = tmp_path / "link_repo"
        symlink_root.symlink_to(real_root)

        repo_id = "repo:real_repo:12345678"
        upsert_repo(
            conn,
            RepoRecord(
                id=repo_id,
                root_path=str(real_root),
                name="real_repo",
                created_at="2026-01-01T00:00:00Z",
            ),
        )
        upsert_files(
            conn,
            [
                FileRecord(
                    id=f"file:{repo_id}:my file.py",
                    repo_id=repo_id,
                    file_path="my file.py",
                    uri=target.resolve().as_uri(),
                    module_path="my file",
                    content_hash=sha256_file(target),
                    size_bytes=target.stat().st_size,
                    last_modified_at="2026-01-01T00:00:00Z",
                )
            ],
        )
        conn.commit()

        # Percent-encoded space and symlinked root URI both resolve to the file row
        via_symlink_uri = (symlink_root / "my file.py").as_uri()
        resolved = resolve_file_by_uri(conn, via_symlink_uri, repo_id=repo_id)
        assert resolved is not None
        assert resolved["id"] == f"file:{repo_id}:my file.py"


class TestReferenceEdgePersistenceAndStats:
    """Tests for transactional edge replacement, single JSON encoding, and AST fallback."""

    def test_replace_reference_edges_single_json_and_savepoint_safety(
        self,
        lsp_db: tuple[Connection, Path],
    ) -> None:
        conn, tmp_path = lsp_db
        repo_id = "repo:test:12345678"
        upsert_repo(
            conn,
            RepoRecord(
                id=repo_id,
                root_path=str(tmp_path),
                name="test",
                created_at="2026-01-01T00:00:00Z",
            ),
        )
        file_rec = FileRecord(
            id=f"file:{repo_id}:a.py",
            repo_id=repo_id,
            file_path="a.py",
            uri="file:///tmp/a.py",
            module_path="a",
            content_hash="sha256:a",
            size_bytes=10,
            last_modified_at="2026-01-01T00:00:00Z",
        )
        upsert_files(conn, [file_rec])
        target_id = f"sym:{repo_id}:function:a.target"
        caller_id = f"sym:{repo_id}:function:a.caller"
        upsert_nodes(
            conn,
            [
                {
                    "id": target_id,
                    "repo_id": repo_id,
                    "file_id": file_rec.id,
                    "kind": "function",
                    "name": "target",
                    "qualified_name": "a.target",
                    "uri": file_rec.uri,
                },
                {
                    "id": caller_id,
                    "repo_id": repo_id,
                    "file_id": file_rec.id,
                    "kind": "function",
                    "name": "caller",
                    "qualified_name": "a.caller",
                    "uri": file_rec.uri,
                },
            ],
        )
        conn.commit()

        # Prior to LSP refresh, build_reference_stats uses AST fallback
        pre_stats = build_reference_stats(conn, target_id)
        assert pre_stats["available"] is False
        assert pre_stats["reference_source"] == "ast"

        edge = build_reference_edge(
            repo_id=repo_id,
            from_id=caller_id,
            to_id=target_id,
            confidence=0.9,
            evidence_file_id=file_rec.id,
            evidence_uri=file_rec.uri,
            evidence_range_json={
                "start": {"line": 2, "character": 4},
                "end": {"line": 2, "character": 10},
            },
            payload_json={"mapping_mode": "exact_symbol"},
        )
        refresh_meta = {
            "target_symbol_id": target_id,
            "available": True,
            "last_refreshed_at": "2026-01-02T00:00:00Z",
            "refresh_status": "ok",
            "error_code": None,
        }

        # Run inside an active transaction to exercise SAVEPOINT path
        conn.execute(select(1))
        replace_reference_edges_for_target(conn, target_id, [edge], refresh_meta)
        conn.commit()

        raw_row = (
            conn.exec_driver_sql(
                "SELECT payload_json, evidence_range_json FROM edges WHERE id = ?",
                (edge["id"],),
            )
            .mappings()
            .one()
        )
        assert isinstance(json.loads(raw_row["payload_json"]), dict)
        assert isinstance(json.loads(raw_row["evidence_range_json"]), dict)

        post_stats = build_reference_stats(conn, target_id)
        assert post_stats["available"] is True
        assert post_stats["reference_source"] == "lsp"
        assert post_stats["reference_count"] == 1
        assert len(list_symbol_reference_edges(conn, target_id, direction="incoming")) == 1


class TestPyrightLspClientSubprocessProtocol:
    """Mocked subprocess protocol tests for ``PyrightLspClient`` and ``LspSessionPool``."""

    def test_missing_executable_raises_lsp_error(self, tmp_path: Path) -> None:
        client = PyrightLspClient(["definitely_missing_lsp_binary_xyz"])
        with pytest.raises(LspError, match="not found"):
            client.start(tmp_path)

    def test_stderr_flood_and_server_request_id_collision_handling(self, tmp_path: Path) -> None:
        """Handles >200 stderr lines without deadlock and dispatches colliding server IDs."""
        log_file = tmp_path / "server_received.json"
        server_script = _write_fake_lsp_server(
            tmp_path / "fake_lsp.py",
            f"""
received_replies = []
while True:
    msg = read_msg()
    if msg is None:
        break
    method = msg.get("method")
    if method == "initialize":
        for i in range(350):
            sys.stderr.write(f"stderr flood line {{i}}\\n")
        sys.stderr.flush()
        # Server sends colliding IDs (1 and 2) BEFORE responding to client initialize (id=1)
        send_msg({{"jsonrpc": "2.0", "id": 1, "method": "workspace/configuration",
                  "params": {{"items": [{{"section": "python"}}, {{"section": "pyright"}}]}}}} )
        reply1 = read_msg()
        received_replies.append(reply1)
        send_msg({{"jsonrpc": "2.0", "id": 2,
                  "method": "client/registerCapability", "params": {{}}}})
        reply2 = read_msg()
        received_replies.append(reply2)
        send_msg({{"jsonrpc": "2.0", "id": msg["id"], "result": {{"capabilities": {{}}}}}})
    elif method == "textDocument/references":
        send_msg({{"jsonrpc": "2.0", "id": msg["id"], "result": []}})
    elif method == "shutdown":
        with open({str(log_file)!r}, "w") as f:
            json.dump(received_replies, f)
        send_msg({{"jsonrpc": "2.0", "id": msg["id"], "result": None}})
    elif method == "exit":
        break
""",
        )

        with PyrightLspClient([sys.executable, "-u", str(server_script)], timeout=5.0) as client:
            client.start(tmp_path)
            assert client.is_running is True
            assert len(client.stderr_lines) == 200
            assert "stderr flood line 349" in client.stderr_lines[-1]
            refs = client.find_references((tmp_path / "a.py").as_uri(), 0, 0)
            assert refs == []

        replies = json.loads(log_file.read_text(encoding="utf-8"))
        assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": [None, None]}
        assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": None}

    def test_content_length_frame_cap_rejected(self, tmp_path: Path) -> None:
        """Oversized Content-Length frames above max_payload_bytes raise LspError."""
        server_script = _write_fake_lsp_server(
            tmp_path / "oversized_lsp.py",
            """
while True:
    msg = read_msg()
    if msg is None:
        break
    if msg.get("method") == "initialize":
        send_msg({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}})
    elif msg.get("method") == "textDocument/references":
        big = "x" * 2048
        send_msg({"jsonrpc": "2.0", "id": msg["id"], "result": [{"uri": big}]})
        break
""",
        )

        with PyrightLspClient(
            [sys.executable, "-u", str(server_script)],
            timeout=5.0,
            max_payload_bytes=512,
        ) as client:
            client.start(tmp_path)
            with pytest.raises(LspError, match="max_lsp_payload_bytes"):
                client.find_references((tmp_path / "a.py").as_uri(), 0, 0)

    def test_timeout_and_zombie_free_kill_close(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hung request times out with LspError and close() kills/reaps SIGTERM-ignoring server."""
        monkeypatch.setattr(lsp_client_mod, "SHUTDOWN_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(lsp_client_mod, "TERMINATE_WAIT_SECONDS", 0.2)

        server_script = _write_fake_lsp_server(
            tmp_path / "hung_lsp.py",
            """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    msg = read_msg()
    if msg is None:
        break
    if msg.get("method") == "initialize":
        send_msg({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}})
    elif msg.get("method") == "textDocument/references":
        time.sleep(10)
    elif msg.get("method") == "shutdown":
        time.sleep(10)
""",
        )

        client = PyrightLspClient([sys.executable, "-u", str(server_script)], timeout=0.3)
        client.start(tmp_path)
        assert client.proc is not None
        pid = client.proc.pid
        with pytest.raises(LspError, match="timed out"):
            client.find_references((tmp_path / "a.py").as_uri(), 0, 0)

        client.close()
        assert client.proc is None
        assert client.is_running is False
        # Process was reaped by wait(); waitpid must raise ChildProcessError
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_lsp_session_pool_caches_by_resolved_root_and_enriches_utf16_references(
        self,
        lsp_db: tuple[Connection, Path],
    ) -> None:
        """LspSessionPool caches sessions, tracks didOpen/didChange, and enriches UTF-16 refs."""
        conn, tmp_path = lsp_db
        repo_root = tmp_path / "real_project"
        repo_root.mkdir()
        target_py = repo_root / "defs.py"
        caller_py = repo_root / "caller.py"
        target_py.write_text('TAG = "😀"\ndef target():\n    return 1\n', encoding="utf-8")
        caller_py.write_text(
            'from defs import target\n\ndef run():\n    x = "😀"; return target()\n',
            encoding="utf-8",
        )

        # Index repo into DB
        cfg = AppConfig()
        repo_rec, files = scan_repository(repo_root, cfg)
        upsert_repo(conn, repo_rec)
        upsert_files(conn, files)
        for f in files:
            nodes, edges, _ = extract_file_graph(repo_rec.id, f, repo_root, config=cfg)
            replace_file_graph(conn, f.id, nodes, edges)
        resolve_repo_edges(conn, repo_rec.id)
        conn.commit()

        target_row = (
            conn.execute(
                select(nodes_table, files_table.c.file_path)
                .select_from(
                    nodes_table.join(files_table, files_table.c.id == nodes_table.c.file_id)
                )
                .where(nodes_table.c.name == "target", nodes_table.c.kind == "function")
            )
            .mappings()
            .one()
        )
        target_sym = dict(target_row)

        caller_line = caller_py.read_text(encoding="utf-8").splitlines()[3]
        cp_start = caller_line.index("target")
        utf16_start = codepoint_offset_to_utf16(caller_line, cp_start)
        utf16_end = utf16_start + len("target")

        starts_file = tmp_path / "starts.txt"
        server_script = _write_fake_lsp_server(
            tmp_path / "enrich_lsp.py",
            f"""
with open({str(starts_file)!r}, "a") as sf:
    sf.write("start\\n")
while True:
    msg = read_msg()
    if msg is None:
        break
    m = msg.get("method")
    if m == "initialize":
        send_msg({{"jsonrpc": "2.0", "id": msg["id"], "result": {{"capabilities": {{}}}}}})
    elif m == "textDocument/references":
        send_msg({{"jsonrpc": "2.0", "id": msg["id"], "result": [{{
            "uri": {caller_py.resolve().as_uri()!r},
            "range": {{"start": {{"line": 3, "character": {utf16_start}}},
                      "end": {{"line": 3, "character": {utf16_end}}}}}
        }}]}})
    elif m == "shutdown":
        send_msg({{"jsonrpc": "2.0", "id": msg["id"], "result": None}})
    elif m == "exit":
        break
""",
        )

        symlink_root = tmp_path / "symlink_project"
        symlink_root.symlink_to(repo_root)

        with LspSessionPool([sys.executable, "-u", str(server_script)]) as pool:
            c1 = pool.get_or_start(repo_root)
            c2 = pool.get_or_start(symlink_root)
            assert c1 is c2
            assert len(starts_file.read_text(encoding="utf-8").splitlines()) == 1

            stats = enrich_references_for_symbol(conn, c1, target_sym)
            assert stats["available"] is True
            assert stats["reference_count"] == 1

            # Check UTF-16 -> codepoint conversion stored exact codepoint column
            stored_edges = list_symbol_reference_edges(
                conn, target_sym["id"], direction="incoming", kinds=[EDGE_KIND_REFERENCES]
            )
            assert len(stored_edges) == 1
            assert stored_edges[0]["evidence_range_json"]["start"]["character"] == cp_start

            # enrich_references_for_symbol opened target_py at version 1; caller_py is unseen
            assert c1._document_versions[target_py.resolve().as_uri()] == 1
            caller_uri = caller_py.resolve().as_uri()
            pool.notify_did_open_or_change(repo_root, caller_uri, "x = 1\n")
            assert c1._document_versions[caller_uri] == 1
            pool.notify_did_open_or_change(repo_root, caller_uri, "x = 2\n")
            assert c1._document_versions[caller_uri] == 2

            outside = tmp_path / "outside.py"
            outside.write_text("SECRET = 1\n", encoding="utf-8")
            (repo_root / "escape.py").symlink_to(outside)
            with pytest.raises(PathSecurityError):
                pool.notify_did_open_or_change(repo_root, (repo_root / "escape.py").as_uri())

            bad_sym = {**target_sym, "file_path": "escape.py"}
            with pytest.raises(PathSecurityError):
                enrich_references_for_symbol(conn, c1, bad_sym)
