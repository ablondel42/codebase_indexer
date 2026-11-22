"""End-to-end ACP v1 prompt turn, permission, cancellation, update-gating, and subagent tests."""

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
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
)
from api.models.config import ApiServerConfig
from api.services.server import ApiServerRunner
from api.utils.formatting import configure_api_terminal_logging
from core.models.config import AppConfig
from core.utils.logging import ROOT_LOGGER_NAME, reset_logging

VALID_WS_HEADERS: dict[str, str] = {
    "host": "127.0.0.1:8000",
    "origin": "http://localhost:5173",
}

GATED_UPDATE_KINDS: tuple[str, ...] = (
    "subagent_update",
    "session_message",
    "session_message_chunk",
    "plan_update",
    "plan_removed",
    "notice",
    "compaction_update",
    "compaction_summary_chunk",
)


def _write_prompt_agent_script(
    script_path: Path,
    *,
    image_cap: bool = True,
    audio_cap: bool = False,
    embedded_cap: bool = True,
    capture_path: Path | None = None,
) -> Path:
    """Write a deterministic stdio ACP agent exercising prompt turns, tools, and subagents."""
    script_body = textwrap.dedent(
        f"""
        import json
        import sys
        from pathlib import Path

        CAPTURE = {str(capture_path)!r} if {capture_path is not None!r} else None
        PROMPT_CAPS = {{
            "image": {image_cap!r},
            "audio": {audio_cap!r},
            "embeddedContext": {embedded_cap!r},
        }}
        session_seq = 0

        def record_in(msg):
            if CAPTURE is not None:
                with Path(CAPTURE).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(msg) + "\\n")

        def send_out(payload):
            sys.stdout.write(json.dumps(payload) + "\\n")
            sys.stdout.flush()

        def read_next_msg():
            for raw in sys.stdin:
                stripped = raw.strip()
                if not stripped:
                    continue
                parsed = json.loads(stripped)
                record_in(parsed)
                return parsed
            return None

        def send_update(sid, update_body):
            send_out({{
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {{"sessionId": sid, "update": update_body}},
            }})

        def send_prompt_stop(req_id, reason):
            send_out({{"jsonrpc": "2.0", "id": req_id, "result": {{"stopReason": reason}}}})

        def run_permission_turn(req_id, sid):
            send_update(sid, {{
                "sessionUpdate": "plan",
                "entries": [
                    {{"content": "Inspect module", "priority": "high", "status": "pending"}},
                ],
            }})
            send_update(sid, {{
                "sessionUpdate": "agent_thought_chunk",
                "messageId": "th_1",
                "content": {{"type": "text", "text": "Checking AST..."}},
            }})
            send_update(sid, {{
                "sessionUpdate": "agent_message_chunk",
                "messageId": "msg_1",
                "content": {{"type": "text", "text": "Analyzing file..."}},
            }})
            send_update(sid, {{
                "sessionUpdate": "tool_call",
                "toolCallId": "call_001",
                "title": "Run analyzer",
                "name": "analyze_file",
                "kind": "execute",
                "status": "pending",
            }})
            send_out({{
                "jsonrpc": "2.0",
                "id": 500,
                "method": "session/request_permission",
                "params": {{
                    "sessionId": sid,
                    "toolCall": {{"toolCallId": "call_001"}},
                    "options": [
                        {{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"}},
                        {{"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}},
                    ],
                }},
            }})

            was_cancelled = False
            outcome_kind = "cancelled"
            while True:
                incoming = read_next_msg()
                if incoming is None:
                    return
                if incoming.get("method") in ("session/cancel", "$/cancel_request"):
                    was_cancelled = True
                    continue
                if incoming.get("id") == 500:
                    res = incoming.get("result") or {{}}
                    outcome_kind = (res.get("outcome") or {{}}).get("outcome", "cancelled")
                    break

            if was_cancelled or outcome_kind == "cancelled":
                send_update(sid, {{
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call_001",
                    "status": "failed",
                }})
                send_prompt_stop(req_id, "cancelled")
                return

            send_update(sid, {{
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_001",
                "status": "in_progress",
            }})
            send_update(sid, {{
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_001",
                "status": "completed",
                "content": [{{
                    "type": "content",
                    "content": {{"type": "text", "text": "No issues found."}},
                }}],
            }})
            send_update(sid, {{
                "sessionUpdate": "usage_update",
                "used": 4200,
                "size": 200000,
                "cost": {{"amount": 0.012, "currency": "USD"}},
            }})
            send_update(sid, {{
                "sessionUpdate": "agent_message_chunk",
                "messageId": "msg_2",
                "content": {{"type": "text", "text": "Analysis complete."}},
            }})
            send_prompt_stop(req_id, "end_turn")

        def run_abort_on_cancel_turn(req_id, sid):
            send_update(sid, {{
                "sessionUpdate": "agent_message_chunk",
                "messageId": "msg_slow",
                "content": {{"type": "text", "text": "Working..."}},
            }})
            while True:
                incoming = read_next_msg()
                if incoming is None:
                    return
                if incoming.get("method") in ("session/cancel", "$/cancel_request"):
                    send_out({{
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {{"code": -32800, "message": "SDK operation aborted"}},
                    }})
                    return

        def run_all_variants_turn(req_id, sid):
            for bad_id, bad_method in ((701, "fs/read_text_file"), (702, "terminal/create")):
                send_out({{
                    "jsonrpc": "2.0",
                    "id": bad_id,
                    "method": bad_method,
                    "params": {{"sessionId": sid}},
                }})
                read_next_msg()

            send_update(sid, {{
                "sessionUpdate": "subagent_update",
                "sessionId": "sess_child_1",
                "title": "Code researcher",
                "capabilities": {{"cancel": {{}}}},
            }})
            send_update("sess_child_1", {{
                "sessionUpdate": "session_message",
                "messageId": "sm_1",
                "senderSessionId": sid,
                "recipientSessionId": "sess_child_1",
            }})
            send_update("sess_child_1", {{
                "sessionUpdate": "session_message_chunk",
                "messageId": "sm_1",
                "content": {{"type": "text", "text": "Delegate check"}},
            }})
            send_update(sid, {{"sessionUpdate": "plan_update", "planId": "p1", "entries": []}})
            send_update(sid, {{"sessionUpdate": "plan_removed", "planId": "p1"}})
            send_update(sid, {{
                "sessionUpdate": "notice",
                "severity": "warning",
                "title": "MCP degraded",
                "description": "Continuing without optional server.",
            }})
            send_update(sid, {{
                "sessionUpdate": "compaction_update",
                "compactionId": "cmp_1",
                "status": "in_progress",
            }})
            send_update(sid, {{
                "sessionUpdate": "compaction_summary_chunk",
                "compactionId": "cmp_1",
                "content": {{"type": "text", "text": "## Summary\\n"}},
            }})
            send_update(sid, {{
                "sessionUpdate": "compaction_update",
                "compactionId": "cmp_1",
                "status": "completed",
            }})
            send_update(sid, {{
                "sessionUpdate": "tool_call",
                "toolCallId": "call_sub",
                "title": "Wait on child",
                "kind": "other",
                "status": "completed",
                "content": [
                    {{"type": "session", "sessionId": "sess_child_1"}},
                    {{"type": "content", "content": {{"type": "text", "text": "Done"}}}},
                ],
            }})
            send_update(
                sid,
                {{"sessionUpdate": "available_commands_update", "availableCommands": []}},
            )
            send_update(sid, {{"sessionUpdate": "current_mode_update", "currentModeId": "code"}})
            send_update(sid, {{"sessionUpdate": "config_option_update", "configOptions": []}})
            send_update(sid, {{"sessionUpdate": "session_info_update", "title": "Updated Title"}})
            send_update(sid, {{
                "sessionUpdate": "user_message_chunk",
                "messageId": "u_echo",
                "content": {{"type": "text", "text": "emit-all-variants"}},
            }})
            send_update(sid, {{
                "sessionUpdate": "agent_message_chunk",
                "messageId": "a_done",
                "content": {{"type": "text", "text": "All updates emitted."}},
            }})
            send_prompt_stop(req_id, "end_turn")

        def run_hold_permission_turn(req_id, sid):
            send_out({{
                "jsonrpc": "2.0",
                "id": 900,
                "method": "session/request_permission",
                "params": {{
                    "sessionId": sid,
                    "toolCall": {{"toolCallId": "call_hold"}},
                    "options": [
                        {{"optionId": "allow-once", "name": "Allow", "kind": "allow_once"}},
                    ],
                }},
            }})
            while True:
                incoming = read_next_msg()
                if incoming is None:
                    return
                if incoming.get("id") == 900:
                    break
            while True:
                incoming = read_next_msg()
                if incoming is None:
                    return
                if incoming.get("method") == "session/cancel":
                    send_prompt_stop(req_id, "cancelled")
                    return

        while True:
            msg = read_next_msg()
            if msg is None:
                break
            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params") or {{}}

            if method == "initialize":
                send_out({{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{
                        "protocolVersion": 1,
                        "agentCapabilities": {{
                            "loadSession": True,
                            "promptCapabilities": PROMPT_CAPS,
                            "sessionCapabilities": {{
                                "list": {{}},
                                "load": {{}},
                                "resume": {{}},
                                "close": {{}},
                                "delete": {{}},
                            }},
                        }},
                        "agentInfo": {{"name": "Prompt Turn Fake Agent", "version": "1.0.0"}},
                        "authMethods": [],
                    }},
                }})
            elif method == "session/new":
                session_seq += 1
                send_out({{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{"sessionId": f"sess_turn_{{session_seq}}"}},
                }})
            elif method in ("session/close", "session/delete", "session/load", "session/resume"):
                send_out({{"jsonrpc": "2.0", "id": req_id, "result": {{}}}})
            elif method == "session/prompt":
                sid = params["sessionId"]
                first_text = ""
                for blk in params.get("prompt", []):
                    if blk.get("type") == "text":
                        first_text = blk.get("text", "")
                        break
                if "inspect" in first_text:
                    run_permission_turn(req_id, sid)
                elif "abort-on-cancel" in first_text:
                    run_abort_on_cancel_turn(req_id, sid)
                elif "emit-all-variants" in first_text:
                    run_all_variants_turn(req_id, sid)
                elif "patch-child-nocancel" in first_text:
                    send_update(sid, {{
                        "sessionUpdate": "subagent_update",
                        "sessionId": "sess_child_1",
                        "capabilities": None,
                    }})
                    send_prompt_stop(req_id, "end_turn")
                elif "hold-permission" in first_text:
                    run_hold_permission_turn(req_id, sid)
                else:
                    send_update(sid, {{
                        "sessionUpdate": "agent_message_chunk",
                        "messageId": "msg_quick",
                        "content": {{"type": "text", "text": f"Echo: {{first_text}}"}},
                    }})
                    send_prompt_stop(req_id, "end_turn")
        """
    ).lstrip()
    script_path.write_text(script_body, encoding="utf-8")
    return script_path


def _make_prompt_client(tmp_path: Path, script_path: Path) -> TestClient:
    """Create a FastAPI ``TestClient`` wired to ``script_path``."""
    api_cfg = ApiServerConfig(
        agy_command=(sys.executable, "-u", str(script_path)),
        workspace_root=tmp_path,
    )
    app_cfg = AppConfig(db_path=tmp_path / "prompt_turn.db")
    return TestClient(
        ApiServerRunner(api_cfg, app_cfg).create_app(),
        base_url="http://127.0.0.1:8000",
    )


def _init_msg(client_capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an ``initialize`` JSON-RPC envelope with optional ``clientCapabilities``."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": client_capabilities or {},
            "clientInfo": {"name": "prompt-test-ui", "version": "0.2.0"},
        },
    }


def _read_captured_jsonl(capture_path: Path) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON records captured by the fake ACP subprocess."""
    return [
        json.loads(line)
        for line in capture_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _collect_turn_updates(ws: Any, prompt_req_id: int) -> tuple[list[str], list[dict[str, Any]]]:
    """Read streamed ``session/update`` frames until ``prompt_req_id`` completes."""
    kinds: list[str] = []
    tool_contents: list[dict[str, Any]] = []
    while True:
        msg = ws.receive_json()
        if msg.get("id") == prompt_req_id:
            return kinds, tool_contents
        upd = msg["params"]["update"]
        kinds.append(upd["sessionUpdate"])
        if upd["sessionUpdate"] == "tool_call":
            tool_contents = upd.get("content", [])


def test_prompt_turn_full_lifecycle_permission_meta_scrubbing_and_followup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Full prompt turn streams updates, validates permissions, scrubs ``_meta``, and continues."""
    capture_path = tmp_path / "full_turn_capture.jsonl"
    script = _write_prompt_agent_script(tmp_path / "full_turn_agent.py", capture_path=capture_path)
    cwd = str(tmp_path.resolve())
    large_png = "iVBORw0KGgoAAAANSUhEUg==" * 12

    root_logger = logging.getLogger(ROOT_LOGGER_NAME)
    reset_logging()
    root_logger.addHandler(logging.NullHandler())

    try:
        configure_api_terminal_logging(debug=False)
        with (
            _make_prompt_client(tmp_path, script) as client,
            client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
        ):
            ws.send_json(_init_msg())
            ws.receive_json()

            ws.send_json(
                {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": cwd}}
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
                                "text": "Please inspect main.py",
                                "annotations": {
                                    "audience": ["assistant"],
                                    "_meta": {"token": "strip-ann-secret", "hint": "focus"},
                                },
                                "_meta": {"authorization": "Bearer block-secret", "tag": "u1"},
                            },
                            {
                                "type": "resource",
                                "resource": {
                                    "uri": "file:///workspace/main.py",
                                    "mimeType": "text/x-python",
                                    "text": "def run():\n    return 1\n",
                                    "_meta": {"cookie": "strip-res-cookie"},
                                },
                            },
                            {
                                "type": "resource_link",
                                "uri": "file:///workspace/README.md",
                                "name": "README.md",
                            },
                            {
                                "type": "image",
                                "mimeType": "image/png",
                                "data": large_png,
                            },
                        ],
                        "_meta": {"api_key": "strip-top-secret", "uiTurn": 1},
                    },
                }
            )

            pre_perm_frames = [ws.receive_json() for _ in range(5)]
            assert [
                f["params"]["update"]["sessionUpdate"] for f in pre_perm_frames[:4]
            ] == ["plan", "agent_thought_chunk", "agent_message_chunk", "tool_call"]
            perm_req = pre_perm_frames[4]
            assert perm_req["id"] == 500
            assert perm_req["method"] == "session/request_permission"

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 999,
                    "result": {"outcome": {"outcome": "selected", "optionId": "allow-once"}},
                }
            )
            spoof_err = ws.receive_json()
            assert spoof_err["error"]["code"] == JSONRPC_INVALID_REQUEST

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 500,
                    "result": {"outcome": {"outcome": "selected", "optionId": "bogus-option"}},
                }
            )
            bad_opt_err = ws.receive_json()
            assert bad_opt_err["error"]["code"] == JSONRPC_INVALID_PARAMS

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 500,
                    "result": {
                        "outcome": {"outcome": "selected", "optionId": "allow-once"},
                        "_meta": {"authorization": "Bearer strip-perm", "source": "dialog"},
                    },
                }
            )

            post_perm_frames = [ws.receive_json() for _ in range(5)]
            assert [
                f["params"]["update"]["sessionUpdate"] for f in post_perm_frames[:4]
            ] == ["tool_call_update", "tool_call_update", "usage_update", "agent_message_chunk"]
            assert post_perm_frames[4] == {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {"stopReason": "end_turn"},
            }

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": sid,
                        "prompt": [{"type": "text", "text": "Follow-up question"}],
                    },
                }
            )
            followup_chunk = ws.receive_json()
            followup_done = ws.receive_json()
            assert (
                followup_chunk["params"]["update"]["content"]["text"]
                == "Echo: Follow-up question"
            )
            assert followup_done == {
                "jsonrpc": "2.0",
                "id": 4,
                "result": {"stopReason": "end_turn"},
            }

        stderr_logs = capsys.readouterr().err
        assert f"<base64 {len(large_png)} chars>" in stderr_logs
        assert "strip-top-secret" not in stderr_logs
        assert "Bearer strip-perm" not in stderr_logs

        captured = _read_captured_jsonl(capture_path)
        prompt_sent = next(m for m in captured if m.get("id") == 3)["params"]
        assert prompt_sent["_meta"] == {"uiTurn": 1}
        assert prompt_sent["prompt"][0]["_meta"] == {"tag": "u1"}
        assert prompt_sent["prompt"][0]["annotations"]["_meta"] == {"hint": "focus"}
        assert "_meta" not in prompt_sent["prompt"][1]["resource"]

        perm_sent = next(m for m in captured if m.get("id") == 500 and "result" in m)
        assert perm_sent["result"]["_meta"] == {"source": "dialog"}
    finally:
        reset_logging()


def test_prompt_session_existence_capability_and_concurrency_gates(tmp_path: Path) -> None:
    """Enforces ``-32002`` inactive session, ``-32602`` caps, and ``-32600`` concurrency."""
    capture_path = tmp_path / "gates_capture.jsonl"
    script = _write_prompt_agent_script(
        tmp_path / "gates_agent.py",
        image_cap=False,
        audio_cap=False,
        embedded_cap=False,
        capture_path=capture_path,
    )
    cwd = str(tmp_path.resolve())

    with (
        _make_prompt_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(_init_msg())
        ws.receive_json()

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess_unknown",
                    "prompt": [{"type": "text", "text": "Hello"}],
                },
            }
        )
        missing_err = ws.receive_json()
        assert missing_err["error"]["code"] == ACP_RESOURCE_NOT_FOUND
        assert missing_err["error"]["data"] == {"sessionId": "sess_unknown"}

        ws.send_json(
            {"jsonrpc": "2.0", "id": 3, "method": "session/new", "params": {"cwd": cwd}}
        )
        sid = ws.receive_json()["result"]["sessionId"]

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "no id"}]},
            }
        )
        assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_REQUEST

        for idx, bad_prompt in enumerate(
            (
                [],
                [{"type": "image", "mimeType": "image/png", "data": "QUJD"}],
                [{"type": "audio", "mimeType": "audio/wav", "data": "QUJD"}],
                [
                    {
                        "type": "resource",
                        "resource": {"uri": "file:///a.py", "text": "x = 1"},
                    }
                ],
            ),
            start=10,
        ):
            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": idx,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": bad_prompt},
                }
            )
            assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [{"type": "text", "text": "abort-on-cancel"}],
                },
            }
        )
        first_chunk = ws.receive_json()
        assert first_chunk["params"]["update"]["sessionUpdate"] == "agent_message_chunk"

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "overlap"}]},
            }
        )
        overlap_err = ws.receive_json()
        assert overlap_err["id"] == 21
        assert overlap_err["error"]["code"] == JSONRPC_INVALID_REQUEST

        ws.send_json(
            {"jsonrpc": "2.0", "method": "$/cancel_request", "params": {"requestId": 20}}
        )
        normalized_cancel = ws.receive_json()
        assert normalized_cancel == {
            "jsonrpc": "2.0",
            "id": 20,
            "result": {"stopReason": "cancelled"},
        }

        ws.send_json(
            {"jsonrpc": "2.0", "id": 22, "method": "session/close", "params": {"sessionId": sid}}
        )
        assert ws.receive_json()["id"] == 22

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 23,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [{"type": "text", "text": "after close"}],
                },
            }
        )
        closed_err = ws.receive_json()
        assert closed_err["error"]["code"] == ACP_RESOURCE_NOT_FOUND
        assert closed_err["error"]["data"] == {"sessionId": sid}


def test_session_cancel_flow_and_notification_id_rejection(tmp_path: Path) -> None:
    """``session/cancel`` + ``cancelled`` outcome finishes with ``stopReason='cancelled'``."""
    script = _write_prompt_agent_script(tmp_path / "cancel_agent.py")
    cwd = str(tmp_path.resolve())

    with (
        _make_prompt_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(_init_msg())
        ws.receive_json()

        ws.send_json(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": cwd}}
        )
        sid = ws.receive_json()["result"]["sessionId"]

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "session/cancel",
                "params": {"sessionId": sid},
            }
        )
        bad_cancel = ws.receive_json()
        assert bad_cancel["id"] == 99
        assert bad_cancel["error"]["code"] == JSONRPC_INVALID_REQUEST

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [{"type": "text", "text": "inspect code"}],
                },
            }
        )
        frames = [ws.receive_json() for _ in range(5)]
        assert frames[-1]["id"] == 500

        ws.send_json(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}
        )
        ws.send_json(
            {"jsonrpc": "2.0", "id": 500, "result": {"outcome": {"outcome": "cancelled"}}}
        )

        failed_tool = ws.receive_json()
        prompt_done = ws.receive_json()
        assert failed_tool["params"]["update"]["status"] == "failed"
        assert prompt_done == {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"stopReason": "cancelled"},
        }


def test_session_update_filtering_when_client_capabilities_omitted(tmp_path: Path) -> None:
    """Suppresses unadvertised ``session/update`` variants and clamped ``fs/*``/``terminal/*``."""
    capture_path = tmp_path / "minimal_caps_capture.jsonl"
    script = _write_prompt_agent_script(tmp_path / "minimal_agent.py", capture_path=capture_path)
    cwd = str(tmp_path.resolve())

    with (
        _make_prompt_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(_init_msg({}))
        ws.receive_json()

        ws.send_json(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": cwd}}
        )
        sid = ws.receive_json()["result"]["sessionId"]

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [{"type": "text", "text": "emit-all-variants"}],
                },
            }
        )
        received_kinds, tool_contents = _collect_turn_updates(ws, prompt_req_id=3)

        for gated in GATED_UPDATE_KINDS:
            assert gated not in received_kinds
        assert "available_commands_update" in received_kinds
        assert [c["type"] for c in tool_contents] == ["content"]

    captured = _read_captured_jsonl(capture_path)
    blocked_errors = [m for m in captured if m.get("id") in (701, 702)]
    assert len(blocked_errors) == 2
    assert all(m["error"]["code"] == JSONRPC_METHOD_NOT_FOUND for m in blocked_errors)


def test_subagent_child_session_updates_cancel_patch_and_restricted_methods(
    tmp_path: Path,
) -> None:
    """Allows all advertised updates and enforces subagent ``cancel`` & restricted methods."""
    capture_path = tmp_path / "subagent_capture.jsonl"
    script = _write_prompt_agent_script(tmp_path / "subagent_agent.py", capture_path=capture_path)
    cwd = str(tmp_path.resolve())
    full_client_caps = {
        "subagents": {},
        "plan": {},
        "session": {"notices": {}, "compaction": {}},
    }

    with (
        _make_prompt_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(_init_msg(full_client_caps))
        ws.receive_json()

        ws.send_json(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": cwd}}
        )
        sid = ws.receive_json()["result"]["sessionId"]

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [{"type": "text", "text": "emit-all-variants"}],
                },
            }
        )
        full_kinds, full_tool_contents = _collect_turn_updates(ws, prompt_req_id=3)

        for expected in GATED_UPDATE_KINDS:
            assert expected in full_kinds
        assert [c["type"] for c in full_tool_contents] == ["session", "content"]

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": "sess_child_1", "_meta": {"cookie": "strip"}},
            }
        )

        restricted_calls = (
            (
                "session/prompt",
                {
                    "sessionId": "sess_child_1",
                    "prompt": [{"type": "text", "text": "illegal"}],
                },
            ),
            ("session/load", {"sessionId": "sess_child_1", "cwd": cwd}),
            ("session/resume", {"sessionId": "sess_child_1", "cwd": cwd}),
            ("session/close", {"sessionId": "sess_child_1"}),
            ("session/delete", {"sessionId": "sess_child_1"}),
            ("session/set_mode", {"sessionId": "sess_child_1", "modeId": "code"}),
        )
        for idx, (restricted_method, restricted_params) in enumerate(restricted_calls, start=30):
            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": idx,
                    "method": restricted_method,
                    "params": restricted_params,
                }
            )
            assert ws.receive_json()["error"]["code"] == JSONRPC_INVALID_PARAMS

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 40,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [{"type": "text", "text": "patch-child-nocancel"}],
                },
            }
        )
        patch_notif = ws.receive_json()
        assert patch_notif["params"]["update"]["sessionUpdate"] == "subagent_update"
        assert ws.receive_json()["id"] == 40

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": "sess_child_1"},
            }
        )
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "ping"}]},
            }
        )
        ws.receive_json()
        assert ws.receive_json()["id"] == 41

    captured = _read_captured_jsonl(capture_path)
    child_cancels = [
        m
        for m in captured
        if m.get("method") == "session/cancel"
        and (m.get("params") or {}).get("sessionId") == "sess_child_1"
    ]
    assert len(child_cancels) == 1
    assert "_meta" not in child_cancels[0]["params"]


def test_session_close_flushes_cancelled_permission_and_turn_to_agent(tmp_path: Path) -> None:
    """Closing a session mid-permission auto-replies ``cancelled`` and sends ``session/cancel``."""
    capture_path = tmp_path / "teardown_capture.jsonl"
    script = _write_prompt_agent_script(tmp_path / "teardown_agent.py", capture_path=capture_path)
    cwd = str(tmp_path.resolve())

    with (
        _make_prompt_client(tmp_path, script) as client,
        client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws,
    ):
        ws.send_json(_init_msg())
        ws.receive_json()

        ws.send_json(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": cwd}}
        )
        sid = ws.receive_json()["result"]["sessionId"]

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": sid,
                    "prompt": [{"type": "text", "text": "hold-permission"}],
                },
            }
        )
        perm_frame = ws.receive_json()
        assert perm_frame["id"] == 900
        assert perm_frame["method"] == "session/request_permission"

        ws.send_json(
            {"jsonrpc": "2.0", "id": 4, "method": "session/close", "params": {"sessionId": sid}}
        )
        first_reply = ws.receive_json()
        second_reply = ws.receive_json()
        by_id = {first_reply["id"]: first_reply, second_reply["id"]: second_reply}
        assert by_id[3] == {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "cancelled"}}
        assert by_id[4] == {"jsonrpc": "2.0", "id": 4, "result": {}}

    captured = _read_captured_jsonl(capture_path)
    auto_perm = next(m for m in captured if m.get("id") == 900 and "result" in m)
    assert auto_perm["result"] == {"outcome": {"outcome": "cancelled"}}
    assert any(
        m.get("method") == "session/cancel" and m.get("params") == {"sessionId": sid}
        for m in captured
    )
