"""End-to-end ACP v1 session setup, list, load replay, MCP security, and delete tests."""

import json
import logging
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.models.acp import JSONRPC_INVALID_PARAMS, JSONRPC_METHOD_NOT_FOUND
from api.models.config import ApiServerConfig
from api.services.server import ApiServerRunner
from api.utils.formatting import configure_api_terminal_logging
from core.models.config import AppConfig
from core.utils.logging import ROOT_LOGGER_NAME, reset_logging

VALID_WS_HEADERS: dict[str, str] = {
    "host": "127.0.0.1:8000",
    "origin": "http://localhost:5173",
}


def _write_session_agent_script(
    script_path: Path,
    *,
    full_capabilities: bool = True,
    capture_path: Path | None = None,
) -> Path:
    """Write a deterministic ACP stdio agent supporting session lifecycle and replay."""
    script_body = textwrap.dedent(
        f"""
        import json
        import sys
        from pathlib import Path

        FULL_CAPS = {full_capabilities!r}
        CAPTURE = {str(capture_path)!r} if {capture_path is not None!r} else None
        sessions = {{
            "sess_existing": {{
                "sessionId": "sess_existing",
                "cwd": {str(script_path.parent.resolve())!r},
                "title": "Existing Architecture Discussion",
                "updatedAt": "2026-11-21T20:00:00Z",
            }}
        }}
        counter = 0

        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if CAPTURE is not None:
                with Path(CAPTURE).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(msg) + "\\n")

            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params") or {{}}

            if method == "initialize":
                if FULL_CAPS:
                    agent_caps = {{
                        "loadSession": True,
                        "mcpCapabilities": {{"http": True, "sse": False}},
                        "sessionCapabilities": {{
                            "list": {{}},
                            "load": {{}},
                            "resume": {{}},
                            "close": {{}},
                            "delete": {{}},
                            "additionalDirectories": {{}},
                            "systemPrompt": {{"mode": {{"override": {{}}}}}},
                        }},
                    }}
                else:
                    agent_caps = {{
                        "loadSession": False,
                        "mcpCapabilities": {{"http": False, "sse": False}},
                        "sessionCapabilities": {{"list": {{}}}},
                    }}
                reply = {{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{
                        "protocolVersion": 1,
                        "agentCapabilities": agent_caps,
                        "agentInfo": {{"name": "Session Fake Agent", "version": "1.0.0"}},
                        "authMethods": [],
                    }},
                }}
            elif method == "session/list":
                reply = {{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{"sessions": list(sessions.values())}},
                }}
            elif method == "session/new":
                counter += 1
                new_id = f"sess_created_{{counter}}"
                sessions[new_id] = {{
                    "sessionId": new_id,
                    "cwd": params["cwd"],
                    "title": "Created Session",
                    "updatedAt": "2026-11-21T21:00:00Z",
                }}
                reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{"sessionId": new_id}}}}
            elif method == "session/load":
                sid = params["sessionId"]
                for role_kind, msg_id, text in (
                    ("user_message_chunk", "msg_u1", "Explain FilesystemView"),
                    ("agent_message_chunk", "msg_a1", "FilesystemView builds a FolderNode tree."),
                ):
                    notif = {{
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {{
                            "sessionId": sid,
                            "update": {{
                                "sessionUpdate": role_kind,
                                "messageId": msg_id,
                                "content": {{"type": "text", "text": text}},
                            }},
                        }},
                    }}
                    sys.stdout.write(json.dumps(notif) + "\\n")
                    sys.stdout.flush()
                reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{}}}}
            elif method in ("session/resume", "session/close"):
                reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{}}}}
            elif method == "session/delete":
                sessions.pop(params["sessionId"], None)
                reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{}}}}
            else:
                reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{}}}}

            sys.stdout.write(json.dumps(reply) + "\\n")
            sys.stdout.flush()
        """
    ).lstrip()
    script_path.write_text(script_body, encoding="utf-8")
    return script_path


def _make_session_client(tmp_path: Path, script_path: Path) -> TestClient:
    """Create a ``TestClient`` configured with ``workspace_root=tmp_path``."""
    api_cfg = ApiServerConfig(
        agy_command=(sys.executable, "-u", str(script_path)),
        workspace_root=tmp_path,
    )
    app_cfg = AppConfig(db_path=tmp_path / "session_test.db")
    return TestClient(
        ApiServerRunner(api_cfg, app_cfg).create_app(),
        base_url="http://127.0.0.1:8000",
    )


def _init_msg() -> dict[str, Any]:
    """Return standard ``initialize`` JSON-RPC envelope."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "ui", "version": "0.2.0"},
        },
    }


def test_healthz_exposes_resolved_default_cwd(tmp_path: Path) -> None:
    """``GET /healthz`` exposes ``default_cwd`` matching ``workspace_root``."""
    script = _write_session_agent_script(tmp_path / "health_agent.py")
    with _make_session_client(tmp_path, script) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["default_cwd"] == str(tmp_path.resolve())
        assert body["cbi_mcp_enabled"] is True


def test_session_new_injects_curated_cbi_mcp_and_redacts_env_in_logs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``session/new`` injects ``codebase-indexer`` MCP, blocks client stdio, & redacts env."""
    capture_path = tmp_path / "session_new_capture.jsonl"
    script = _write_session_agent_script(
        tmp_path / "new_agent.py",
        capture_path=capture_path,
    )
    extra_dir = tmp_path / "shared_docs"
    extra_dir.mkdir()

    root_logger = logging.getLogger(ROOT_LOGGER_NAME)
    reset_logging()
    root_logger.addHandler(logging.NullHandler())

    try:
        configure_api_terminal_logging(debug=False)
        with (
            _make_session_client(tmp_path, script) as client,
            client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
        ):
            ws.send_json(_init_msg())
            ws.receive_json()

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {
                        "cwd": str(tmp_path),
                        "mcpServers": [
                            {"name": "evil", "command": "/bin/sh", "args": ["-c", "id"]}
                        ],
                    },
                }
            )
            blocked_stdio = ws.receive_json()
            assert blocked_stdio["error"]["code"] == JSONRPC_INVALID_PARAMS

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/new",
                    "params": {
                        "cwd": str(tmp_path),
                        "additionalDirectories": [str(extra_dir)],
                        "systemPrompt": {
                            "mode": "override",
                            "content": [{"type": "text", "text": "Be concise."}],
                        },
                        "mcpServers": [
                            {
                                "type": "http",
                                "name": "remote-mcp",
                                "url": "https://example.com/mcp",
                                "headers": [
                                    {"name": "Authorization", "value": "Bearer secret-hdr"}
                                ],
                            }
                        ],
                        "_meta": {"includeCbiMcp": True, "authorization": "strip-me"},
                    },
                }
            )
            created = ws.receive_json()
            assert created == {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {"sessionId": "sess_created_1"},
            }

        stderr_logs = capsys.readouterr().err
        assert '"value": "[REDACTED]"' in stderr_logs
        assert "Bearer secret-hdr" not in stderr_logs

        captured_lines = [
            json.loads(line)
            for line in capture_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        new_call = captured_lines[-1]["params"]
        assert "_meta" not in new_call
        assert new_call["additionalDirectories"] == [str(extra_dir.resolve())]
        mcp_names = [srv["name"] for srv in new_call["mcpServers"]]
        assert mcp_names == ["remote-mcp", "codebase-indexer"]
    finally:
        reset_logging()


def test_session_list_load_replay_resume_close_and_delete(tmp_path: Path) -> None:
    """Full lifecycle: ``list``, ``load`` (replaying chunks), ``resume``, ``close``, ``delete``."""
    script = _write_session_agent_script(tmp_path / "lifecycle_agent.py")
    cwd = str(tmp_path.resolve())

    with (
        _make_session_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(_init_msg())
        ws.receive_json()

        ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "session/list", "params": {"cwd": cwd}})
        listed = ws.receive_json()["result"]["sessions"]
        assert [s["sessionId"] for s in listed] == ["sess_existing"]

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/load",
                "params": {"sessionId": "sess_existing", "cwd": cwd, "mcpServers": []},
            }
        )
        chunk_1 = ws.receive_json()
        chunk_2 = ws.receive_json()
        load_done = ws.receive_json()
        assert chunk_1["method"] == "session/update"
        assert chunk_1["params"]["update"]["sessionUpdate"] == "user_message_chunk"
        assert chunk_2["method"] == "session/update"
        assert chunk_2["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
        assert load_done == {"jsonrpc": "2.0", "id": 3, "result": {}}

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/resume",
                "params": {"sessionId": "sess_existing", "cwd": cwd, "mcpServers": []},
            }
        )
        assert ws.receive_json() == {"jsonrpc": "2.0", "id": 4, "result": {}}

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "session/close",
                "params": {"sessionId": "sess_existing"},
            }
        )
        assert ws.receive_json() == {"jsonrpc": "2.0", "id": 5, "result": {}}

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "session/delete",
                "params": {"sessionId": "sess_existing"},
            }
        )
        assert ws.receive_json() == {"jsonrpc": "2.0", "id": 6, "result": {}}

        ws.send_json({"jsonrpc": "2.0", "id": 7, "method": "session/list", "params": {}})
        assert ws.receive_json()["result"]["sessions"] == []


def test_session_capability_and_path_security_gates(tmp_path: Path) -> None:
    """Rejects relative/missing ``cwd`` (``-32602``) and unadvertised capabilities (``-32601``)."""
    capture_path = tmp_path / "restricted_capture.jsonl"
    script = _write_session_agent_script(
        tmp_path / "restricted_agent.py",
        full_capabilities=False,
        capture_path=capture_path,
    )
    cwd = str(tmp_path.resolve())

    with (
        _make_session_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(_init_msg())
        ws.receive_json()

        ws.send_json(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "relative/dir"}}
        )
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/new",
                "params": {"cwd": str(tmp_path / "missing_dir")},
            }
        )
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/new",
                "params": {"cwd": cwd, "additionalDirectories": [cwd]},
            }
        )
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS

        for idx, method_call in enumerate(
            (
                ("session/load", {"sessionId": "s1", "cwd": cwd}),
                ("session/resume", {"sessionId": "s1", "cwd": cwd}),
                ("session/close", {"sessionId": "s1"}),
                ("session/delete", {"sessionId": "s1"}),
            ),
            start=10,
        ):
            ws.send_json(
                {"jsonrpc": "2.0", "id": idx, "method": method_call[0], "params": method_call[1]}
            )
            assert ws.receive_json()["error"]["code"] == JSONRPC_METHOD_NOT_FOUND

    forwarded = [
        json.loads(line)["method"]
        for line in capture_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert forwarded == ["initialize"]


def test_session_artifacts_endpoints_list_read_and_enforce_containment(tmp_path: Path) -> None:
    """Artifact HTTP endpoints list/read ``brain/<sessionId>/*.md`` & block traversal/rebinding."""
    script = _write_session_agent_script(tmp_path / "artifacts_agent.py")
    gemini_root = tmp_path / "gemini_store"
    session_brain = gemini_root / "antigravity" / "brain" / "sess-123"
    session_brain.mkdir(parents=True)

    (session_brain / "walkthrough.md").write_text("# Walkthrough\nAll green.\n", encoding="utf-8")
    (session_brain / "walkthrough.md.metadata.json").write_text(
        json.dumps(
            {
                "summary": "Verified ACP walkthrough.",
                "updatedAt": "2026-11-21T22:00:00Z",
                "userFacing": True,
            }
        ),
        encoding="utf-8",
    )
    (session_brain / "plan.md").write_text("# Plan\nStep 1.\n", encoding="utf-8")
    (session_brain / ".hidden.md").write_text("secret", encoding="utf-8")
    sys_dir = session_brain / ".system_generated"
    sys_dir.mkdir()
    (sys_dir / "internal.md").write_text("internal", encoding="utf-8")
    outside_secret = tmp_path / "outside.md"
    outside_secret.write_text("outside", encoding="utf-8")
    (session_brain / "escape.md").symlink_to(outside_secret)

    api_cfg = ApiServerConfig(
        agy_command=(sys.executable, "-u", str(script)),
        agy_gemini_dir=str(gemini_root.resolve()),
        workspace_root=tmp_path,
    )
    app = ApiServerRunner(api_cfg, AppConfig(db_path=tmp_path / "art.db")).create_app()

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        rebind = client.get(
            "/api/sessions/sess-123/artifacts",
            headers={"host": "attacker.example:8000"},
        )
        assert rebind.status_code == 403

        bad_origin = client.get(
            "/api/sessions/sess-123/artifacts",
            headers={"host": "127.0.0.1:8000", "origin": "https://evil.example"},
        )
        assert bad_origin.status_code == 403

        listed = client.get(
            "/api/sessions/sess-123/artifacts",
            headers={"host": "127.0.0.1:8000"},
        )
        assert listed.status_code == 200
        names = [item["name"] for item in listed.json()["artifacts"]]
        assert set(names) == {"walkthrough.md", "plan.md"}

        doc = client.get(
            "/api/sessions/sess-123/artifacts/walkthrough.md",
            headers={"host": "127.0.0.1:8000"},
        )
        assert doc.status_code == 200
        body = doc.json()
        assert body["summary"] == "Verified ACP walkthrough."
        assert body["content"] == "# Walkthrough\nAll green.\n"

        empty_sess = client.get(
            "/api/sessions/sess-empty/artifacts",
            headers={"host": "127.0.0.1:8000"},
        )
        assert empty_sess.status_code == 200
        assert empty_sess.json()["artifacts"] == []

        assert (
            client.get(
                "/api/sessions/sess-123/artifacts/escape.md",
                headers={"host": "127.0.0.1:8000"},
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/sessions/sess-123/artifacts/.hidden.md",
                headers={"host": "127.0.0.1:8000"},
            ).status_code
            == 400
        )
        assert (
            client.get(
                "/api/sessions/sess-123/artifacts/walkthrough.md.metadata.json",
                headers={"host": "127.0.0.1:8000"},
            ).status_code
            == 400
        )
        assert (
            client.get(
                "/api/sessions/..%2Fescape/artifacts",
                headers={"host": "127.0.0.1:8000"},
            ).status_code
            in (400, 404)
        )
