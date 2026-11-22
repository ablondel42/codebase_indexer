"""End-to-end ACP v1 authentication, method gating, secret redaction, and logout tests."""

import json
import logging
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.models.acp import (
    ACP_AUTH_REQUIRED,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
)
from api.models.config import (
    AcpServerConfig,
    AgentConfig,
    CapabilityPolicyConfig,
    LogFormat,
    LoggingConfig,
    RelayOnlyPolicy,
)
from api.services.logging import AcpLoggingConfigurator
from api.services.server import ApiServerRunner
from core.models.config import AppConfig
from core.utils.logging import ROOT_LOGGER_NAME, reset_logging

VALID_WS_HEADERS: dict[str, str] = {
    "host": "127.0.0.1:8000",
    "origin": "http://localhost:5173",
}


def _write_auth_agent_script(
    script_path: Path,
    *,
    advertise_logout: bool = True,
    capture_log_path: Path | None = None,
) -> Path:
    """Write a deterministic ACP stdio agent supporting all 4 auth methods + terminal."""
    script_body = textwrap.dedent(
        f"""
        import json
        import sys
        from pathlib import Path

        ADVERTISE_LOGOUT = {advertise_logout!r}
        CAPTURE_LOG = {str(capture_log_path)!r} if {capture_log_path is not None!r} else None
        authenticated = False

        AUTH_METHODS = [
            {{
                "id": "oauth-personal",
                "name": "Log in with Google",
                "description": "Log in with your Google account",
            }},
            {{
                "id": "oauth-business",
                "name": "Log in with Gemini Enterprise",
                "description": "Log in with your Gemini Enterprise account",
            }},
            {{
                "id": "gemini-api-key",
                "name": "Gemini API key",
                "description": "Use an API key with Gemini Developer API",
            }},
            {{
                "id": "agent-platform",
                "name": "Gemini Enterprise Agent Platform",
                "description": "Use Gemini Enterprise Agent Platform with ADC",
            }},
            {{
                "id": "terminal-login",
                "name": "Log in from the terminal",
                "type": "terminal",
                "args": ["--login"],
            }},
        ]

        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if CAPTURE_LOG is not None:
                with Path(CAPTURE_LOG).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(msg) + "\\n")

            method = msg.get("method")
            req_id = msg.get("id")
            if method == "initialize":
                caps = {{"sessionCapabilities": {{"list": {{}}}}}}
                if ADVERTISE_LOGOUT:
                    caps["auth"] = {{"logout": {{}}}}
                reply = {{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{
                        "protocolVersion": 1,
                        "agentCapabilities": caps,
                        "agentInfo": {{"name": "Auth Fake Agent", "version": "1.0.0"}},
                        "authMethods": AUTH_METHODS,
                    }},
                }}
            elif method == "session/list":
                if not authenticated:
                    reply = {{
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {{"code": -32000, "message": "Authentication required"}},
                    }}
                else:
                    reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{"sessions": []}}}}
            elif method == "authenticate":
                params = msg.get("params") or {{}}
                method_id = params.get("methodId")
                meta = params.get("_meta") or {{}}
                if method_id == "gemini-api-key" and not meta.get("apiKey"):
                    reply = {{
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {{"code": -32602, "message": "Missing _meta.apiKey"}},
                    }}
                else:
                    authenticated = True
                    reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{}}}}
            elif method == "logout":
                authenticated = False
                reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{}}}}
            else:
                reply = {{"jsonrpc": "2.0", "id": req_id, "result": {{}}}}

            sys.stdout.write(json.dumps(reply) + "\\n")
            sys.stdout.flush()
        """
    ).lstrip()
    script_path.write_text(script_body, encoding="utf-8")
    return script_path


def _make_auth_client(
    tmp_path: Path,
    script_path: Path,
    *,
    auth_terminal: RelayOnlyPolicy = RelayOnlyPolicy.DISABLED,
) -> TestClient:
    """Create a ``TestClient`` wired to ``script_path``."""
    api_cfg = AcpServerConfig(
        agent=AgentConfig(
            command=(sys.executable, "-u", str(script_path)),
            workspace_root=tmp_path,
        ),
        capabilities=CapabilityPolicyConfig(auth_terminal=auth_terminal),
    )
    app_cfg = AppConfig(db_path=tmp_path / "auth_test.db")
    return TestClient(
        ApiServerRunner(api_cfg, app_cfg).create_app(),
        base_url="http://127.0.0.1:8000",
    )


def _init_msg(req_id: int = 1) -> dict[str, Any]:
    """Build baseline ``initialize`` request."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "ui", "version": "0.2.0"},
        },
    }


def test_acp_oauth_personal_authenticate_and_logout_lifecycle(tmp_path: Path) -> None:
    """Probes ``-32000``, authenticates ``oauth-personal``, lists sessions, and logs out."""
    script = _write_auth_agent_script(tmp_path / "auth_agent.py")
    with (
        _make_auth_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        init_resp = (ws.send_json(_init_msg(1)), ws.receive_json())[1]
        method_ids = [m["id"] for m in init_resp["result"]["authMethods"]]
        assert "oauth-personal" in method_ids
        assert "oauth-business" in method_ids
        assert "gemini-api-key" in method_ids
        assert "agent-platform" in method_ids

        ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "session/list", "params": {}})
        unauth_resp = ws.receive_json()
        assert unauth_resp["error"]["code"] == ACP_AUTH_REQUIRED

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "authenticate",
                "params": {"methodId": "oauth-personal"},
            }
        )
        assert ws.receive_json() == {"jsonrpc": "2.0", "id": 3, "result": {}}

        ws.send_json({"jsonrpc": "2.0", "id": 4, "method": "session/list", "params": {}})
        assert ws.receive_json() == {"jsonrpc": "2.0", "id": 4, "result": {"sessions": []}}

        ws.send_json({"jsonrpc": "2.0", "id": 5, "method": "logout", "params": {}})
        assert ws.receive_json() == {"jsonrpc": "2.0", "id": 5, "result": {}}

        ws.send_json({"jsonrpc": "2.0", "id": 6, "method": "session/list", "params": {}})
        assert ws.receive_json()["error"]["code"] == ACP_AUTH_REQUIRED


def test_acp_all_four_methods_and_scoped_api_key_redaction(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Allows all 4 agent methods, scopes ``_meta.apiKey``, and redacts it in stderr logs."""
    capture_log = tmp_path / "agent_received.jsonl"
    script = _write_auth_agent_script(
        tmp_path / "four_methods_agent.py",
        capture_log_path=capture_log,
    )
    root_logger = logging.getLogger(ROOT_LOGGER_NAME)
    reset_logging()
    root_logger.addHandler(logging.NullHandler())

    try:
        AcpLoggingConfigurator(LoggingConfig(format=LogFormat.PRETTY, level="INFO")).configure()
        with (
            _make_auth_client(tmp_path, script) as client,
            client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
        ):
            ws.send_json(_init_msg(1))
            ws.receive_json()

            for idx, method_id in enumerate(
                ("oauth-personal", "oauth-business", "agent-platform"),
                start=10,
            ):
                ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": idx,
                        "method": "authenticate",
                        "params": {
                            "methodId": method_id,
                            "_meta": {"apiKey": "should-be-stripped", "authorization": "secret"},
                        },
                    }
                )
                assert ws.receive_json() == {"jsonrpc": "2.0", "id": idx, "result": {}}

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "authenticate",
                    "params": {
                        "methodId": "gemini-api-key",
                        "_meta": {"apiKey": "AIza-super-secret-key", "authorization": "strip"},
                    },
                }
            )
            assert ws.receive_json() == {"jsonrpc": "2.0", "id": 20, "result": {}}

        stderr_text = capsys.readouterr().err
        assert '"apiKey": "[REDACTED]"' in stderr_text
        assert "AIza-super-secret-key" not in stderr_text
        assert "should-be-stripped" not in stderr_text

        forwarded = [
            json.loads(line)
            for line in capture_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        auth_calls = {item["params"]["methodId"]: item["params"] for item in forwarded[1:]}
        assert "_meta" not in auth_calls["oauth-personal"]
        assert "_meta" not in auth_calls["oauth-business"]
        assert "_meta" not in auth_calls["agent-platform"]
        assert auth_calls["gemini-api-key"]["_meta"] == {"apiKey": "AIza-super-secret-key"}
    finally:
        reset_logging()


def test_acp_rejects_unknown_or_terminal_auth_method_and_unsupported_logout(
    tmp_path: Path,
) -> None:
    """Bridge rejects unknown/terminal ``methodId`` (``-32602``) and unadvertised ``logout``."""
    capture_log = tmp_path / "guard_received.jsonl"
    script = _write_auth_agent_script(
        tmp_path / "no_logout_agent.py",
        advertise_logout=False,
        capture_log_path=capture_log,
    )

    with (
        _make_auth_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(_init_msg(1))
        ws.receive_json()

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "authenticate",
                "params": {"methodId": "not-advertised"},
            }
        )
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "authenticate",
                "params": {"methodId": "terminal-login"},
            }
        )
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS

        ws.send_json({"jsonrpc": "2.0", "id": 4, "method": "logout", "params": {}})
        assert ws.receive_json()["error"]["code"] == JSONRPC_METHOD_NOT_FOUND

    forwarded = [
        json.loads(line)
        for line in capture_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [item["method"] for item in forwarded] == ["initialize"]
