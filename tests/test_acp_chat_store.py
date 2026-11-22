"""Tests for SQLite chat timeline persistence (``SqlChatStore``) & zero HTTP chat endpoints."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from api.models.config import AcpServerConfig
from api.services.server import ApiServerRunner
from core.models.chat import (
    ChatItemRole,
    ChatPageQuery,
    ChatPlanStep,
    ChatTimelineItemUpsert,
    ChatTranscriptCursor,
)
from core.models.config import AppConfig
from storage.services.chat import SqlChatStore


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


def test_api_server_mounts_no_http_chat_or_artifact_routes(tmp_path: Path) -> None:
    """``ApiServerRunner`` exposes zero HTTP wrapper routes (``404`` for REST paths)."""
    app = ApiServerRunner(
        AcpServerConfig(),
        AppConfig(db_path=tmp_path / "pure_ws.db"),
    ).create_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/healthz").status_code == 404
        assert client.get("/api/sessions/s1/messages").status_code == 404
        assert client.get("/api/sessions/s1/state").status_code == 404
        assert client.get("/api/sessions/s1/artifacts").status_code == 404
