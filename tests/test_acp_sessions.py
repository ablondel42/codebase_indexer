"""End-to-end ACP v1 session setup, list, load replay, MCP security, and delete tests."""

import json
import logging
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.models.acp import (
    ACP_RESOURCE_NOT_FOUND,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
)
from api.models.config import (
    AcpServerConfig,
    AgentConfig,
    LogFormat,
    LoggingConfig,
    McpPolicyConfig,
)
from api.services.logging import AcpLoggingConfigurator
from api.services.server import ApiServerRunner
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
                if params.get("_meta", {{}}).get("emitMetadata"):
                    reply = {{
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {{
                            "sessionId": new_id,
                            "models": {{
                                "availableModels": [
                                    {{
                                        "modelId": "gemini-4-neon-high",
                                        "name": "Gemini 4 Neon (High)",
                                    }},
                                    {{
                                        "modelId": "gemini-4-carbon-high",
                                        "name": "Gemini 4 Carbon (High)",
                                    }},
                                    {{
                                        "modelId": "gemini-pro-orchestrator",
                                        "name": "Orchestrator (Carbon)",
                                    }},
                                ],
                                "currentModelId": "gemini-4-neon-high",
                            }},
                            "modes": {{
                                "currentModeId": "default",
                                "availableModes": [
                                    {{
                                        "id": "default",
                                        "name": "Default",
                                        "description": "Ask first",
                                    }},
                                    {{
                                        "id": "plan",
                                        "name": "Plan",
                                        "description": "Plan carefully",
                                    }},
                                ],
                            }},
                            "configOptions": [
                                {{
                                    "id": "model",
                                    "name": "Model",
                                    "category": "model",
                                    "type": "select",
                                    "currentValue": "gemini-4-neon",
                                    "options": [
                                        {{"value": "gemini-4-neon", "name": "Gemini 4 Neon"}},
                                        {{"value": "gemini-4-carbon", "name": "Gemini 4 Carbon"}},
                                        {{
                                            "value": "gemini-pro-orchestrator",
                                            "name": "Orchestrator (Carbon)",
                                        }},
                                    ],
                                }},
                                {{
                                    "id": "thought_level",
                                    "name": "Thinking Level",
                                    "category": "thought_level",
                                    "type": "select",
                                    "currentValue": "high",
                                    "options": [
                                        {{"value": "low", "name": "Low"}},
                                        {{"value": "high", "name": "High"}},
                                        {{"value": "xhigh", "name": "Extra"}},
                                    ],
                                }},
                                {{
                                    "id": "mode",
                                    "name": "Mode",
                                    "category": "mode",
                                    "type": "select",
                                    "currentValue": "default",
                                    "options": [
                                        {{"value": "default", "name": "Default"}},
                                        {{"value": "plan", "name": "Plan"}},
                                    ],
                                }},
                            ],
                            "_meta": {{
                                "antigravity": {{
                                    "tier": {{
                                        "name": "Google AI Ultra",
                                        "id": "g1-ultra-lite-tier",
                                        "upgradeUri": "https://antigravity.google/g1-upgrade",
                                    }}
                                }}
                            }},
                        }},
                    }}
                    sys.stdout.write(json.dumps(reply) + "\\n")
                    for upd in (
                        {{
                            "sessionUpdate": "available_commands_update",
                            "availableCommands": [
                                {{
                                    "name": "plan",
                                    "description": "Plan carefully before executing.",
                                }},
                                {{
                                    "name": "grill-me",
                                    "description": "Interview me to align on a plan.",
                                }},
                            ],
                        }},
                        {{
                            "sessionUpdate": "usage_update",
                            "used": 0,
                            "size": 1048576,
                        }},
                    ):
                        sys.stdout.write(
                            json.dumps({{
                                "jsonrpc": "2.0",
                                "method": "session/update",
                                "params": {{"sessionId": new_id, "update": upd}},
                            }})
                            + "\\n"
                        )
                    sys.stdout.flush()
                    continue
                reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{"sessionId": new_id}}}}
            elif method == "session/set_config_option":
                cfg_id = params["configId"]
                val = params["value"]
                reply = {{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{
                        "configOptions": [
                            {{
                                "id": "model",
                                "name": "Model",
                                "category": "model",
                                "type": "select",
                                "currentValue": val if cfg_id == "model" else "gemini-4-neon",
                                "options": [
                                    {{"value": "gemini-4-neon", "name": "Gemini 4 Neon"}},
                                    {{"value": "gemini-4-carbon", "name": "Gemini 4 Carbon"}},
                                    {{
                                        "value": "gemini-pro-orchestrator",
                                        "name": "Orchestrator (Carbon)",
                                    }},
                                ],
                            }},
                            {{
                                "id": "thought_level",
                                "name": "Thinking Level",
                                "category": "thought_level",
                                "type": "select",
                                "currentValue": val if cfg_id == "thought_level" else "high",
                                "options": [
                                    {{"value": "low", "name": "Low"}},
                                    {{"value": "high", "name": "High"}},
                                ],
                            }},
                            {{
                                "id": "mode",
                                "name": "Mode",
                                "category": "mode",
                                "type": "select",
                                "currentValue": val if cfg_id == "mode" else "default",
                                "options": [
                                    {{"value": "default", "name": "Default"}},
                                    {{"value": "plan", "name": "Plan"}},
                                ],
                            }},
                        ]
                    }},
                }}
            elif method == "session/set_mode":
                reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{}}}}
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


def _make_session_client(
    tmp_path: Path,
    script_path: Path,
    *,
    mcp: McpPolicyConfig | None = None,
) -> TestClient:
    """Create a ``TestClient`` configured with ``workspace_root=tmp_path``."""
    api_cfg = AcpServerConfig(
        agent=AgentConfig(
            command=(sys.executable, "-u", str(script_path)),
            workspace_root=tmp_path,
        ),
        mcp=mcp if mcp is not None else McpPolicyConfig(),
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


def test_zero_http_routes_mounted_on_pure_ws_server(tmp_path: Path) -> None:
    """Pure WebSocket gateway returns ``404`` for legacy HTTP wrapper routes."""
    script = _write_session_agent_script(tmp_path / "health_agent.py")
    with _make_session_client(tmp_path, script) as client:
        assert client.get("/healthz").status_code == 404
        assert client.get("/api/sessions/sess-1/state").status_code == 404
        assert client.get("/api/sessions/sess-1/artifacts").status_code == 404
        assert client.get("/api/sessions/sess-1/messages").status_code == 404


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
        AcpLoggingConfigurator(LoggingConfig(format=LogFormat.PRETTY, level="INFO")).configure()
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


def test_session_allowlisted_client_stdio_mcp_permitted(tmp_path: Path) -> None:
    """Explicit ``McpPolicyConfig.allowed_stdio_commands`` permits matching client stdio MCP."""
    capture_path = tmp_path / "stdio_allowed_capture.jsonl"
    script = _write_session_agent_script(
        tmp_path / "stdio_allowed_agent.py",
        capture_path=capture_path,
    )
    with (
        _make_session_client(
            tmp_path,
            script,
            mcp=McpPolicyConfig(allowed_stdio_commands=("/bin/echo",)),
        ) as client,
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
                        {"name": "ok-stdio", "command": "/bin/echo", "args": ["ready"]}
                    ],
                },
            }
        )
        assert ws.receive_json() == {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"sessionId": "sess_created_1"},
        }
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/new",
                "params": {
                    "cwd": str(tmp_path),
                    "mcpServers": [{"name": "blocked", "command": "/bin/sh", "args": []}],
                },
            }
        )
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS


def test_session_runtime_state_tracking_via_ws(tmp_path: Path) -> None:
    """Tracks session lifecycle, config option updates, mode switches, & post-close errors."""
    capture_path = tmp_path / "runtime_state_capture.jsonl"
    script = _write_session_agent_script(
        tmp_path / "runtime_state_agent.py",
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
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_config_option",
                "params": {"sessionId": "sess_missing", "configId": "model", "value": "x"},
            }
        )
        assert ws.receive_json()["error"]["code"] == ACP_RESOURCE_NOT_FOUND

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/new",
                "params": {"cwd": cwd, "_meta": {"emitMetadata": True}},
            }
        )
        new_resp = ws.receive_json()
        sid = new_resp["result"]["sessionId"]
        cmd_notif = ws.receive_json()
        usage_notif = ws.receive_json()
        assert cmd_notif["params"]["update"]["sessionUpdate"] == "available_commands_update"
        assert usage_notif["params"]["update"]["sessionUpdate"] == "usage_update"

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": sid,
                    "configId": "model",
                    "value": "not-an-option",
                },
            }
        )
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": sid,
                    "configId": "model",
                    "value": "gemini-4-carbon",
                    "_meta": {"authorization": "Bearer strip-me", "uiSource": "composer"},
                },
            }
        )
        cfg_resp = ws.receive_json()
        assert cfg_resp["id"] == 5
        assert cfg_resp["result"]["configOptions"][0]["currentValue"] == "gemini-4-carbon"

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "session/set_mode",
                "params": {"sessionId": sid, "modeId": "plan"},
            }
        )
        assert ws.receive_json() == {"jsonrpc": "2.0", "id": 6, "result": {}}

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/set_config_option",
                "params": {
                    "sessionId": sid,
                    "configId": "model",
                    "value": "gemini-pro-orchestrator",
                },
            }
        )
        orch_resp = ws.receive_json()
        assert orch_resp["id"] == 7
        assert orch_resp["result"]["configOptions"][0]["currentValue"] == "gemini-pro-orchestrator"

        ws.send_json(
            {"jsonrpc": "2.0", "id": 8, "method": "session/close", "params": {"sessionId": sid}}
        )
        assert ws.receive_json()["id"] == 8

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "session/set_mode",
                "params": {"sessionId": sid, "modeId": "default"},
            }
        )
        assert ws.receive_json()["error"]["code"] == ACP_RESOURCE_NOT_FOUND

    captured = [
        json.loads(line)
        for line in capture_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    forwarded_cfg = next(m for m in captured if m.get("id") == 5)
    assert forwarded_cfg["params"]["_meta"] == {"uiSource": "composer"}
