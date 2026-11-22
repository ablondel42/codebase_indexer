"""Tests for SQLite chat timeline persistence, silent hydration, and REST pagination."""

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from api.models.config import ApiServerConfig
from api.services.server import ApiServerRunner
from core.models.chat import (
    ChatItemRole,
    ChatPageQuery,
    ChatPlanStep,
    ChatTimelineItemUpsert,
)
from core.models.config import AppConfig
from storage.services.chat import SqlChatStore

VALID_WS_HEADERS: dict[str, str] = {
    "host": "127.0.0.1:8000",
    "origin": "http://localhost:5173",
}


def _write_chat_agent_script(script_path: Path, *, capture_path: Path) -> Path:
    """Write a deterministic ACP agent supporting silent load hydration and prompt turns."""
    script_body = textwrap.dedent(
        f"""
        import json
        import sys
        from pathlib import Path

        CAPTURE = {str(capture_path)!r}
        counter = 0

        def send(payload):
            sys.stdout.write(json.dumps(payload) + "\\n")
            sys.stdout.flush()

        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            msg = json.loads(line)
            with Path(CAPTURE).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(msg) + "\\n")

            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params") or {{}}

            if method == "initialize":
                send({{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{
                        "protocolVersion": 1,
                        "agentCapabilities": {{
                            "loadSession": True,
                            "sessionCapabilities": {{
                                "list": {{}},
                                "load": {{}},
                                "resume": {{}},
                                "close": {{}},
                                "delete": {{}},
                            }},
                        }},
                        "agentInfo": {{"name": "Chat Test Agent", "version": "1.0.0"}},
                        "authMethods": [],
                    }},
                }})
            elif method == "session/load":
                sid = params["sessionId"]
                replay = [
                    ("user_message_chunk", "u1", "First user question"),
                    ("agent_thought_chunk", "t1", "Thinking step 1."),
                    ("agent_thought_chunk", "t1", " Step 2."),
                    ("agent_message_chunk", "a1", "First "),
                    ("agent_message_chunk", "a1", "answer."),
                    ("user_message_chunk", "u2", "Second question"),
                    ("agent_message_chunk", "a2", "Second answer."),
                ]
                for kind, msg_id, txt in replay:
                    send({{
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {{
                            "sessionId": sid,
                            "update": {{
                                "sessionUpdate": kind,
                                "messageId": msg_id,
                                "content": {{"type": "text", "text": txt}},
                            }},
                        }},
                    }})
                send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{
                        "sessionId": sid,
                        "update": {{
                            "sessionUpdate": "tool_call",
                            "toolCallId": "tc_hist",
                            "kind": "read",
                            "title": "Read file",
                            "status": "completed",
                            "locations": [{{"path": "src/main.py"}}],
                            "rawOutput": "ok",
                        }},
                    }},
                }})
                send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{
                        "sessionId": sid,
                        "update": {{
                            "sessionUpdate": "available_commands_update",
                            "availableCommands": [{{"name": "plan", "description": "Plan work"}}],
                        }},
                    }},
                }})
                send({{"jsonrpc": "2.0", "id": req_id, "result": {{}}}})
            elif method == "session/resume":
                send({{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{"modes": {{"currentModeId": "default", "availableModes": []}}}},
                }})
            elif method == "session/new":
                counter += 1
                send({{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{"sessionId": f"s_live_{{counter}}"}},
                }})
            elif method == "session/prompt":
                sid = params["sessionId"]
                for kind, msg_id, txt in (
                    ("agent_thought_chunk", None, "Checking "),
                    ("agent_thought_chunk", None, "codebase..."),
                    ("agent_message_chunk", "live_a1", "Hello "),
                    ("agent_message_chunk", "live_a1", "from ACP."),
                ):
                    upd = {{
                        "sessionUpdate": kind,
                        "content": {{"type": "text", "text": txt}},
                    }}
                    if msg_id is not None:
                        upd["messageId"] = msg_id
                    send({{
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {{"sessionId": sid, "update": upd}},
                    }})
                send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{
                        "sessionId": sid,
                        "update": {{
                            "sessionUpdate": "tool_call",
                            "toolCallId": "tc_live",
                            "kind": "search",
                            "title": "Search symbols",
                            "status": "in_progress",
                            "locations": ["src/api/services/chat.py"],
                        }},
                    }},
                }})
                send({{
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {{
                        "sessionId": sid,
                        "update": {{
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "tc_live",
                            "status": "completed",
                            "rawOutput": {{"stdout": "3 matches", "exitCode": 0}},
                        }},
                    }},
                }})
                send({{"jsonrpc": "2.0", "id": req_id, "result": {{"stopReason": "end_turn"}}}})
            elif method in ("session/close", "session/delete"):
                send({{"jsonrpc": "2.0", "id": req_id, "result": {{}}}})
            else:
                send({{"jsonrpc": "2.0", "id": req_id, "result": {{}}}})
        """
    ).lstrip()
    script_path.write_text(script_body, encoding="utf-8")
    return script_path


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


def test_sql_chat_store_turn_grouping_coalescing_and_cursor_pagination(tmp_path: Path) -> None:
    """Stores 25 turns, coalesces chunks/tools in place, and paginates without torn turns."""
    store = SqlChatStore(tmp_path / "cbi_chat.db")
    try:
        deltas: list[ChatTimelineItemUpsert] = []
        for idx in range(1, 13):
            deltas.append(
                ChatTimelineItemUpsert(
                    item_id=f"u_{idx}",
                    role=ChatItemRole.USER,
                    text=f"Question {idx}",
                    merge_mode="replace",
                )
            )
            deltas.extend(
                [
                    ChatTimelineItemUpsert(
                        item_id=f"th_{idx}",
                        role=ChatItemRole.THOUGHT,
                        text="Part A ",
                        merge_mode="append_text",
                    ),
                    ChatTimelineItemUpsert(
                        item_id=f"th_{idx}",
                        role=ChatItemRole.THOUGHT,
                        text="Part B",
                        merge_mode="append_text",
                    ),
                    ChatTimelineItemUpsert(
                        item_id=f"tool_{idx}",
                        role=ChatItemRole.TOOL,
                        tool_kind="read",
                        tool_title="Read module",
                        tool_status="running",
                        locations=("src/core/models/chat.py",),
                    ),
                    ChatTimelineItemUpsert(
                        item_id=f"tool_{idx}",
                        role=ChatItemRole.TOOL,
                        tool_status="done",
                        text="Loaded 100 lines",
                        exit_code=0,
                    ),
                    ChatTimelineItemUpsert(
                        item_id=f"plan_{idx}",
                        role=ChatItemRole.PLAN,
                        plan_title="Execution plan",
                        plan_steps=(ChatPlanStep(title="Inspect", status="done"),),
                    ),
                    ChatTimelineItemUpsert(
                        item_id=f"a_{idx}",
                        role=ChatItemRole.AGENT,
                        text=f"Answer {idx}",
                        merge_mode="append_text",
                    ),
                ]
            )
        deltas.append(
            ChatTimelineItemUpsert(
                item_id="u_13",
                role=ChatItemRole.USER,
                text="Final follow-up",
                merge_mode="replace",
            )
        )

        store.upsert_items("sess_25", deltas, mark_hydrated=True)
        meta = store.get_session("sess_25")
        assert meta is not None
        assert meta.hydrated is True
        assert meta.last_turn_index == 25

        tail = store.get_history_page(ChatPageQuery(session_id="sess_25", limit_turns=8))
        assert tail.has_more is True
        assert tail.next_before_turn == 18
        assert sorted({item.turn_index for item in tail.items}) == list(range(18, 26))

        mid = store.get_history_page(
            ChatPageQuery(session_id="sess_25", limit_turns=10, before_turn=tail.next_before_turn)
        )
        assert mid.has_more is True
        assert mid.next_before_turn == 8
        assert sorted({item.turn_index for item in mid.items}) == list(range(8, 18))

        head = store.get_history_page(
            ChatPageQuery(session_id="sess_25", limit_turns=10, before_turn=mid.next_before_turn)
        )
        assert head.has_more is False
        assert head.next_before_turn is None
        assert sorted({item.turn_index for item in head.items}) == list(range(1, 8))

        combined = [*head.items, *mid.items, *tail.items]
        assert [item.seq for item in combined] == list(range(1, len(combined) + 1))
        first_thought = next(item for item in combined if item.item_id == "th_1")
        assert first_thought.text == "Part A Part B"
        first_tool = next(item for item in combined if item.item_id == "tool_1")
        assert first_tool.tool_status == "done"
        assert first_tool.locations == ("src/core/models/chat.py",)
        assert first_tool.text == "Loaded 100 lines"

        store.upsert_items(
            "sess_25",
            [ChatTimelineItemUpsert(item_id="fresh", role=ChatItemRole.USER, text="Reset")],
            reset_existing=True,
            mark_hydrated=True,
        )
        reset_page = store.get_history_page(ChatPageQuery(session_id="sess_25"))
        assert reset_page.total_turns == 1
        assert [item.item_id for item in reset_page.items] == ["fresh"]

        store.delete_session("sess_25")
        assert store.get_session("sess_25") is None
        assert store.get_history_page(ChatPageQuery(session_id="sess_25")).items == ()
    finally:
        store.close()


def test_silent_hydration_then_fast_resume_avoids_ws_replay(tmp_path: Path) -> None:
    """First ``hydrateOnly`` resume ingests via ``session/load`` silently; second uses resume."""
    capture_path = tmp_path / "hydration_capture.jsonl"
    script = _write_chat_agent_script(tmp_path / "chat_agent.py", capture_path=capture_path)
    api_cfg = ApiServerConfig(
        agy_command=(sys.executable, "-u", str(script)),
        workspace_root=tmp_path,
        chat_db_path=tmp_path / "cbi_chat.db",
    )
    app_cfg = AppConfig(db_path=tmp_path / "idx.db")
    cwd = str(tmp_path.resolve())

    with TestClient(
        ApiServerRunner(api_cfg, app_cfg).create_app(),
        base_url="http://127.0.0.1:8000",
    ) as client:
        with client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws:
            ws.send_json(_init_msg())
            ws.receive_json()

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/resume",
                    "params": {
                        "sessionId": "sess_legacy",
                        "cwd": cwd,
                        "mcpServers": [],
                        "_meta": {"hydrateOnly": True},
                    },
                }
            )
            first_frame = ws.receive_json()
            assert first_frame["method"] == "session/update"
            assert first_frame["params"]["update"]["sessionUpdate"] == "available_commands_update"

            setup_resp = ws.receive_json()
            assert setup_resp["id"] == 2
            assert setup_resp["result"]["_meta"]["cbiChat"] == {
                "hydrated": True,
                "totalTurns": 4,
                "totalItems": 6,
            }

        tail_resp = client.get(
            "/api/sessions/sess_legacy/messages?limit_turns=2",
            headers={"host": "127.0.0.1:8000"},
        )
        assert tail_resp.status_code == 200
        tail_body = tail_resp.json()
        assert tail_body["hydrated"] is True
        assert tail_body["hasMore"] is True
        assert tail_body["nextBeforeTurn"] == 3
        assert [item["turnIndex"] for item in tail_body["items"]] == [3, 4, 4]
        assert tail_body["items"][2]["toolStatus"] == "done"

        head_resp = client.get(
            f"/api/sessions/sess_legacy/messages?limit_turns=2&before_turn={tail_body['nextBeforeTurn']}",
            headers={"host": "127.0.0.1:8000"},
        )
        assert head_resp.status_code == 200
        head_body = head_resp.json()
        assert head_body["hasMore"] is False
        assert head_body["nextBeforeTurn"] is None
        assert [item["text"] for item in head_body["items"]] == [
            "First user question",
            "Thinking step 1. Step 2.",
            "First answer.",
        ]

        with client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws2:
            ws2.send_json(_init_msg())
            ws2.receive_json()

            ws2.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "session/resume",
                    "params": {
                        "sessionId": "sess_legacy",
                        "cwd": cwd,
                        "mcpServers": [],
                        "_meta": {"hydrateOnly": True},
                    },
                }
            )
            fast_resp = ws2.receive_json()
            assert fast_resp["id"] == 10
            assert fast_resp["result"]["_meta"]["cbiChat"]["hydrated"] is True

    forwarded_methods = [
        json.loads(line)["method"]
        for line in capture_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert forwarded_methods == ["initialize", "session/load", "initialize", "session/resume"]


def test_realtime_prompt_flush_security_and_session_delete(tmp_path: Path) -> None:
    """Persists prompts and live stream updates immediately and enforces HTTP security gates."""
    capture_path = tmp_path / "live_capture.jsonl"
    script = _write_chat_agent_script(tmp_path / "live_agent.py", capture_path=capture_path)
    api_cfg = ApiServerConfig(
        agy_command=(sys.executable, "-u", str(script)),
        workspace_root=tmp_path,
        chat_db_path=tmp_path / "cbi_chat.db",
    )
    app_cfg = AppConfig(db_path=tmp_path / "idx.db")
    cwd = str(tmp_path.resolve())

    with TestClient(
        ApiServerRunner(api_cfg, app_cfg).create_app(),
        base_url="http://127.0.0.1:8000",
    ) as client:
        assert (
            client.get(
                "/api/sessions/s1/messages",
                headers={"host": "attacker.example:8000"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/sessions/s1/messages",
                headers={"host": "127.0.0.1:8000", "origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/sessions/..%2Fescape/messages",
                headers={"host": "127.0.0.1:8000"},
            ).status_code
            in (400, 404)
        )

        with client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws:
            ws.send_json(_init_msg())
            ws.receive_json()

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": cwd, "mcpServers": []},
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
                        "prompt": [{"type": "text", "text": "Analyze chat persistence"}],
                    },
                }
            )
            received = [ws.receive_json() for _ in range(7)]
            assert received[-1] == {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}}

            page = client.get(
                f"/api/sessions/{sid}/messages?limit_turns=8",
                headers={"host": "127.0.0.1:8000"},
            ).json()
            assert page["hydrated"] is True
            assert page["totalTurns"] == 2
            roles = [item["role"] for item in page["items"]]
            assert roles == ["user", "thought", "agent", "tool"]
            assert page["items"][0]["text"] == "Analyze chat persistence"
            assert page["items"][1]["text"] == "Checking codebase..."
            assert page["items"][2]["text"] == "Hello from ACP."
            assert page["items"][3]["toolStatus"] == "done"
            assert page["items"][3]["exitCode"] == 0
            assert page["items"][3]["text"] == "3 matches"

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/delete",
                    "params": {"sessionId": sid},
                }
            )
            assert ws.receive_json() == {"jsonrpc": "2.0", "id": 4, "result": {}}

        deleted_page = client.get(
            f"/api/sessions/{sid}/messages",
            headers={"host": "127.0.0.1:8000"},
        ).json()
        assert deleted_page["hydrated"] is False
        assert deleted_page["items"] == []
