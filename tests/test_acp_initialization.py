"""Security, protocol-state, and lifecycle tests for FastAPI ``/acp`` initialization."""

import json
import logging
import os
import shutil
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.models.acp import (
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_PARSE_ERROR,
)
from api.models.config import DEFAULT_AGY_APP_DATA_DIR, ApiServerConfig
from api.services.server import ApiServerRunner
from api.utils.formatting import configure_api_terminal_logging
from api.utils.security import build_agy_acp_command
from core.models.config import AppConfig
from core.models.exceptions import AcpError
from core.utils.logging import ROOT_LOGGER_NAME, reset_logging

VALID_ORIGIN_HEADERS: dict[str, str] = {
    "host": "127.0.0.1:8000",
    "origin": "http://localhost:5173",
}


def _write_fake_acp_script(
    script_path: Path,
    *,
    protocol_version: int = 1,
    capture_path: Path | None = None,
    pid_path: Path | None = None,
) -> Path:
    """Create a deterministic stdio NDJSON ACP agent test script."""
    script_body = textwrap.dedent(
        f"""
        import json
        import os
        import sys
        from pathlib import Path

        CAPTURE = {str(capture_path)!r} if {capture_path is not None!r} else None
        PID_FILE = {str(pid_path)!r} if {pid_path is not None!r} else None
        if PID_FILE is not None:
            Path(PID_FILE).write_text(str(os.getpid()), encoding="utf-8")

        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            msg = json.loads(line)
            method = msg.get("method")
            req_id = msg.get("id")
            if method == "initialize":
                if CAPTURE is not None:
                    Path(CAPTURE).write_text(json.dumps(msg), encoding="utf-8")
                reply = {{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{
                        "protocolVersion": {protocol_version},
                        "agentCapabilities": {{
                            "loadSession": True,
                            "load_session": True,
                            "promptCapabilities": {{
                                "image": True,
                                "audio": False,
                                "embeddedContext": True,
                            }},
                            "sessionCapabilities": {{"list": {{}}}},
                            "session_capabilities": {{"list": {{}}}},
                            "auth": {{"logout": {{}}}},
                            "_meta": {{"antigravity": {{"tier": True}}}},
                        }},
                        "agentInfo": {{
                            "name": "Fake ACP Server",
                            "title": "Fake ACP",
                            "version": "0.1.0",
                        }},
                        "authMethods": [
                            {{"id": "oauth-personal", "name": "Log in with Google"}},
                        ],
                    }},
                }}
            else:
                reply = {{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{"echoMethod": method}},
                }}
            sys.stdout.write(json.dumps(reply) + "\\n")
            sys.stdout.flush()
        """
    ).lstrip()
    script_path.write_text(script_body, encoding="utf-8")
    return script_path


def _make_test_client(
    tmp_path: Path,
    script_path: Path,
    *,
    max_connections: int = 4,
    initialize_timeout_seconds: float = 5.0,
) -> TestClient:
    """Build a ``TestClient`` backed by a temporary DB and fake ACP subprocess command."""
    api_cfg = ApiServerConfig(
        agy_command=(sys.executable, "-u", str(script_path)),
        max_connections=max_connections,
        initialize_timeout_seconds=initialize_timeout_seconds,
        workspace_root=tmp_path,
    )
    app_cfg = AppConfig(db_path=tmp_path / "api_test.db")
    app = ApiServerRunner(api_cfg, app_cfg).create_app()
    return TestClient(app, base_url="http://127.0.0.1:8000")


def _initialize_payload(
    *,
    req_id: int | str = 1,
    protocol_version: Any = 1,
    client_capabilities: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct an ACP JSON-RPC 2.0 ``initialize`` request dict."""
    params: dict[str, Any] = {
        "protocolVersion": protocol_version,
        "clientCapabilities": client_capabilities if client_capabilities is not None else {},
        "clientInfo": {
            "name": "test-ui",
            "title": "Test UI",
            "version": "1.0.0",
        },
    }
    if meta is not None:
        params["_meta"] = meta
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "initialize",
        "params": params,
    }


def _assert_pid_exited(pid: int, timeout_seconds: float = 2.0) -> None:
    """Poll briefly until child process ``pid`` is no longer alive."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"Subprocess pid={pid} remained alive after socket close.")


def test_healthz_and_non_loopback_bind_guard(tmp_path: Path) -> None:
    """``/healthz`` reports ACP v1 readiness and non-loopback bind fails closed."""
    with pytest.raises(AcpError, match="non-loopback"):
        ApiServerRunner(
            ApiServerConfig(host="0.0.0.0"),
            AppConfig(db_path=tmp_path / "blocked.db"),
        ).create_app()

    allowed_app = ApiServerRunner(
        ApiServerConfig(host="0.0.0.0", allow_non_loopback=True),
        AppConfig(db_path=tmp_path / "allowed.db"),
    ).create_app()
    with TestClient(allowed_app, base_url="http://127.0.0.1:8000") as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["supported_acp_versions"] == [1]
        assert body["agy_app_data_dir"] == DEFAULT_AGY_APP_DATA_DIR
        assert body["agy_profile_dir"] == "~/.gemini/antigravity"

    assert build_agy_acp_command(ApiServerConfig()) == (
        "agy",
        "acp",
        "--stdio",
        "--sandbox",
        "--app-data-dir",
        "antigravity",
    )
    isolated_gemini = str((tmp_path / "cbi-gemini").resolve())
    assert build_agy_acp_command(
        ApiServerConfig(agy_gemini_dir=isolated_gemini, agy_app_data_dir="acp")
    ) == (
        "agy",
        "acp",
        "--stdio",
        "--sandbox",
        "--gemini-dir",
        isolated_gemini,
        "--app-data-dir",
        "acp",
    )
    for bad_dir in ("/abs/path", "../escape", ""):
        with pytest.raises(AcpError):
            build_agy_acp_command(ApiServerConfig(agy_app_data_dir=bad_dir))
    for bad_gemini in (".relative-gemini", f"{isolated_gemini}/../escape", ""):
        with pytest.raises(AcpError):
            build_agy_acp_command(ApiServerConfig(agy_gemini_dir=bad_gemini))


def test_acp_initialize_happy_path_clamping_and_meta_scrubbing(tmp_path: Path) -> None:
    """Happy-path ``initialize`` clamps capabilities, scrubs ``_meta``, and relays post-init."""
    capture_path = tmp_path / "captured_init.json"
    pid_path = tmp_path / "agent.pid"
    script = _write_fake_acp_script(
        tmp_path / "fake_acp.py",
        capture_path=capture_path,
        pid_path=pid_path,
    )

    with _make_test_client(tmp_path, script) as client:
        with client.websocket_connect("/acp", headers=VALID_ORIGIN_HEADERS) as ws:
            ws.send_json(
                _initialize_payload(
                    req_id=42,
                    client_capabilities={
                        "auth": {"terminal": True},
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": True,
                        "plan": {},
                    },
                    meta={
                        "authorization": "Bearer top-secret",
                        "traceparent": "00-trace-id",
                        "nested": {"token": "abc", "uiTheme": "dark"},
                    },
                )
            )
            init_resp = ws.receive_json()
            assert init_resp["id"] == 42
            result = init_resp["result"]
            assert result["protocolVersion"] == 1
            assert result["agentInfo"]["name"] == "Fake ACP Server"
            assert result["authMethods"][0]["id"] == "oauth-personal"

            ws.send_json({"jsonrpc": "2.0", "id": 43, "method": "session/list", "params": {}})
            list_resp = ws.receive_json()
            assert list_resp == {
                "jsonrpc": "2.0",
                "id": 43,
                "result": {"echoMethod": "session/list"},
            }

        captured = json.loads(capture_path.read_text(encoding="utf-8"))
        sent_caps = captured["params"]["clientCapabilities"]
        assert sent_caps["auth"] == {"terminal": False}
        assert sent_caps["fs"] == {"readTextFile": False, "writeTextFile": False}
        assert sent_caps["terminal"] is False
        assert sent_caps["plan"] == {}
        assert captured["params"]["_meta"] == {"nested": {"uiTheme": "dark"}}

        child_pid = int(pid_path.read_text(encoding="utf-8"))
        _assert_pid_exited(child_pid)


def test_acp_rejects_untrusted_origin_and_dns_rebinding_host_before_accept(tmp_path: Path) -> None:
    """Missing/untrusted ``Origin`` and non-loopback ``Host`` fail pre-accept with ``1008``."""
    capture_path = tmp_path / "never_spawned.json"
    script = _write_fake_acp_script(tmp_path / "fake_acp.py", capture_path=capture_path)

    bad_header_cases = [
        {"host": "127.0.0.1:8000"},
        {"host": "127.0.0.1:8000", "origin": "https://evil.example"},
        {"host": "127.0.0.1:8000", "origin": "null"},
        {"origin": "http://localhost:5173", "host": "attacker.example:8000"},
    ]
    with _make_test_client(tmp_path, script) as client:
        for headers in bad_header_cases:
            with (
                pytest.raises(WebSocketDisconnect) as exc_info,
                client.websocket_connect("/acp", headers=headers),
            ):
                pytest.fail("Connection should have been rejected before accept.")
            assert exc_info.value.code == 1008
    assert not capture_path.exists()


def test_acp_enforces_state_machine_jsonrpc_errors_and_connection_cap(tmp_path: Path) -> None:
    """Rejects pre-init methods, batches, bad params, duplicates, and excess sockets."""
    capture_path = tmp_path / "state_capture.json"
    script = _write_fake_acp_script(tmp_path / "fake_acp.py", capture_path=capture_path)

    with (
        _make_test_client(tmp_path, script, max_connections=1) as client,
        client.websocket_connect("/acp", headers=VALID_ORIGIN_HEADERS) as ws,
    ):
        ws.send_text("{not-json")
        assert ws.receive_json()["error"]["code"] == JSONRPC_PARSE_ERROR

        ws.send_json([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}])
        batch_err = ws.receive_json()
        assert batch_err["id"] is None
        assert batch_err["error"]["code"] == JSONRPC_INVALID_REQUEST

        ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_REQUEST
        assert not capture_path.exists()

        ws.send_json(_initialize_payload(req_id=3, protocol_version=True))
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS
        assert not capture_path.exists()

        ws.send_json(_initialize_payload(req_id=4, protocol_version=0))
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS

        ws.send_json(_initialize_payload(req_id=5, protocol_version=1))
        ok_resp = ws.receive_json()
        assert ok_resp["id"] == 5
        assert ok_resp["result"]["protocolVersion"] == 1

        ws.send_json(_initialize_payload(req_id=6, protocol_version=1))
        dup_err = ws.receive_json()
        assert dup_err["id"] == 6
        assert dup_err["error"]["code"] == JSONRPC_INVALID_REQUEST

        with (
            pytest.raises(WebSocketDisconnect) as busy_exc,
            client.websocket_connect("/acp", headers=VALID_ORIGIN_HEADERS),
        ):
            pytest.fail("Second concurrent connection should be rejected.")
        assert busy_exc.value.code == 1013


def test_acp_rejects_unsupported_agent_protocol_version_and_cleans_up(tmp_path: Path) -> None:
    """Incompatible agent ``protocolVersion`` closes socket with ``1002`` and reaps agent."""
    pid_path = tmp_path / "bad_version.pid"
    script = _write_fake_acp_script(
        tmp_path / "bad_ver_acp.py",
        protocol_version=99,
        pid_path=pid_path,
    )

    with (
        _make_test_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_ORIGIN_HEADERS) as ws,
    ):
        ws.send_json(_initialize_payload(req_id=9, protocol_version=1))
        err_frame = ws.receive_json()
        assert err_frame["id"] == 9
        assert err_frame["error"]["code"] == JSONRPC_INVALID_PARAMS
        with pytest.raises(WebSocketDisconnect) as close_exc:
            ws.receive_text()
        assert close_exc.value.code == 1002

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    _assert_pid_exited(child_pid)


def test_acp_terminal_logging_indents_json_and_redacts_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``configure_api_terminal_logging`` emits indented ACP JSON to stderr without secrets."""
    script = _write_fake_acp_script(tmp_path / "log_acp.py")
    root_logger = logging.getLogger(ROOT_LOGGER_NAME)
    reset_logging()
    root_logger.addHandler(logging.NullHandler())

    try:
        configure_api_terminal_logging(debug=False)
        with (
            _make_test_client(tmp_path, script) as client,
            client.websocket_connect("/acp", headers=VALID_ORIGIN_HEADERS) as ws,
        ):
            ws.send_json(
                _initialize_payload(
                    req_id=7,
                    meta={"authorization": "Bearer super-secret-token", "theme": "dark"},
                )
            )
            assert ws.receive_json()["id"] == 7

        err_out = capsys.readouterr().err
        assert "UI -> Bridge (request #7 · initialize)\n  {\n    \"jsonrpc\": \"2.0\"" in err_out
        assert "Bridge -> agy (request #7 · clamped initialize)\n  {" in err_out
        assert "agy -> UI (response #7 · initialize)\n  {" in err_out
        assert "\"authorization\": \"[REDACTED]\"" in err_out
        assert "super-secret-token" not in err_out
    finally:
        reset_logging()


@pytest.mark.skipif(shutil.which("agy") is None, reason="agy CLI not installed")
def test_live_agy_acp_initialization_smoke(tmp_path: Path) -> None:
    """Smoke-test real ``agy acp --stdio --sandbox`` handshake through FastAPI bridge."""
    app = ApiServerRunner(
        ApiServerConfig(workspace_root=tmp_path),
        AppConfig(db_path=tmp_path / "live_agy.db"),
    ).create_app()
    with (
        TestClient(app, base_url="http://127.0.0.1:8000") as client,
        client.websocket_connect("/acp", headers=VALID_ORIGIN_HEADERS) as ws,
    ):
        ws.send_json(_initialize_payload(req_id=0, protocol_version=1))
        resp = ws.receive_json()
        assert resp["id"] == 0
        assert resp["result"]["protocolVersion"] == 1
        assert "Antigravity" in resp["result"]["agentInfo"]["name"]
