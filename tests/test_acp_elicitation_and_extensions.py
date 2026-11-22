"""Tests for ACP elicitation, boolean config options, extensions, subagents, & Bearer auth."""

import json
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from api.models.acp import JSONRPC_INVALID_PARAMS, JSONRPC_METHOD_NOT_FOUND
from api.models.config import (
    AcpServerConfig,
    AgentConfig,
    CapabilityPolicyConfig,
    TransportConfig,
)
from api.services.server import ApiServerRunner
from core.models.config import AppConfig

VALID_WS_HEADERS: dict[str, str] = {
    "host": "127.0.0.1:8000",
    "origin": "http://localhost:5173",
}


def _write_elicitation_agent(script_path: Path) -> Path:
    """Write a stdio ACP agent exercising elicitation, config options, & extensions."""
    script_body = textwrap.dedent(
        """
        import json
        import sys

        def send(payload):
            sys.stdout.write(json.dumps(payload) + "\\n")
            sys.stdout.flush()

        def read_next():
            for raw in sys.stdin:
                s = raw.strip()
                if s:
                    return json.loads(s)
            return None

        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            msg = json.loads(line)
            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params") or {}
            if method == "initialize":
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "loadSession": True,
                            "sessionCapabilities": {"load": {}},
                        },
                        "agentInfo": {"name": "Elicit Agent", "version": "1.0.0"},
                        "authMethods": [],
                    },
                })
            elif method == "session/new":
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "sessionId": "s_el",
                        "configOptions": [
                            {
                                "id": "strict",
                                "name": "Strict",
                                "type": "boolean",
                                "currentValue": False,
                            },
                            {
                                "id": "mode",
                                "name": "Mode",
                                "type": "select",
                                "currentValue": "default",
                                "options": [{"value": "default", "name": "Default"}],
                            },
                        ],
                    },
                })
            elif method == "session/set_config_option":
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "configOptions": [
                            {
                                "id": "strict",
                                "name": "Strict",
                                "type": "boolean",
                                "currentValue": params.get("value"),
                            }
                        ]
                    },
                })
            elif method == "_vendor/ping":
                send({"jsonrpc": "2.0", "id": req_id, "result": {"pong": True}})
            elif method == "session/load":
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": params["sessionId"],
                        "update": {
                            "sessionUpdate": "subagent_update",
                            "sessionId": "child_hist",
                            "status": "running",
                        },
                    },
                })
                send({"jsonrpc": "2.0", "id": req_id, "result": {}})
            elif method == "session/prompt":
                sid = params["sessionId"]
                cmd = json.loads(params["prompt"][0]["text"])
                send({
                    "jsonrpc": "2.0",
                    "id": cmd["reqId"],
                    "method": "elicitation/create",
                    "params": cmd["elicitParams"],
                })
                rep = read_next()
                if cmd.get("completeUrlId"):
                    send({
                        "jsonrpc": "2.0",
                        "method": "elicitation/complete",
                        "params": {"elicitationId": cmd["completeUrlId"]},
                    })
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"stopReason": "end_turn", "_meta": {"reply": rep}},
                })
            else:
                send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        """
    ).lstrip()
    script_path.write_text(script_body, encoding="utf-8")
    return script_path


def _make_client(
    tmp_path: Path,
    script: Path,
    *,
    caps: CapabilityPolicyConfig | None = None,
    transport: TransportConfig | None = None,
) -> TestClient:
    """Create a ``TestClient`` configured for elicitation and extension tests."""
    cfg = AcpServerConfig(
        transport=transport if transport is not None else TransportConfig(),
        agent=AgentConfig(command=(sys.executable, "-u", str(script)), workspace_root=tmp_path),
        capabilities=caps if caps is not None else CapabilityPolicyConfig(),
    )
    app = ApiServerRunner(cfg, AppConfig(db_path=tmp_path / "elic.db")).create_app()
    return TestClient(app, base_url="http://127.0.0.1:8000")


def test_elicitation_empty_caps_rejects_form_and_url_modes(tmp_path: Path) -> None:
    """Advertising ``elicitation: {}`` enables neither ``form`` nor ``url`` (``-32602``)."""
    script = _write_elicitation_agent(tmp_path / "empty_elic.py")
    with (
        _make_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {"elicitation": {}}},
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

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "reqId": 81,
                                    "elicitParams": {
                                        "sessionId": sid,
                                        "mode": "form",
                                        "message": "Enter token",
                                        "requestedSchema": {"type": "object", "properties": {}},
                                    },
                                }
                            ),
                        }
                    ],
                },
            }
        )
        prompt_rep = ws.receive_json()
        assert prompt_rep["result"]["_meta"]["reply"]["error"]["code"] == JSONRPC_INVALID_PARAMS


def test_elicitation_form_validation_and_url_https_complete_flow(tmp_path: Path) -> None:
    """Validates form schema values, blocks HTTP URLs, and tracks ``elicitation/complete``."""
    script = _write_elicitation_agent(tmp_path / "full_elic.py")
    with (
        _make_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {"elicitation": {"form": {}, "url": {}}},
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

        # Reject plain HTTP elicitation URLs unless elicitation_allow_http_url=True.
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "reqId": 82,
                                    "elicitParams": {
                                        "sessionId": sid,
                                        "mode": "url",
                                        "message": "Login",
                                        "elicitationId": "url_http",
                                        "url": "http://insecure.example/auth",
                                    },
                                }
                            ),
                        }
                    ],
                },
            }
        )
        http_rep = ws.receive_json()
        assert http_rep["result"]["_meta"]["reply"]["error"]["code"] == JSONRPC_INVALID_PARAMS

        # Form elicitation validates schema bounds and retries cleanly on invalid content.
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "reqId": 83,
                                    "elicitParams": {
                                        "sessionId": sid,
                                        "mode": "form",
                                        "message": "Choose count",
                                        "requestedSchema": {
                                            "type": "object",
                                            "properties": {
                                                "count": {"type": "integer", "minimum": 1}
                                            },
                                            "required": ["count"],
                                        },
                                    },
                                }
                            ),
                        }
                    ],
                },
            }
        )
        elic_req = ws.receive_json()
        assert elic_req["id"] == 83
        ws.send_json(
            {"jsonrpc": "2.0", "id": 83, "result": {"action": "accept", "content": {"count": 0}}}
        )
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS
        ws.send_json(
            {"jsonrpc": "2.0", "id": 83, "result": {"action": "accept", "content": {"count": 2}}}
        )
        assert ws.receive_json()["result"]["_meta"]["reply"]["result"]["content"] == {"count": 2}

        # Valid HTTPS URL elicitation followed by elicitation/complete notification.
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "reqId": 84,
                                    "completeUrlId": "url_https_1",
                                    "elicitParams": {
                                        "sessionId": sid,
                                        "mode": "url",
                                        "message": "Complete OAuth",
                                        "elicitationId": "url_https_1",
                                        "url": "https://accounts.example.com/oauth",
                                    },
                                }
                            ),
                        }
                    ],
                },
            }
        )
        url_req = ws.receive_json()
        assert url_req["id"] == 84
        ws.send_json({"jsonrpc": "2.0", "id": 84, "result": {"action": "accept"}})
        complete_notif = ws.receive_json()
        assert complete_notif["method"] == "elicitation/complete"
        assert complete_notif["params"] == {"elicitationId": "url_https_1"}
        assert ws.receive_json()["id"] == 5


def test_boolean_config_option_extension_gating_and_historical_subagent(tmp_path: Path) -> None:
    """Filters boolean options without cap, gates ``_*`` extensions, & skips replay child."""
    script = _write_elicitation_agent(tmp_path / "ext_agent.py")
    caps = CapabilityPolicyConfig(
        allow_extension_methods=True,
        allowed_extension_prefixes=("_vendor/",),
    )
    with (
        _make_client(tmp_path, script, caps=caps) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {"subagents": {}},
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
        new_res = ws.receive_json()["result"]
        assert [o["id"] for o in new_res["configOptions"]] == ["mode"]

        # Disallowed prefix rejected; allowlisted _vendor/ping forwarded.
        ws.send_json({"jsonrpc": "2.0", "id": 3, "method": "_other/ping", "params": {}})
        assert ws.receive_json()["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
        ws.send_json({"jsonrpc": "2.0", "id": 4, "method": "_vendor/ping", "params": {}})
        assert ws.receive_json()["result"] == {"pong": True}

        # Historical replay subagent child does not block cancel after session/load ends.
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "session/load",
                "params": {
                    "sessionId": new_res["sessionId"],
                    "cwd": str(tmp_path.resolve()),
                    "mcpServers": [],
                },
            }
        )
        sub_notif = ws.receive_json()
        assert sub_notif["params"]["update"]["sessionId"] == "child_hist"
        assert ws.receive_json()["id"] == 5


def test_bearer_token_required_when_configured_on_upgrade(tmp_path: Path) -> None:
    """Configured ``transport.auth_token`` enforces constant-time Bearer auth on upgrade."""
    script = _write_elicitation_agent(tmp_path / "auth_tok.py")
    transport = TransportConfig(auth_token=SecretStr("top-bearer-secret"))
    with _make_client(tmp_path, script, transport=transport) as client:
        with (
            pytest.raises(WebSocketDisconnect) as bad_exc,
            client.websocket_connect("/acp", headers={"host": "127.0.0.1:8000"}),
        ):
            pytest.fail("Expected rejection without Bearer token")
        assert bad_exc.value.code == 1008

        with client.websocket_connect(
            "/acp",
            headers={
                "host": "127.0.0.1:8000",
                "authorization": "Bearer top-bearer-secret",
            },
        ) as ws:
            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 1, "clientCapabilities": {}},
                }
            )
            assert ws.receive_json()["id"] == 1
