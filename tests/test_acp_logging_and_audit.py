"""Tests for dual-mode ACP logging (NDJSON/pretty), correlation context, redaction, & audit."""

import json
import logging
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.models.config import (
    AcpServerConfig,
    AgentConfig,
    CapabilityPolicy,
    CapabilityPolicyConfig,
    LogFormat,
    LoggingConfig,
)
from api.services.logging import AcpAuditLogger, AcpLogContext, AcpLoggingConfigurator
from api.services.server import ApiServerRunner
from api.utils.formatting import format_ndjson_log_line, redact_for_log
from core.models.config import AppConfig
from core.utils.logging import ROOT_LOGGER_NAME, reset_logging

VALID_WS_HEADERS: dict[str, str] = {
    "host": "127.0.0.1:8000",
    "origin": "http://localhost:5173",
}


def _write_logging_agent(script_path: Path) -> Path:
    """Write a minimal ACP stdio agent for structured log & correlation tests."""
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
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {"sessionCapabilities": {"list": {}}},
                        "agentInfo": {"name": "Log Agent", "version": "1.0.0"},
                        "authMethods": [],
                    },
                }
            elif method == "session/new":
                reply = {"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": "sess_log_1"}}
            else:
                reply = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            sys.stdout.write(json.dumps(reply) + "\\n")
            sys.stdout.flush()
        """
    ).lstrip()
    script_path.write_text(script_body, encoding="utf-8")
    return script_path


def test_ndjson_production_logging_correlation_latency_and_log_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PROD ``json`` format emits valid NDJSON with ``traceparent`` and ``duration_ms``."""
    script = _write_logging_agent(tmp_path / "ndjson_agent.py")
    log_file = tmp_path / "acp.ndjson"
    cfg = AcpServerConfig(
        agent=AgentConfig(command=(sys.executable, "-u", str(script)), workspace_root=tmp_path),
        logging=LoggingConfig(
            format=LogFormat.JSON,
            level="INFO",
            log_file=log_file,
            log_payloads=False,
        ),
    )
    app = ApiServerRunner(cfg, AppConfig(db_path=tmp_path / "ndjson_log.db")).create_app()

    reset_logging()
    logging.getLogger(ROOT_LOGGER_NAME).addHandler(logging.NullHandler())
    try:
        AcpLoggingConfigurator(cfg.logging).configure()
        trace_id = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        with (
            TestClient(app, base_url="http://127.0.0.1:8000") as client,
            client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
        ):
            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "_meta": {"traceparent": trace_id, "token": "hide-init-token"},
                    },
                }
            )
            assert ws.receive_json()["id"] == 1

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {
                        "cwd": str(tmp_path.resolve()),
                        "_meta": {"traceparent": trace_id},
                    },
                }
            )
            assert ws.receive_json()["result"]["sessionId"] == "sess_log_1"

        stderr_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
        file_lines = [
            line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert stderr_lines
        assert len(file_lines) == len(stderr_lines)

        parsed = [json.loads(line) for line in file_lines]
        assert all(item["timestamp"].endswith("Z") for item in parsed)
        assert all("hide-init-token" not in line for line in file_lines)
        assert any(item.get("traceparent") == trace_id for item in parsed)
        assert any(isinstance(item.get("duration_ms"), float) for item in parsed)
        assert any(
            item.get("audit") is True and item.get("event") == "ws_upgrade_accepted"
            for item in parsed
        )
    finally:
        reset_logging()


def test_deep_redaction_covers_headers_env_urls_and_nested_secrets() -> None:
    """Deep redaction scrubs credentials, URLs, and MCP/terminal ``env`` & ``headers``."""
    raw: dict[str, Any] = {
        "apiKey": "raw-api-key",
        "Authorization": "Bearer top-secret",
        "cookie": "sid=abc",
        "token": "tok-val",
        "mcpServers": [
            {
                "type": "http",
                "name": "srv",
                "url": "https://user:pass123@mcp.example.com/sse",
                "headers": [{"name": "X-Api-Key", "value": "hdr-secret"}],
            }
        ],
        "env": [{"name": "DB_PASSWORD", "value": "env-secret"}],
    }
    redacted = redact_for_log(raw)
    assert redacted["apiKey"] == "[REDACTED]"
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["cookie"] == "[REDACTED]"
    assert redacted["token"] == "[REDACTED]"
    assert redacted["mcpServers"][0]["url"] == "https://[REDACTED]@mcp.example.com/sse"
    assert redacted["mcpServers"][0]["headers"] == [
        {"name": "X-Api-Key", "value": "[REDACTED]"}
    ]
    assert redacted["env"] == [{"name": "DB_PASSWORD", "value": "[REDACTED]"}]

    ndjson_line = format_ndjson_log_line(raw)
    secrets_list = (
        "raw-api-key",
        "top-secret",
        "sid=abc",
        "tok-val",
        "pass123",
        "hdr-secret",
        "env-secret",
    )
    for secret in secrets_list:
        assert secret not in ndjson_line


def test_audit_logger_emits_security_events_and_pretty_format_tags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``AcpAuditLogger`` records structured events while ``AcpLogContext`` binds scope."""
    script = _write_logging_agent(tmp_path / "audit_agent.py")
    cfg = AcpServerConfig(
        agent=AgentConfig(command=(sys.executable, "-u", str(script)), workspace_root=tmp_path),
        capabilities=CapabilityPolicyConfig(fs_read=CapabilityPolicy.DISABLED),
        logging=LoggingConfig(format=LogFormat.JSON, level="INFO", log_audit_events=True),
    )
    app = ApiServerRunner(cfg, AppConfig(db_path=tmp_path / "audit_log.db")).create_app()

    reset_logging()
    logging.getLogger(ROOT_LOGGER_NAME).addHandler(logging.NullHandler())
    try:
        AcpLoggingConfigurator(cfg.logging).configure()
        audit = AcpAuditLogger(enabled=True)
        with AcpLogContext.bind(
            connection_id="c_test",
            session_id="s_test",
            request_id=9,
            method="fs/read_text_file",
        ):
            audit.record(
                "sensitive_write_blocked",
                outcome="blocked",
                details={"path": "/repo/.git/hooks/pre-commit"},
            )

        with (
            TestClient(app, base_url="http://127.0.0.1:8000") as client,
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                "/acp",
                headers={"host": "127.0.0.1:8000", "origin": "https://evil.example"},
            ),
        ):
            pass

        err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
        records = [json.loads(line) for line in err_lines]
        blocked = next(r for r in records if r.get("event") == "sensitive_write_blocked")
        assert blocked["level"] == "WARNING"
        assert blocked["connection_id"] == "c_test"
        assert blocked["session_id"] == "s_test"
        assert blocked["request_id"] == 9
        assert blocked["outcome"] == "blocked"
        assert any(r.get("event") == "ws_upgrade_rejected" for r in records)
    finally:
        reset_logging()
