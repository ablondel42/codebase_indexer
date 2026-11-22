"""End-to-end tests for ACP ``fs/*`` and ``terminal/*`` disabled/relay/host security & async."""

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from api.models.acp import (
    ACP_REQUEST_CANCELLED,
    ACP_RESOURCE_NOT_FOUND,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
)
from api.models.config import (
    AcpServerConfig,
    AgentConfig,
    CapabilityPolicy,
    CapabilityPolicyConfig,
)
from api.services.server import ApiServerRunner
from api.utils.terminal_buffer import append_bounded_utf8, decode_terminal_buffer
from core.models.config import AppConfig

VALID_WS_HEADERS: dict[str, str] = {
    "host": "127.0.0.1:8000",
    "origin": "http://localhost:5173",
}


def _write_ops_agent(script_path: Path, *, scenario_file: Path | None = None) -> Path:
    """Write a stdio ACP agent driving host/relay FS and terminal scenarios from prompts."""
    script_body = textwrap.dedent(
        f"""
        import json
        import sys
        from pathlib import Path

        LOG = {str(scenario_file)!r} if {scenario_file is not None!r} else None

        def send(payload):
            sys.stdout.write(json.dumps(payload) + "\\n")
            sys.stdout.flush()

        def read_reply():
            for raw in sys.stdin:
                s = raw.strip()
                if s:
                    return json.loads(s)
            return None

        def call_client(req_id, method, params):
            send({{"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}})
            while True:
                msg = read_reply()
                if msg is None:
                    return {{"error": {{"code": -32603, "message": "EOF"}}}}
                if msg.get("id") == req_id:
                    return msg

        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            msg = json.loads(line)
            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params") or {{}}
            if method == "initialize":
                if LOG is not None:
                    Path(LOG).write_text(json.dumps(params), encoding="utf-8")
                send({{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{
                        "protocolVersion": 1,
                        "agentCapabilities": {{
                            "sessionCapabilities": {{"close": {{}}}},
                        }},
                        "agentInfo": {{"name": "Ops Agent", "version": "1.0.0"}},
                        "authMethods": [],
                    }},
                }})
            elif method == "session/new":
                send({{"jsonrpc": "2.0", "id": req_id, "result": {{"sessionId": "s_ops"}}}})
            elif method == "session/close":
                send({{"jsonrpc": "2.0", "id": req_id, "result": {{}}}})
            elif method == "session/prompt":
                sid = params["sessionId"]
                cmd = json.loads(params["prompt"][0]["text"])
                results = []
                for step in cmd["steps"]:
                    step_id = step["id"]
                    send({{
                        "jsonrpc": "2.0",
                        "id": step_id,
                        "method": step["method"],
                        "params": step["params"],
                    }})
                if cmd.get("cancelId") is not None:
                    send({{
                        "jsonrpc": "2.0",
                        "method": "$/cancel_request",
                        "params": {{"requestId": cmd["cancelId"]}},
                    }})
                expected = set(cmd.get("waitIds", [s["id"] for s in cmd["steps"]]))
                while len(results) < len(expected):
                    rep = read_reply()
                    if rep is None:
                        break
                    if rep.get("id") in expected:
                        results.append(rep)
                send({{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{"stopReason": "end_turn", "_meta": {{"replies": results}}}},
                }})
            else:
                send({{"jsonrpc": "2.0", "id": req_id, "result": {{}}}})
        """
    ).lstrip()
    script_path.write_text(script_body, encoding="utf-8")
    return script_path


def _make_ops_client(
    tmp_path: Path,
    script: Path,
    caps: CapabilityPolicyConfig,
) -> TestClient:
    """Build ``TestClient`` with explicit FS and terminal policies."""
    cfg = AcpServerConfig(
        agent=AgentConfig(command=(sys.executable, "-u", str(script)), workspace_root=tmp_path),
        capabilities=caps,
    )
    app = ApiServerRunner(cfg, AppConfig(db_path=tmp_path / "ops.db")).create_app()
    return TestClient(app, base_url="http://127.0.0.1:8000")


def _run_prompt_steps(
    ws: Any,
    sid: str,
    steps: list[dict[str, Any]],
    *,
    cancel_id: int | None = None,
) -> list[dict[str, Any]]:
    """Run agent->client step batch inside ``session/prompt`` and return captured replies."""
    payload = json.dumps({"steps": steps, "cancelId": cancel_id})
    ws.send_json(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": payload}]},
        }
    )
    resp = ws.receive_json()
    return list(resp["result"]["_meta"]["replies"])


def test_disabled_defaults_block_agent_fs_and_terminal_requests(tmp_path: Path) -> None:
    """Default ``disabled`` policy advertises false caps and rejects agent FS/terminal calls."""
    init_capture = tmp_path / "disabled_init.json"
    script = _write_ops_agent(tmp_path / "disabled_ops.py", scenario_file=init_capture)
    target = tmp_path / "sample.txt"
    target.write_text("hello", encoding="utf-8")

    with (
        _make_ops_client(tmp_path, script, CapabilityPolicyConfig()) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": True,
                    },
                },
            }
        )
        ws.receive_json()
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": str(tmp_path.resolve())},
            }
        )
        sid = ws.receive_json()["result"]["sessionId"]
        replies = _run_prompt_steps(
            ws,
            sid,
            [
                {
                    "id": 201,
                    "method": "fs/read_text_file",
                    "params": {"sessionId": sid, "path": str(target)},
                },
                {
                    "id": 202,
                    "method": "terminal/create",
                    "params": {"sessionId": sid, "command": sys.executable, "args": ["-V"]},
                },
            ],
        )

    assert all(r["error"]["code"] == JSONRPC_METHOD_NOT_FOUND for r in replies)
    sent_caps = json.loads(init_capture.read_text(encoding="utf-8"))["clientCapabilities"]
    assert sent_caps["fs"] == {"readTextFile": False, "writeTextFile": False}
    assert sent_caps["terminal"] is False


def test_host_fs_containment_symlink_escape_slicing_and_sensitive_writes(tmp_path: Path) -> None:
    """Host FS slices lines, creates files safely, and blocks escapes/sensitive writes."""
    ws_dir = (tmp_path / "workspace").resolve()
    ws_dir.mkdir()
    outside = (tmp_path / "outside_secret.txt").resolve()
    outside.write_text("top secret", encoding="utf-8")

    notes = ws_dir / "notes.txt"
    notes.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    escape_link = ws_dir / "escape_link.txt"
    os.symlink(outside, escape_link)

    script = _write_ops_agent(tmp_path / "host_fs.py")
    caps = CapabilityPolicyConfig(fs_read=CapabilityPolicy.HOST, fs_write=CapabilityPolicy.HOST)

    with (
        _make_ops_client(tmp_path, script, caps) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {}},
            }
        )
        ws.receive_json()
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": str(ws_dir)},
            }
        )
        sid = ws.receive_json()["result"]["sessionId"]

        replies = _run_prompt_steps(
            ws,
            sid,
            [
                {
                    "id": 301,
                    "method": "fs/read_text_file",
                    "params": {"sessionId": sid, "path": str(notes), "line": 2, "limit": 2},
                },
                {
                    "id": 302,
                    "method": "fs/read_text_file",
                    "params": {"sessionId": sid, "path": str(escape_link)},
                },
                {
                    "id": 303,
                    "method": "fs/read_text_file",
                    "params": {"sessionId": sid, "path": str(ws_dir / "nope.txt")},
                },
                {
                    "id": 304,
                    "method": "fs/write_text_file",
                    "params": {
                        "sessionId": sid,
                        "path": str(ws_dir / "sub" / "out.txt"),
                        "content": "created safely",
                    },
                },
                {
                    "id": 305,
                    "method": "fs/write_text_file",
                    "params": {
                        "sessionId": sid,
                        "path": str(ws_dir / ".git" / "hooks" / "pre-commit"),
                        "content": "#!/bin/sh\necho pwned\n",
                    },
                },
            ],
        )

    by_id = {r["id"]: r for r in replies}
    assert by_id[301]["result"] == {"content": "line2\nline3\n"}
    assert by_id[302]["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert by_id[303]["error"]["code"] == ACP_RESOURCE_NOT_FOUND
    assert by_id[304]["result"] == {}
    assert (ws_dir / "sub" / "out.txt").read_text(encoding="utf-8") == "created safely"
    assert by_id[305]["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert not (ws_dir / ".git" / "hooks" / "pre-commit").exists()


def test_utf8_character_boundary_ring_buffer_truncation() -> None:
    """``append_bounded_utf8`` never leaves invalid leading UTF-8 continuation bytes."""
    buf, trunc = append_bounded_utf8(b"", "héllo🌍world".encode(), byte_limit=8)
    assert trunc is True
    decoded = decode_terminal_buffer(buf)
    assert "\ufffd" not in decoded
    assert decoded.endswith("world")


def test_host_terminal_security_nonblocking_concurrency_and_cleanup(tmp_path: Path) -> None:
    """Host terminals enforce allowlist/env, run ``wait_for_exit`` non-blocking, & cancel."""
    ws_dir = (tmp_path / "term_ws").resolve()
    ws_dir.mkdir()
    script = _write_ops_agent(tmp_path / "term_agent.py")
    caps = CapabilityPolicyConfig(
        terminal=CapabilityPolicy.HOST,
        terminal_allowed_commands=(sys.executable, Path(sys.executable).name),
    )

    with (
        _make_ops_client(tmp_path, script, caps) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {}},
            }
        )
        ws.receive_json()
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": str(ws_dir)},
            }
        )
        sid = ws.receive_json()["result"]["sessionId"]

        guard_replies = _run_prompt_steps(
            ws,
            sid,
            [
                {
                    "id": 401,
                    "method": "terminal/create",
                    "params": {"sessionId": sid, "command": "/bin/sh", "args": ["-c", "id"]},
                },
                {
                    "id": 402,
                    "method": "terminal/create",
                    "params": {
                        "sessionId": sid,
                        "command": sys.executable,
                        "args": ["-c", "print('ok')"],
                        "env": [{"name": "DYLD_INSERT_LIBRARIES", "value": "/tmp/evil.dylib"}],
                    },
                },
                {
                    "id": 403,
                    "method": "terminal/create",
                    "params": {
                        "sessionId": sid,
                        "command": sys.executable,
                        "args": ["-c", "import time; print('started', flush=True); time.sleep(10)"],
                    },
                },
            ],
        )
        g_by_id = {r["id"]: r for r in guard_replies}
        assert g_by_id[401]["error"]["code"] == JSONRPC_INVALID_PARAMS
        assert g_by_id[402]["error"]["code"] == JSONRPC_INVALID_PARAMS
        term_id = g_by_id[403]["result"]["terminalId"]

        # Start wait_for_exit concurrently and cancel it via $/cancel_request without blocking.
        cancel_replies = _run_prompt_steps(
            ws,
            sid,
            [
                {
                    "id": 404,
                    "method": "terminal/wait_for_exit",
                    "params": {"sessionId": sid, "terminalId": term_id},
                },
                {
                    "id": 405,
                    "method": "terminal/output",
                    "params": {"sessionId": sid, "terminalId": term_id},
                },
            ],
            cancel_id=404,
        )
        c_by_id = {r["id"]: r for r in cancel_replies}
        assert c_by_id[404]["error"]["code"] == ACP_REQUEST_CANCELLED
        assert "started" in c_by_id[405]["result"]["output"]

        kill_replies = _run_prompt_steps(
            ws,
            sid,
            [
                {
                    "id": 406,
                    "method": "terminal/kill",
                    "params": {"sessionId": sid, "terminalId": term_id},
                },
                {
                    "id": 407,
                    "method": "terminal/wait_for_exit",
                    "params": {"sessionId": sid, "terminalId": term_id},
                },
                {
                    "id": 408,
                    "method": "terminal/release",
                    "params": {"sessionId": sid, "terminalId": term_id},
                },
                {
                    "id": 409,
                    "method": "terminal/output",
                    "params": {"sessionId": sid, "terminalId": term_id},
                },
            ],
        )
        k_by_id = {r["id"]: r for r in kill_replies}
        assert k_by_id[406]["result"] == {}
        assert k_by_id[407]["result"] != {}
        assert k_by_id[408]["result"] == {}
        assert k_by_id[409]["error"]["code"] == ACP_RESOURCE_NOT_FOUND


def test_relay_mode_fs_routes_valid_calls_to_ws_client(tmp_path: Path) -> None:
    """``relay`` FS mode forwards agent FS calls to WS client & returns validated result."""
    ws_dir = tmp_path.resolve()
    target = ws_dir / "relayed.txt"
    script_body = textwrap.dedent(
        """
        import json
        import sys

        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            msg = json.loads(line)
            method = msg.get("method")
            req_id = msg.get("id")
            if method == "initialize":
                reply = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []},
                }
            elif method == "session/new":
                reply = {"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": "s_rel"}}
            elif method == "session/prompt":
                target_path = msg["params"]["prompt"][0]["text"]
                sys.stdout.write(
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": 77,
                        "method": "fs/read_text_file",
                        "params": {"sessionId": "s_rel", "path": target_path},
                    }) + "\\n"
                )
                sys.stdout.flush()
                client_rep = json.loads(sys.stdin.readline())
                reply = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"stopReason": "end_turn", "_meta": {"read": client_rep["result"]}},
                }
            else:
                reply = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            sys.stdout.write(json.dumps(reply) + "\\n")
            sys.stdout.flush()
        """
    ).lstrip()
    script = tmp_path / "relay_fs.py"
    script.write_text(script_body, encoding="utf-8")

    caps = CapabilityPolicyConfig(fs_read=CapabilityPolicy.RELAY)
    with (
        _make_ops_client(tmp_path, script, caps) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": True}},
                },
            }
        )
        ws.receive_json()
        ws.send_json(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(ws_dir)}}
        )
        sid = ws.receive_json()["result"]["sessionId"]
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": str(target)}]},
            }
        )
        relayed_req = ws.receive_json()
        assert relayed_req["id"] == 77
        assert relayed_req["method"] == "fs/read_text_file"
        ws.send_json({"jsonrpc": "2.0", "id": 77, "result": {"content": "from-ws-client"}})
        prompt_done = ws.receive_json()
        assert prompt_done["result"]["_meta"]["read"] == {"content": "from-ws-client"}
