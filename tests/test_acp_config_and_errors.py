"""Tests for 3-layer ACP server configuration, fail-closed defaults, & JSON-RPC error matrix."""

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.models.config import (
    AcpServerConfig,
    AgentConfig,
    CapabilityPolicy,
    CapabilityPolicyConfig,
    LimitsConfig,
    LogFormat,
    LoggingConfig,
    RelayOnlyPolicy,
    TransportConfig,
)
from api.models.jsonrpc import (
    ACP_AUTH_REQUIRED,
    ACP_REQUEST_CANCELLED,
    ACP_RESOURCE_NOT_FOUND,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
)
from api.services.server import ApiServerRunner
from api.utils.config_loader import resolve_acp_server_config
from api.utils.errors import exception_to_jsonrpc_frame
from core.models.config import AppConfig
from core.models.exceptions import AcpConfigError, AcpInternalError

VALID_WS_HEADERS: dict[str, str] = {
    "host": "127.0.0.1:8000",
    "origin": "http://localhost:5173",
}


def _write_error_matrix_agent(script_path: Path) -> Path:
    """Write a stdio fake agent returning ACP errors & supporting cancellation."""
    script_body = textwrap.dedent(
        """
        import json
        import sys
        import time

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
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "sessionCapabilities": {"list": {}, "resume": {}, "close": {}},
                        },
                        "agentInfo": {"name": "Error Matrix Agent", "version": "1.0.0"},
                        "authMethods": [],
                    },
                }
            elif method == "session/list":
                reply = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Authentication required"},
                }
            elif method == "session/resume":
                reply = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32002, "message": "Session not found"},
                }
            elif method == "session/new":
                reply = {"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": "s_err"}}
            elif method == "session/prompt":
                cancel_seen = False
                for inner in sys.stdin:
                    inner_s = inner.strip()
                    if not inner_s:
                        continue
                    imsg = json.loads(inner_s)
                    if imsg.get("method") in ("session/cancel", "$/cancel_request"):
                        cancel_seen = True
                        break
                code = -32800 if cancel_seen else -32603
                reply = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": code, "message": "Cancelled" if cancel_seen else "Boom"},
                }
            else:
                reply = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            sys.stdout.write(json.dumps(reply) + "\\n")
            sys.stdout.flush()
        """
    ).lstrip()
    script_path.write_text(script_body, encoding="utf-8")
    return script_path


def test_fail_closed_defaults_and_three_layer_config_precedence(tmp_path: Path) -> None:
    """Defaults fail closed and resolve with TOML -> ``CBI_ACP_*`` env -> CLI precedence."""
    default_cfg = resolve_acp_server_config()
    assert default_cfg.transport.host == "127.0.0.1"
    assert default_cfg.transport.allow_non_loopback is False
    assert default_cfg.transport.effective_allow_missing_origin is True
    assert default_cfg.capabilities.fs_read == CapabilityPolicy.DISABLED
    assert default_cfg.capabilities.fs_write == CapabilityPolicy.DISABLED
    assert default_cfg.capabilities.terminal == CapabilityPolicy.DISABLED
    assert default_cfg.capabilities.auth_terminal == RelayOnlyPolicy.DISABLED
    assert default_cfg.capabilities.fs_allow_sensitive_writes is False
    assert default_cfg.capabilities.terminal_allow_dangerous_env is False
    assert default_cfg.capabilities.terminal_allow_unrestricted_commands is False
    assert default_cfg.capabilities.elicitation_allow_http_url is False
    assert default_cfg.mcp.allow_client_stdio is False
    assert default_cfg.mcp.allow_insecure_http_urls is False

    toml_file = tmp_path / "acp.toml"
    toml_file.write_text(
        textwrap.dedent(
            """
            [transport]
            port = 8100
            max_connections = 6

            [capabilities]
            fs_read = "relay"

            [mcp]
            allowed_stdio_commands = ["/usr/bin/env"]

            [logging]
            format = "json"
            level = "WARNING"
            """
        ),
        encoding="utf-8",
    )

    env = {
        "CBI_ACP_PORT": "8200",
        "CBI_ACP__CAPABILITIES__FS_READ": "host",
        "CBI_ACP__MCP__ALLOWED_STDIO_COMMANDS": '["/bin/echo", "/usr/bin/python3"]',
        "CBI_ACP_LOG_LEVEL": "INFO",
    }
    cli_overrides: dict[str, Any] = {
        "transport": {"port": 8300},
        "logging": {"format": "pretty"},
    }
    resolved = resolve_acp_server_config(
        config_file=toml_file,
        environ=env,
        cli_overrides=cli_overrides,
    )
    assert resolved.transport.port == 8300
    assert resolved.transport.max_connections == 6
    assert resolved.capabilities.fs_read == CapabilityPolicy.HOST
    assert resolved.mcp.allowed_stdio_commands == ("/bin/echo", "/usr/bin/python3")
    assert resolved.logging.format == LogFormat.PRETTY
    assert resolved.logging.level == "INFO"


def test_invalid_configs_fail_fast_with_acp_config_error(tmp_path: Path) -> None:
    """Malformed TOML, missing host auth, and unrestricted host terminals raise errors."""
    missing = tmp_path / "missing.toml"
    with pytest.raises(AcpConfigError, match="Failed reading ACP config"):
        resolve_acp_server_config(config_file=missing)

    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text("[transport\nport = ", encoding="utf-8")
    with pytest.raises(AcpConfigError, match="Malformed TOML"):
        resolve_acp_server_config(config_file=bad_toml)

    with pytest.raises(AcpConfigError, match="non-loopback"):
        resolve_acp_server_config(cli_overrides={"transport": {"host": "0.0.0.0"}})

    with pytest.raises(AcpConfigError, match="auth_token"):
        resolve_acp_server_config(
            cli_overrides={"transport": {"host": "0.0.0.0", "allow_non_loopback": True}}
        )

    with pytest.raises(AcpConfigError, match="terminal_allowed_commands"):
        resolve_acp_server_config(cli_overrides={"capabilities": {"terminal": "host"}})


def test_internal_error_redacts_detail_in_prod_json_and_exposes_in_dev_debug() -> None:
    """``AcpInternalError`` hides internal message in JSON PROD but reveals it in DEV DEBUG."""
    exc = AcpInternalError(
        "Database password=supersecret leaked",
        wire_data={"apiKey": "secret-123", "component": "gateway"},
    )
    prod_frame = exception_to_jsonrpc_frame(exc, 11, is_notification=False, debug=False)
    assert prod_frame is not None
    prod_body = json.loads(prod_frame)
    assert prod_body["error"]["code"] == JSONRPC_INTERNAL_ERROR
    assert prod_body["error"]["message"] == "Internal error"
    assert "data" not in prod_body["error"]

    dev_frame = exception_to_jsonrpc_frame(exc, 11, is_notification=False, debug=True)
    assert dev_frame is not None
    dev_body = json.loads(dev_frame)
    assert dev_body["error"]["code"] == JSONRPC_INTERNAL_ERROR
    assert "Database password=supersecret leaked" in dev_body["error"]["message"]
    assert dev_body["error"]["data"] == {"apiKey": "[REDACTED]", "component": "gateway"}

    assert exception_to_jsonrpc_frame(exc, None, is_notification=True, debug=True) is None


def test_full_jsonrpc_and_acp_error_code_matrix_and_notification_silence(tmp_path: Path) -> None:
    """Exercises every JSON-RPC/ACP error code and ensures notifications never elicit replies."""
    script = _write_error_matrix_agent(tmp_path / "err_agent.py")
    cfg = AcpServerConfig(
        transport=TransportConfig(),
        agent=AgentConfig(command=(sys.executable, "-u", str(script)), workspace_root=tmp_path),
        capabilities=CapabilityPolicyConfig(),
        limits=LimitsConfig(),
        logging=LoggingConfig(format=LogFormat.JSON, level="INFO"),
    )
    app = ApiServerRunner(cfg, AppConfig(db_path=tmp_path / "err_matrix.db")).create_app()

    with (
        TestClient(app, base_url="http://127.0.0.1:8000") as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_text("{bad-json")
        assert ws.receive_json()["error"]["code"] == JSONRPC_PARSE_ERROR

        ws.send_json([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}])
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_REQUEST

        # Invalid pre-init notification produces no error response; verified via next request ID.
        ws.send_json({"jsonrpc": "2.0", "method": "unknown/before_init", "params": {}})

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {}},
            }
        )
        assert ws.receive_json()["id"] == 1

        # Unknown & malformed notifications must remain strictly silent per JSON-RPC 2.0.
        ws.send_json({"jsonrpc": "2.0", "method": "unrecognized/notification", "params": {}})
        ws.send_json({"jsonrpc": "2.0", "method": "session/cancel", "params": {"bad": True}})

        ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "no_such_method", "params": {}})
        err_missing = ws.receive_json()
        assert err_missing["id"] == 2
        assert err_missing["error"]["code"] == JSONRPC_METHOD_NOT_FOUND

        ws.send_json(
            {"jsonrpc": "2.0", "id": 3, "method": "session/new", "params": {"cwd": "relative"}}
        )
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS

        ws.send_json({"jsonrpc": "2.0", "id": 4, "method": "session/list", "params": {}})
        assert ws.receive_json()["error"]["code"] == ACP_AUTH_REQUIRED

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "session/resume",
                "params": {"sessionId": "missing", "cwd": str(tmp_path.resolve())},
            }
        )
        assert ws.receive_json()["error"]["code"] == ACP_RESOURCE_NOT_FOUND

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "session/new",
                "params": {"cwd": str(tmp_path.resolve())},
            }
        )
        sid = ws.receive_json()["result"]["sessionId"]

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]},
            }
        )
        ws.send_json(
            {"jsonrpc": "2.0", "method": "$/cancel_request", "params": {"requestId": 7}}
        )
        cancelled_reply = ws.receive_json()
        assert cancelled_reply["id"] == 7
        assert cancelled_reply["error"]["code"] == ACP_REQUEST_CANCELLED
