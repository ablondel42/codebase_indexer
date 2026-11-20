"""Tests for LSP client, session pool, UTF-16 mapping, framing, and reference enrichment."""

import json
import os
import sys
from pathlib import Path

import pytest

from core.models import (
    AST_DEPENDENCY_EDGE_KINDS,
    AppConfig,
    DirectedEdgeQuery,
    EdgeKind,
    FileRecord,
    LspError,
    PathSecurityError,
    Position,
    Range,
    ReferenceRefreshState,
    RefreshStatus,
    RepoRecord,
    SymbolKind,
    SymbolNode,
)
from core.utils.hashing import sha256_file
from core.utils.pathing import extract_uri_candidates
from graph import ReferenceAnalyzer
from lsp import (
    LspClientOptions,
    LspSessionPool,
    PyrightLspClient,
    ReferenceEnricher,
)
from lsp.utils.framing import (
    encode_jsonrpc_message,
    parse_content_length_header,
    parse_header_line,
)
from lsp.utils.locations import (
    build_reference_edge,
    find_module_node_for_file,
    get_reference_query_position,
    normalize_location,
    pick_smallest_containing_symbol,
    range_contains,
)
from lsp.utils.utf16 import (
    codepoint_offset_to_utf16,
    position_codepoint_to_utf16,
    position_utf16_to_codepoint,
    range_utf16_to_codepoint,
    utf16_offset_to_codepoint,
)
from parsing import AstExtractionPipeline, RepoScanner, SymbolLinker
from storage import SqlUnitOfWorkFactory


@pytest.fixture
def uow_factory(tmp_path: Path) -> SqlUnitOfWorkFactory:
    """Create and initialize a SQLite UnitOfWorkFactory for LSP tests."""
    factory = SqlUnitOfWorkFactory(tmp_path / "lsp.db")
    with factory.open(initialize=True):
        pass
    return factory


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


class TestMapperUtf16AndFraming:
    """Tests for UTF-16 <-> Python codepoint mapping, containment, and wire framing."""

    def test_utf16_codepoint_roundtrip_with_supplementary_emoji(self) -> None:
        """Emoji supplementary plane characters convert cleanly between UTF-16 and codepoints."""
        line = "msg = '😀'; target()"
        cp_target = line.index("target")
        utf16_target = codepoint_offset_to_utf16(line, cp_target)

        assert cp_target == 11
        # '😀' is 1 Python codepoint but 2 UTF-16 code units
        assert utf16_target == 12
        assert utf16_offset_to_codepoint(line, utf16_target) == cp_target

        pos = position_codepoint_to_utf16([line], Position(line=0, character=cp_target))
        assert pos == Position(line=0, character=12)
        assert position_utf16_to_codepoint([line], pos) == Position(line=0, character=cp_target)

        rng = range_utf16_to_codepoint(
            [line],
            Range(
                start=Position(line=0, character=12),
                end=Position(line=0, character=18),
            ),
        )
        assert line[rng.start.character : rng.end.character] == "target"

    def test_range_contains_and_pick_smallest_prefers_non_module_on_tie(self) -> None:
        """pick_smallest_containing_symbol prefers non-module symbols on exact span ties."""
        outer = Range(start=Position(line=0, character=0), end=Position(line=10, character=0))
        inner = Range(start=Position(line=5, character=0), end=Position(line=6, character=0))
        assert range_contains(outer, inner) is True
        assert range_contains(inner, outer) is False

        mod_sym = SymbolNode(
            id="node:mod",
            repo_id="project:r",
            file_id="file:r:a.py",
            kind=SymbolKind.MODULE,
            name="a",
            qualified_name="a",
            uri="file:///a.py",
            decl_range=outer,
        )
        fn_sym = SymbolNode(
            id="node:fn",
            repo_id="project:r",
            file_id="file:r:a.py",
            kind=SymbolKind.FUNCTION,
            name="fn",
            qualified_name="a.fn",
            uri="file:///a.py",
            decl_range=outer,
        )
        picked = pick_smallest_containing_symbol([mod_sym, fn_sym], outer)
        assert picked is not None and picked.id == "node:fn"
        mod_node = find_module_node_for_file([mod_sym, fn_sym])
        assert mod_node is not None and mod_node.id == "node:mod"

    def test_normalize_location_and_framing_helpers(self) -> None:
        """normalize_location handles Location/LocationLink and framing enforces bounds."""
        loc = {
            "uri": "file:///a.py",
            "range": {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 5}},
        }
        norm_loc = normalize_location(loc)
        assert norm_loc is not None
        assert norm_loc.uri == "file:///a.py"
        assert norm_loc.range.start == Position(line=1, character=2)

        link = {
            "targetUri": "file:///b.py",
            "targetSelectionRange": {
                "start": {"line": 3, "character": 4},
                "end": {"line": 3, "character": 9},
            },
        }
        norm_link = normalize_location(link)
        assert norm_link is not None
        assert norm_link.uri == "file:///b.py"
        assert norm_link.range.start == Position(line=3, character=4)
        assert normalize_location({"uri": ""}) is None

        framed = encode_jsonrpc_message({"jsonrpc": "2.0", "id": 1}, max_payload_bytes=256)
        hdr_line, rest = framed.split(b"\r\n", 1)
        key, val = parse_header_line(hdr_line)
        assert key == "content-length"
        assert parse_content_length_header({key: val}, max_payload_bytes=256) == len(
            rest.removeprefix(b"\r\n")
        )
        with pytest.raises(LspError):
            encode_jsonrpc_message({"big": "x" * 100}, max_payload_bytes=16)


class TestResolverAndPosition:
    """Tests for ``get_reference_query_position`` and URI candidate resolution."""

    def test_query_position_preference_and_missing(self) -> None:
        """Prefers selection_range.start, falls back to decl_range, raises LspError if missing."""
        sym = SymbolNode(
            id="node:1",
            repo_id="project:r",
            file_id="file:r:a.py",
            kind=SymbolKind.FUNCTION,
            name="f",
            qualified_name="a.f",
            uri="file:///a.py",
            selection_range=Range(
                start=Position(line=2, character=4),
                end=Position(line=2, character=8),
            ),
            decl_range=Range(
                start=Position(line=1, character=0),
                end=Position(line=5, character=0),
            ),
        )
        assert get_reference_query_position(sym) == Position(line=2, character=4)
        no_sel = sym.model_copy(update={"selection_range": None})
        assert get_reference_query_position(no_sel) == Position(line=1, character=0)
        no_rng = no_sel.model_copy(update={"decl_range": None})
        with pytest.raises(LspError, match="no stored range"):
            get_reference_query_position(no_rng)

    def test_resolve_file_by_uri_percent_decoding_and_symlinked_root(
        self,
        uow_factory: SqlUnitOfWorkFactory,
        tmp_path: Path,
    ) -> None:
        """Percent-encoded spaces and symlinked root URIs resolve to the tracked FileRecord."""
        real_root = tmp_path / "real_repo"
        real_root.mkdir()
        target = real_root / "my file.py"
        target.write_text("x = 1\n", encoding="utf-8")
        symlink_root = tmp_path / "link_repo"
        symlink_root.symlink_to(real_root)

        repo_id = "project:real_repo:12345678"
        with uow_factory.open() as uow:
            uow.repos.upsert(
                RepoRecord(
                    id=repo_id,
                    root_path=str(real_root),
                    name="real_repo",
                    created_at="2026-01-01T00:00:00Z",
                )
            )
            uow.files.upsert_many([
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
            ])

        via_symlink_uri = (symlink_root / "my file.py").as_uri()
        candidates, resolved_path = extract_uri_candidates(via_symlink_uri)
        assert resolved_path == target.resolve()
        assert len(candidates) >= 2

        with uow_factory.open(read_only=True) as ro_uow:
            resolved = ro_uow.files.resolve_by_uri(via_symlink_uri, repo_id=repo_id)
            assert resolved is not None
            assert resolved.id == f"file:{repo_id}:my file.py"


class TestReferenceEdgePersistenceAndStats:
    """Tests for transactional LSP edge replacement, single JSON encoding, and AST fallback."""

    def test_replace_reference_edges_single_json_and_savepoint_safety(
        self,
        uow_factory: SqlUnitOfWorkFactory,
        tmp_path: Path,
    ) -> None:
        """replace_lsp_references_for_target stores single JSON and switches stats to LSP."""
        repo_id = "project:test:12345678"
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
        target_id = f"node:{repo_id}:function:a.target"
        caller_id = f"node:{repo_id}:function:a.caller"
        target_node = SymbolNode(
            id=target_id,
            repo_id=repo_id,
            file_id=file_rec.id,
            kind=SymbolKind.FUNCTION,
            name="target",
            qualified_name="a.target",
            uri=file_rec.uri,
        )
        caller_node = SymbolNode(
            id=caller_id,
            repo_id=repo_id,
            file_id=file_rec.id,
            kind=SymbolKind.FUNCTION,
            name="caller",
            qualified_name="a.caller",
            uri=file_rec.uri,
        )
        analyzer = ReferenceAnalyzer()

        with uow_factory.open() as uow:
            uow.repos.upsert(
                RepoRecord(
                    id=repo_id,
                    root_path=str(tmp_path),
                    name="test",
                    created_at="2026-01-01T00:00:00Z",
                )
            )
            uow.files.upsert_many([file_rec])
            uow.nodes.upsert_many([target_node, caller_node])

            pre_stats = analyzer.build_stats(uow, target_id)
            assert pre_stats.available is False
            assert pre_stats.reference_source == "ast"

            edge = build_reference_edge(
                repo_id=repo_id,
                target=target_node,
                source=caller_node,
                file_id=file_rec.id,
                evidence_uri=file_rec.uri,
                usage_range=Range(
                    start=Position(line=2, character=4),
                    end=Position(line=2, character=10),
                ),
            )
            assert edge.id == f"edge:{repo_id}:references:{caller_id}@3:5->{target_id}"
            assert edge.location == "/tmp/a.py:3:5"
            assert edge.confidence == 0.9

            module_node = SymbolNode(
                id=f"node:{repo_id}:module:a",
                repo_id=repo_id,
                file_id=file_rec.id,
                kind=SymbolKind.MODULE,
                name="a",
                qualified_name="a",
                uri=file_rec.uri,
            )
            mod_edge = build_reference_edge(
                repo_id=repo_id,
                target=target_node,
                source=module_node,
                file_id=file_rec.id,
                evidence_uri=file_rec.uri,
                usage_range=Range(
                    start=Position(line=0, character=12),
                    end=Position(line=0, character=18),
                ),
            )
            assert mod_edge.confidence == 0.9
            assert mod_edge.payload["mapping_mode"] == "module_scope"
            assert mod_edge.id == f"edge:{repo_id}:references:{module_node.id}@1:13->{target_id}"
            assert mod_edge.location == "/tmp/a.py:1:13"
            uow.edges.replace_lsp_references_for_target(repo_id, target_id, [edge])
            uow.refresh.upsert(
                ReferenceRefreshState(
                    target_symbol_id=target_id,
                    available=True,
                    last_refreshed_at="2026-01-02T00:00:00Z",
                    refresh_status=RefreshStatus.OK,
                )
            )

            post_stats = analyzer.build_stats(uow, target_id)
            assert post_stats.available is True
            assert post_stats.reference_source == "lsp"
            assert post_stats.reference_count == 1
            directed = uow.edges.list_directed(
                DirectedEdgeQuery(symbol_id=target_id, direction="incoming")
            )
            assert len(directed) == 1

        raw_res = uow_factory.execute_readonly_sql(
            f"SELECT payload_json, evidence_range_json FROM edges WHERE id = '{edge.id}'"
        )
        raw_row = raw_res.rows[0]
        assert isinstance(json.loads(raw_row["payload_json"]), dict)
        assert isinstance(json.loads(raw_row["evidence_range_json"]), dict)


class TestPyrightLspClientSubprocessProtocol:
    """Mocked subprocess protocol tests for ``PyrightLspClient`` and ``LspSessionPool``."""

    def test_missing_executable_raises_lsp_error(self, tmp_path: Path) -> None:
        """Unresolvable server_cmd executable raises LspError."""
        client = PyrightLspClient(
            LspClientOptions(server_cmd=("definitely_missing_lsp_binary_xyz",))
        )
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

        opts = LspClientOptions(
            server_cmd=(sys.executable, "-u", str(server_script)),
            request_timeout_seconds=5.0,
        )
        with PyrightLspClient(opts) as client:
            client.start(tmp_path)
            assert client.is_running is True
            assert len(client.stderr_lines) == 200
            assert "stderr flood line 349" in client.stderr_lines[-1]
            refs = client.find_references(
                (tmp_path / "a.py").as_uri(),
                Position(line=0, character=0),
            )
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

        opts = LspClientOptions(
            server_cmd=(sys.executable, "-u", str(server_script)),
            request_timeout_seconds=5.0,
            max_payload_bytes=512,
        )
        with PyrightLspClient(opts) as client:
            client.start(tmp_path)
            with pytest.raises(LspError, match="max_lsp_payload_bytes"):
                client.find_references(
                    (tmp_path / "a.py").as_uri(),
                    Position(line=0, character=0),
                )

    def test_timeout_and_zombie_free_kill_close(self, tmp_path: Path) -> None:
        """Hung request times out with LspError and close() kills/reaps SIGTERM-ignoring server."""
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

        opts = LspClientOptions(
            server_cmd=(sys.executable, "-u", str(server_script)),
            request_timeout_seconds=0.3,
            shutdown_timeout_seconds=0.2,
            terminate_wait_seconds=0.2,
        )
        client = PyrightLspClient(opts)
        client.start(tmp_path)
        assert client.pid is not None
        pid = client.pid
        with pytest.raises(LspError, match="timed out"):
            client.find_references(
                (tmp_path / "a.py").as_uri(),
                Position(line=0, character=0),
            )

        client.close()
        assert client.pid is None
        assert client.is_running is False
        # Process was reaped by wait(); waitpid must raise ChildProcessError
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_lsp_session_pool_caches_by_resolved_root_and_enriches_utf16_references(
        self,
        uow_factory: SqlUnitOfWorkFactory,
        tmp_path: Path,
    ) -> None:
        """LspSessionPool caches sessions, tracks didOpen/didChange, and enriches UTF-16 refs."""
        repo_root = tmp_path / "real_project"
        repo_root.mkdir()
        target_py = repo_root / "defs.py"
        caller_py = repo_root / "caller.py"
        target_py.write_text('TAG = "😀"\ndef target():\n    return 1\n', encoding="utf-8")
        caller_py.write_text(
            'from defs import target\n\ndef run():\n    x = "😀"; return target()\n',
            encoding="utf-8",
        )

        cfg = AppConfig()
        scan_report = RepoScanner(cfg).scan_repository(repo_root)
        pipeline = AstExtractionPipeline(cfg)
        linker = SymbolLinker()

        with uow_factory.open() as uow:
            uow.repos.upsert(scan_report.repo)
            for f in scan_report.files:
                ext = pipeline.extract_file(repo_root, f)
                uow.replace_file_graph(f, ext.nodes, ext.edges)
            nodes = uow.nodes.list_for_repo(scan_report.repo.id)
            cand_edges = uow.edges.list_for_repo(
                scan_report.repo.id, kinds=AST_DEPENDENCY_EDGE_KINDS
            )
            link_res = linker.resolve(scan_report.repo.id, scan_report.repo.name, nodes, cand_edges)
            uow.edges.update_resolved_targets(link_res.updates)

        with uow_factory.open(read_only=True) as ro_uow:
            target_node = ro_uow.nodes.get_by_qualified_name(
                scan_report.repo.id, "defs.target", kind=SymbolKind.FUNCTION
            )
            assert target_node is not None
            target_sym = ro_uow.nodes.get_summary_by_id(target_node.id)
            assert target_sym is not None

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
        enricher = ReferenceEnricher(cfg)
        opts = LspClientOptions(server_cmd=(sys.executable, "-u", str(server_script)))

        with LspSessionPool(opts, config=cfg) as pool:
            c1 = pool.acquire(repo_root)
            c2 = pool.acquire(symlink_root)
            assert c1 is c2
            assert len(starts_file.read_text(encoding="utf-8").splitlines()) == 1

            with uow_factory.open() as uow:
                result = enricher.enrich_symbol(uow, c1, target_sym)
                assert result.available is True
                assert result.reference_count == 1
                assert result.refresh_status == RefreshStatus.OK

                stored_edges = uow.edges.list_directed(
                    DirectedEdgeQuery(
                        symbol_id=target_sym.id,
                        direction="incoming",
                        kinds=(EdgeKind.REFERENCES,),
                    )
                )
                assert len(stored_edges) == 1
                assert stored_edges[0].evidence_range is not None
                assert stored_edges[0].evidence_range.start.character == cp_start

            assert isinstance(c1, PyrightLspClient)
            assert c1.document_version(target_py.resolve().as_uri()) == 1
            caller_uri = caller_py.resolve().as_uri()
            pool.notify_did_open_or_change(repo_root, caller_uri, "x = 1\n")
            assert c1.document_version(caller_uri) == 1
            pool.notify_did_open_or_change(repo_root, caller_uri, "x = 2\n")
            assert c1.document_version(caller_uri) == 2

            outside = tmp_path / "outside.py"
            outside.write_text("SECRET = 1\n", encoding="utf-8")
            (repo_root / "escape.py").symlink_to(outside)
            with pytest.raises(PathSecurityError):
                pool.notify_did_open_or_change(repo_root, (repo_root / "escape.py").as_uri())

            bad_sym = target_sym.model_copy(update={"file_path": "escape.py"})
            with pytest.raises(PathSecurityError), uow_factory.open() as uow:
                enricher.enrich_symbol(uow, c1, bad_sym)
