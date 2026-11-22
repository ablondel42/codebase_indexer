"""Tests for SQLite chat timeline persistence, silent hydration, and REST pagination."""

import json
import sqlite3
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from api.models.config import ApiServerConfig
from api.services.server import ApiServerRunner
from api.utils.transcript import classify_profile_watch_path
from core.models.chat import (
    ChatItemRole,
    ChatPageQuery,
    ChatPlanStep,
    ChatTimelineItemUpsert,
    ChatTranscriptCursor,
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


def _recv_sync_until(ws: Any, predicate: Any, *, max_frames: int = 8) -> dict[str, Any]:
    """Receive JSON frames from ``ws`` with bounded timeout until ``predicate(params)`` matches."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        for _ in range(max_frames):
            frame = pool.submit(ws.receive_json).result(timeout=3.0)
            if frame.get("method") != "cbi/chat_sync":
                continue
            params = frame.get("params") or {}
            if predicate(params):
                return params
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    raise AssertionError("Timed out waiting for matching cbi/chat_sync frame")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one compact NDJSON line and flush immediately to disk."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        handle.flush()


def test_sql_chat_store_migrates_legacy_schema_columns(tmp_path: Path) -> None:
    """Upgrades pre-existing ``cbi_chat.db`` tables cleanly without losing stored rows."""
    db_path = tmp_path / "legacy_chat.db"
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.executescript(
        """
        CREATE TABLE chat_sessions (
            session_id TEXT PRIMARY KEY,
            hydrated INTEGER NOT NULL DEFAULT 0,
            last_seq INTEGER NOT NULL DEFAULT 0,
            last_turn_index INTEGER NOT NULL DEFAULT 0,
            last_turn_kind TEXT,
            total_items INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE chat_items (
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            turn_index INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            tool_kind TEXT,
            tool_title TEXT,
            tool_status TEXT,
            exit_code INTEGER,
            locations_json TEXT,
            plan_title TEXT,
            plan_steps_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (session_id, seq)
        );
        INSERT INTO chat_sessions VALUES ('s_old', 1, 1, 1, 'user', 1, '2026-11-22T00:00:00Z');
        INSERT INTO chat_items VALUES (
            's_old', 1, 1, 'auto_1', 'user', 'Hello',
            NULL, NULL, NULL, NULL, NULL, NULL, NULL,
            '2026-11-22T00:00:00Z', '2026-11-22T00:00:00Z'
        );
        """
    )
    raw_conn.commit()
    raw_conn.close()

    store = SqlChatStore(db_path)
    try:
        meta = store.get_session("s_old")
        assert meta is not None
        assert meta.hydrated is True
        assert meta.transcript_step_index is None
        assert meta.transcript_offset == 0

        stored = store.upsert_items(
            "s_old",
            [
                ChatTimelineItemUpsert(
                    item_id="a_new",
                    role=ChatItemRole.AGENT,
                    text="Reply",
                    duration_sec=3.4,
                )
            ],
            transcript_cursor=ChatTranscriptCursor(step_index=2, byte_offset=128),
        )
        assert len(stored) == 1
        assert stored[0].duration_sec == 3.4
        updated = store.get_session("s_old")
        assert updated is not None
        assert updated.transcript_step_index == 2
        assert updated.transcript_offset == 128
    finally:
        store.close()


def test_external_profile_watcher_streams_cbi_chat_sync_and_durations(tmp_path: Path) -> None:
    """Watches external transcript, artifact, and session writes and pushes ``cbi/chat_sync``."""
    gemini_root = (tmp_path / "gemini").resolve()
    profile_root = gemini_root / "antigravity"
    sid = "sess_ext_watch"
    logs_dir = profile_root / "brain" / sid / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    (profile_root / "conversations").mkdir(parents=True)
    transcript = logs_dir / "transcript_full.jsonl"
    transcript.touch()

    ignored_paths = (
        profile_root / "cbi_chat.db-wal",
        profile_root / "conversation_summaries.db",
        profile_root / "conversation_summaries.db-wal",
        profile_root / "conversation_summaries.db-shm",
        profile_root / "conversations" / "s1.db-wal",
        profile_root / "conversations" / "s1.db-shm",
    )
    for ignored_path in ignored_paths:
        assert classify_profile_watch_path(profile_root, ignored_path, is_directory=False) == (
            "ignore",
            None,
        )

    script = _write_chat_agent_script(
        tmp_path / "ext_agent.py",
        capture_path=tmp_path / "cap.jsonl",
    )
    api_cfg = ApiServerConfig(
        agy_command=(sys.executable, "-u", str(script)),
        agy_gemini_dir=str(gemini_root),
        workspace_root=tmp_path,
        chat_db_path=profile_root / "cbi_chat.db",
        watch_agy_profile=True,
    )
    runner = ApiServerRunner(api_cfg, AppConfig(db_path=tmp_path / "idx.db"))
    runner.profile_watcher.set_debounce_windows(transcript_seconds=0.02, sessions_seconds=0.04)

    with TestClient(runner.create_app(), base_url="http://127.0.0.1:8000") as client:
        time.sleep(0.25)
        with client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws:
            ws.send_json(_init_msg())
            assert ws.receive_json()["id"] == 1

            _append_jsonl(
                transcript,
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-22T15:00:00Z",
                    "content": (
                        "<USER_REQUEST>\nAudit module\n</USER_REQUEST>\n"
                        "<ADDITIONAL_METADATA>\nx\n</ADDITIONAL_METADATA>"
                    ),
                },
            )
            p0 = _recv_sync_until(
                ws,
                lambda p: p.get("event") == "items_upserted"
                and any(i["id"] == "tf:0:user" for i in p.get("items", [])),
            )
            assert p0["sessionId"] == sid
            assert p0["turnActive"] is True
            assert p0["turnStartedAt"] == "2026-11-22T15:00:00Z"
            assert p0["items"][0]["text"] == "Audit module"

            _append_jsonl(
                transcript,
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-22T15:00:01Z",
                    "thinking": "Checking layout.",
                    "tool_calls": [{"name": "read_file", "args": {"Path": "/repo/app.py"}}],
                },
            )
            p1 = _recv_sync_until(
                ws,
                lambda p: p.get("event") == "items_upserted"
                and any(i["id"] == "tf:1:tool:0" for i in p.get("items", [])),
            )
            tool_item = next(i for i in p1["items"] if i["id"] == "tf:1:tool:0")
            assert tool_item["toolStatus"] == "running"
            assert tool_item["toolKind"] == "read"
            assert tool_item["locations"] == ["/repo/app.py"]

            _append_jsonl(
                transcript,
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-22T15:00:04Z",
                    "content": (
                        "Created At: 2026-11-22T15:00:04+00:00\n"
                        "Completed At: 2026-11-22T15:00:06+00:00\nprint('ok')"
                    ),
                },
            )
            p2 = _recv_sync_until(
                ws,
                lambda p: p.get("event") == "items_upserted"
                and any(
                    i["id"] == "tf:1:tool:0" and i.get("toolStatus") == "done"
                    for i in p.get("items", [])
                ),
            )
            thought_upd = next(i for i in p2["items"] if i["id"] == "tf:1:thought")
            tool_done = next(i for i in p2["items"] if i["id"] == "tf:1:tool:0")
            assert thought_upd["durationSec"] == 3.0
            assert tool_done["durationSec"] == 2.0
            assert tool_done["text"] == "print('ok')"

            _append_jsonl(
                transcript,
                {
                    "step_index": 3,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-22T15:00:07Z",
                    "content": "Done auditing.",
                    "tool_calls": [],
                },
            )
            p3 = _recv_sync_until(
                ws,
                lambda p: p.get("event") == "items_upserted"
                and any(i["id"] == "tf:3:agent" for i in p.get("items", [])),
            )
            assert p3["turnActive"] is False

            new_db = profile_root / "conversations" / "sess_new_ext.db"
            new_db.write_bytes(b"SQLite format 3\x00")
            _recv_sync_until(ws, lambda p: p.get("event") == "sessions_changed")

            # Modifying an already-known .db must NOT re-emit sessions_changed (no infinite loop).
            new_db.write_bytes(b"SQLite format 3\x00updated")
            (profile_root / "brain" / sid / "plan.md").write_text("# Plan\n", encoding="utf-8")
            next_evt = _recv_sync_until(
                ws,
                lambda p: p.get("event") in ("artifacts_changed", "sessions_changed"),
            )
            assert next_evt["event"] == "artifacts_changed"
            assert next_evt["sessionId"] == sid

        page = client.get(
            f"/api/sessions/{sid}/messages?limit_turns=8",
            headers={"host": "127.0.0.1:8000"},
        ).json()
        assert [i["id"] for i in page["items"]] == [
            "tf:0:user",
            "tf:1:thought",
            "tf:1:tool:0",
            "tf:3:agent",
        ]
        assert page["items"][1]["durationSec"] == 3.0
        assert page["items"][2]["durationSec"] == 2.0


def test_legacy_hydrated_session_auto_migrates_and_handles_rewind(tmp_path: Path) -> None:
    """Auto-migrates legacy ``auto_*`` sessions to ``tf:*`` and emits reset on rewind."""
    gemini_root = (tmp_path / "gemini_mig").resolve()
    profile_root = gemini_root / "antigravity"
    sid = "sess_migrated"
    logs_dir = profile_root / "brain" / sid / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    transcript = logs_dir / "transcript_full.jsonl"

    capture_path = tmp_path / "mig_capture.jsonl"
    script = _write_chat_agent_script(tmp_path / "mig_agent.py", capture_path=capture_path)
    api_cfg = ApiServerConfig(
        agy_command=(sys.executable, "-u", str(script)),
        agy_gemini_dir=str(gemini_root),
        workspace_root=tmp_path,
        chat_db_path=profile_root / "cbi_chat.db",
        watch_agy_profile=True,
    )
    runner = ApiServerRunner(api_cfg, AppConfig(db_path=tmp_path / "idx.db"))
    runner.profile_watcher.set_debounce_windows(transcript_seconds=0.02, sessions_seconds=0.04)

    runner.chat_store.upsert_items(
        sid,
        [
            ChatTimelineItemUpsert(
                item_id="auto_old_u",
                role=ChatItemRole.USER,
                text="Old question",
            ),
            ChatTimelineItemUpsert(
                item_id="auto_old_a",
                role=ChatItemRole.AGENT,
                text="Old reply",
            ),
        ],
        mark_hydrated=True,
    )

    _append_jsonl(
        transcript,
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-22T15:10:00Z",
            "content": "<USER_REQUEST>\nOld question\n</USER_REQUEST>",
        },
    )
    _append_jsonl(
        transcript,
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-22T15:10:02Z",
            "content": "Old reply",
            "tool_calls": [],
        },
    )
    _append_jsonl(
        transcript,
        {
            "step_index": 2,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-22T15:11:00Z",
            "content": "<USER_REQUEST>\nExternal follow-up\n</USER_REQUEST>",
        },
    )
    _append_jsonl(
        transcript,
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-22T15:11:05Z",
            "thinking": "Analyzing external follow-up.",
            "content": "External answer",
            "tool_calls": [],
        },
    )

    with TestClient(runner.create_app(), base_url="http://127.0.0.1:8000") as client:
        time.sleep(0.25)
        migrated = client.get(
            f"/api/sessions/{sid}/messages?limit_turns=8",
            headers={"host": "127.0.0.1:8000"},
        ).json()
        assert [i["id"] for i in migrated["items"]] == [
            "tf:0:user",
            "tf:1:agent",
            "tf:2:user",
            "tf:3:thought",
            "tf:3:agent",
        ]
        meta = runner.chat_store.get_session(sid)
        assert meta is not None
        assert meta.transcript_step_index == 3

        with client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws:
            ws.send_json(_init_msg())
            assert ws.receive_json()["id"] == 1

            ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "session/resume",
                    "params": {
                        "sessionId": sid,
                        "cwd": str(tmp_path.resolve()),
                        "mcpServers": [],
                        "_meta": {"hydrateOnly": True},
                    },
                }
            )
            resume_resp = ws.receive_json()
            assert resume_resp["id"] == 9

            transcript.write_text(
                json.dumps(
                    {
                        "step_index": 0,
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "created_at": "2026-11-22T15:10:00Z",
                        "content": "<USER_REQUEST>\nRewound question\n</USER_REQUEST>",
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            reset_evt = _recv_sync_until(ws, lambda p: p.get("event") == "timeline_reset")
            assert reset_evt["sessionId"] == sid
            assert reset_evt["totalItems"] == 1

        after_rewind = client.get(
            f"/api/sessions/{sid}/messages?limit_turns=8",
            headers={"host": "127.0.0.1:8000"},
        ).json()
        assert [i["text"] for i in after_rewind["items"]] == ["Rewound question"]

    forwarded_methods = [
        json.loads(line)["method"]
        for line in capture_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert forwarded_methods == ["initialize", "session/resume"]


def _append_bg_launch(transcript: Path, sid: str) -> None:
    """Append a user prompt, backgrounded ``run_command`` tool call, and waiting reply."""
    _append_jsonl(
        transcript,
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-22T15:35:48Z",
            "content": "<USER_REQUEST>\nRun verification suite\n</USER_REQUEST>",
        },
    )
    _append_jsonl(
        transcript,
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-22T15:35:49Z",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest -q"}}],
        },
    )
    _append_jsonl(
        transcript,
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "RUNNING",
            "created_at": "2026-11-22T15:35:49Z",
            "content": (
                "Created At: 2026-11-22T15:35:49+00:00\n"
                f"Tool is running as a background task with task id: {sid}/task-982\n"
                "Task Description: pytest -q\n"
                f"Task logs are available at: file:///tmp/{sid}/task-982.log\n"
                "Log output:\nAll checks passed!\r\n\n"
                "YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS: A) wait or B) report.\n"
                " DO NOTHING ELSE."
            ),
        },
    )
    _append_jsonl(
        transcript,
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-22T15:35:50Z",
            "content": "Launched background verification.",
            "tool_calls": [],
        },
    )


def _assert_parser_version_self_heal(
    gemini_root: Path,
    profile_root: Path,
    tmp_path: Path,
    sid: str,
) -> None:
    """Downgrade ``parser_version`` + item status in SQLite and verify automatic repair."""
    db_path = profile_root / "cbi_chat.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE chat_sessions SET parser_version = 1, transcript_step_index = 4, "
        "transcript_offset = 999 WHERE session_id = ?",
        (sid,),
    )
    conn.execute(
        "UPDATE chat_items SET tool_status = 'running', exit_code = NULL, duration_sec = NULL, "
        "text = 'Tool is running as a background task' WHERE session_id = ? AND item_id = ?",
        (sid, "tf:1:tool:0"),
    )
    conn.commit()
    conn.close()

    script = _write_chat_agent_script(tmp_path / "heal_agent.py", capture_path=tmp_path / "h.jsonl")
    api_cfg = ApiServerConfig(
        agy_command=(sys.executable, "-u", str(script)),
        agy_gemini_dir=str(gemini_root),
        workspace_root=tmp_path,
        chat_db_path=db_path,
        watch_agy_profile=True,
    )
    restarted = ApiServerRunner(api_cfg, AppConfig(db_path=tmp_path / "idx_heal.db"))
    with TestClient(restarted.create_app(), base_url="http://127.0.0.1:8000") as client:
        repaired = client.get(
            f"/api/sessions/{sid}/messages?limit_turns=8",
            headers={"host": "127.0.0.1:8000"},
        ).json()
    restarted.engine.close()

    tool_map = {item["id"]: item for item in repaired["items"]}
    assert tool_map["tf:1:tool:0"]["toolStatus"] == "done"
    assert tool_map["tf:1:tool:0"]["exitCode"] == 0
    assert tool_map["tf:1:tool:0"]["durationSec"] == 15.0
    assert tool_map["tf:5:tool:0"]["toolStatus"] == "error"


def test_background_task_resolution_and_parser_version_self_heal(tmp_path: Path) -> None:
    """Resolves background tasks from ``SYSTEM_MESSAGE`` and repairs stale DB rows on boot."""
    gemini_root = (tmp_path / "gemini_bg").resolve()
    profile_root = gemini_root / "antigravity"
    sid = "sess_bg_task"
    logs_dir = profile_root / "brain" / sid / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    transcript = logs_dir / "transcript_full.jsonl"
    transcript.touch()

    script = _write_chat_agent_script(tmp_path / "bg_agent.py", capture_path=tmp_path / "bg.jsonl")
    api_cfg = ApiServerConfig(
        agy_command=(sys.executable, "-u", str(script)),
        agy_gemini_dir=str(gemini_root),
        workspace_root=tmp_path,
        chat_db_path=profile_root / "cbi_chat.db",
        watch_agy_profile=True,
    )
    runner = ApiServerRunner(api_cfg, AppConfig(db_path=tmp_path / "idx.db"))
    runner.profile_watcher.set_debounce_windows(transcript_seconds=0.02, sessions_seconds=0.04)

    with TestClient(runner.create_app(), base_url="http://127.0.0.1:8000") as client:
        time.sleep(0.25)
        with client.websocket_connect("/acp", headers=VALID_WS_HEADERS) as ws:
            ws.send_json(_init_msg())
            assert ws.receive_json()["id"] == 1

            _append_bg_launch(transcript, sid)
            launched = _recv_sync_until(
                ws,
                lambda p: any(i["id"] == "tf:2:agent" for i in p.get("items", [])),
            )
            assert launched["turnActive"] is True
            running_page = client.get(
                f"/api/sessions/{sid}/messages?limit_turns=8",
                headers={"host": "127.0.0.1:8000"},
            ).json()
            running_tool = next(i for i in running_page["items"] if i["id"] == "tf:1:tool:0")
            assert running_tool["toolStatus"] == "running"
            assert running_tool["text"] == "All checks passed!"

            _append_jsonl(
                transcript,
                {
                    "step_index": 3,
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "created_at": "2026-11-22T15:36:04Z",
                    "content": (
                        "<SYSTEM_MESSAGE>\n"
                        f"[Message] timestamp=2026-11-22T15:36:04Z sender={sid}/task-982 "
                        f'priority=MESSAGE_PRIORITY_HIGH content=Task id "{sid}/task-982" '
                        "finished with result:\nCommand exited with code 0.\n\nCommand output:\n"
                        f"52 passed\r\n\nLog: file:///tmp/{sid}/task-982.log\n</SYSTEM_MESSAGE>"
                    ),
                },
            )
            _append_jsonl(
                transcript,
                {
                    "step_index": 4,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-22T15:36:06Z",
                    "content": "Finished checking.",
                    "tool_calls": [],
                },
            )
            settled = _recv_sync_until(
                ws,
                lambda p: any(i["id"] == "tf:4:agent" for i in p.get("items", [])),
            )
            assert settled["turnActive"] is False

            _append_jsonl(
                transcript,
                {
                    "step_index": 5,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-22T15:36:10Z",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": "sleep 60"}}],
                },
            )
            _append_jsonl(
                transcript,
                {
                    "step_index": 5,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "RUNNING",
                    "created_at": "2026-11-22T15:36:10Z",
                    "content": (
                        f"Created At: 2026-11-22T15:36:10+00:00\n"
                        f"Tool is running as a background task with task id: {sid}/task-999\n"
                        "YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS: A) wait"
                    ),
                },
            )
            _append_jsonl(
                transcript,
                {
                    "step_index": 6,
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "created_at": "2026-11-22T15:36:12Z",
                    "content": (
                        "<SYSTEM_MESSAGE>\n"
                        f"[Message] timestamp=2026-11-22T15:36:12Z sender={sid}/task-999 "
                        f'priority=MESSAGE_PRIORITY_LOW content=Task id "{sid}/task-999" '
                        "was canceled with result:\nTool execution was canceled\n"
                        f"Log: file:///tmp/{sid}/task-999.log\n</SYSTEM_MESSAGE>"
                    ),
                },
            )
            canceled = _recv_sync_until(
                ws,
                lambda p: any(
                    i["id"] == "tf:5:tool:0" and i.get("toolStatus") == "error"
                    for i in p.get("items", [])
                ),
            )
            canceled_tool = next(i for i in canceled["items"] if i["id"] == "tf:5:tool:0")
            assert canceled_tool["text"] == "Tool execution was canceled"

        final_page = client.get(
            f"/api/sessions/{sid}/messages?limit_turns=8",
            headers={"host": "127.0.0.1:8000"},
        ).json()
    runner.engine.close()

    done_tool = next(i for i in final_page["items"] if i["id"] == "tf:1:tool:0")
    assert done_tool["toolStatus"] == "done"
    assert done_tool["exitCode"] == 0
    assert done_tool["durationSec"] == 15.0
    assert "Log: file://" not in done_tool["text"]
    assert "52 passed" in done_tool["text"]

    _assert_parser_version_self_heal(gemini_root, profile_root, tmp_path, sid)
