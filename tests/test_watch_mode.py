"""Tests for watch mode event normalization, scheduler, deletion cleanup, and persistence."""

from collections.abc import Iterator
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import func, or_, select
from sqlalchemy.engine import Connection

from repo_context.config import AppConfig
from repo_context.indexing.events import FileChangeEvent, normalize_event
from repo_context.indexing.incremental import (
    handle_deleted_file,
    process_event_batch,
    reindex_changed_file,
)
from repo_context.indexing.invalidation import invalidate_reference_summaries_for_file
from repo_context.indexing.scheduler import EventScheduler, collapse_events
from repo_context.indexing.watch import _process_watch_batch, make_watch_connection_factory
from repo_context.parsing.pathing import derive_file_id, derive_repo_id
from repo_context.storage import (
    close_connection,
    edges_table,
    get_connection,
    initialize_database,
    nodes_table,
    reference_refresh_table,
    upsert_edges,
    upsert_reference_refresh,
)


@pytest.fixture
def watch_env(tmp_path: Path) -> Iterator[tuple[Connection, Path, Path, AppConfig]]:
    """Yield ``(conn, db_path, repo_root, config)`` and close the connection in teardown."""
    db_path = tmp_path / "watch.db"
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "helper.py").write_text(
        "def helper() -> int:\n    return 1\n", encoding="utf-8"
    )
    (repo_root / "src" / "module.py").write_text(
        "from src.helper import helper\n\ndef hello() -> int:\n    return helper()\n",
        encoding="utf-8",
    )

    conn = get_connection(db_path)
    try:
        initialize_database(conn)
        yield conn, db_path, repo_root, AppConfig()
    finally:
        close_connection(conn)


class TestEventNormalization:
    """Tests for ``normalize_event`` returning ``list[FileChangeEvent]``."""

    def test_created_modified_deleted_return_list_with_repo_id(
        self,
        watch_env: tuple[Connection, Path, Path, AppConfig],
    ) -> None:
        _, _, repo_root, cfg = watch_env
        expected_repo_id = derive_repo_id(repo_root)
        target = repo_root / "src" / "module.py"

        for ev_type in ("created", "modified", "deleted"):
            raw = SimpleNamespace(event_type=ev_type, src_path=str(target), is_directory=False)
            normalized = normalize_event(raw, repo_root, cfg)
            assert len(normalized) == 1
            ev = normalized[0]
            assert ev.event_type == ev_type
            assert ev.repo_relative_path == "src/module.py"
            assert ev.repo_id == expected_repo_id

    def test_moved_event_rename_and_tmp_atomic_save(
        self,
        watch_env: tuple[Connection, Path, Path, AppConfig],
    ) -> None:
        _, _, repo_root, cfg = watch_env
        old_path = repo_root / "src" / "old_mod.py"
        new_path = repo_root / "src" / "module.py"

        move_ev = SimpleNamespace(
            event_type="moved",
            src_path=str(old_path),
            dest_path=str(new_path),
            is_directory=False,
        )
        events = normalize_event(move_ev, repo_root, cfg)
        assert [(e.event_type, e.repo_relative_path) for e in events] == [
            ("deleted", "src/old_mod.py"),
            ("modified", "src/module.py"),
        ]
        assert events[1].old_repo_relative_path == "src/old_mod.py"

        # Atomic save: temp file foo.py.tmp -> src/module.py emits only modified for destination
        atomic_ev = SimpleNamespace(
            event_type="moved",
            src_path=str(repo_root / "src" / "module.py.tmp"),
            dest_path=str(new_path),
            is_directory=False,
        )
        atomic_events = normalize_event(atomic_ev, repo_root, cfg)
        assert [(e.event_type, e.repo_relative_path) for e in atomic_events] == [
            ("modified", "src/module.py")
        ]

    def test_traversal_symlink_ignored_dirs_and_tests_filtering(
        self,
        watch_env: tuple[Connection, Path, Path, AppConfig],
        tmp_path: Path,
    ) -> None:
        _, _, repo_root, cfg = watch_env
        outside = tmp_path / "outside.py"
        outside.write_text("SECRET = 1\n", encoding="utf-8")
        (repo_root / "escape.py").symlink_to(outside)

        assert (
            normalize_event(
                SimpleNamespace(event_type="modified", src_path=str(repo_root / ".." / "outside.py")),
                repo_root,
                cfg,
            )
            == []
        )
        assert (
            normalize_event(
                SimpleNamespace(event_type="created", src_path=str(repo_root / "escape.py")),
                repo_root,
                cfg,
            )
            == []
        )
        assert (
            normalize_event(
                SimpleNamespace(event_type="created", src_path=str(repo_root / "pkg.egg-info" / "a.py")),
                repo_root,
                cfg,
            )
            == []
        )
        assert (
            normalize_event(
                SimpleNamespace(event_type="created", src_path=str(repo_root / "tests" / "test_a.py")),
                repo_root,
                cfg,
            )
            == []
        )

        # include_tests=True allows test files
        (repo_root / "tests").mkdir()
        test_file = repo_root / "tests" / "test_a.py"
        test_file.write_text("def test_x(): pass\n", encoding="utf-8")
        with_tests = normalize_event(
            SimpleNamespace(event_type="created", src_path=str(test_file)),
            repo_root,
            AppConfig(include_tests=True),
        )
        assert len(with_tests) == 1


class TestCollapseAndSerializedScheduler:
    """Tests for sequence-aware collapsing and serialized ``EventScheduler`` execution."""

    def test_collapse_deleted_created_to_modified_and_ephemeral_create_delete(self) -> None:
        # [deleted, created] -> 'modified'
        replaced = collapse_events(
            [
                FileChangeEvent("deleted", "/r/a.py", "a.py", True),
                FileChangeEvent("created", "/r/a.py", "a.py", True),
            ]
        )
        assert [(e.event_type, e.repo_relative_path) for e in replaced] == [("modified", "a.py")]

        # [created, deleted] -> disappeared (empty)
        ephemeral = collapse_events(
            [
                FileChangeEvent("created", "/r/b.py", "b.py", True),
                FileChangeEvent("deleted", "/r/b.py", "b.py", True),
            ]
        )
        assert ephemeral == []

        # [created, modified] -> 'created'
        created_mod = collapse_events(
            [
                FileChangeEvent("created", "/r/c.py", "c.py", True),
                FileChangeEvent("modified", "/r/c.py", "c.py", True),
            ]
        )
        assert [(e.event_type, e.repo_relative_path) for e in created_mod] == [("created", "c.py")]

    def test_scheduler_serializes_batches_on_single_worker_thread(self) -> None:
        active = 0
        max_active = 0
        batches: list[list[FileChangeEvent]] = []
        first_started = threading.Event()
        lock = threading.Lock()

        def on_batch(batch: list[FileChangeEvent]) -> None:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            first_started.set()
            time.sleep(0.05)
            with lock:
                active -= 1
            batches.append(batch)

        scheduler = EventScheduler(debounce_ms=20, on_batch_ready=on_batch)
        try:
            scheduler.submit(FileChangeEvent("modified", "/r/a.py", "a.py", True))
            assert first_started.wait(timeout=1.0)
            scheduler.submit(FileChangeEvent("modified", "/r/b.py", "b.py", True))
        finally:
            scheduler.stop()

        assert max_active == 1
        assert len(batches) == 2


class TestIncrementalDeletionRollbackAndCustomDbPath:
    """Tests for deletion cleanup, parse failure rollback, and watch batching."""

    def test_handle_deleted_file_leaves_zero_orphan_edges_and_marks_external_stale(
        self,
        watch_env: tuple[Connection, Path, Path, AppConfig],
    ) -> None:
        conn, _, repo_root, cfg = watch_env
        repo_id = derive_repo_id(repo_root)

        assert reindex_changed_file(conn, repo_root, "src/helper.py", cfg)["status"] == "reindexed"
        assert reindex_changed_file(conn, repo_root, "src/module.py", cfg)["status"] == "reindexed"

        helper_sym_id = str(
            conn.execute(
                select(nodes_table.c.id).where(
                    nodes_table.c.name == "helper",
                    nodes_table.c.kind == "function",
                )
            ).scalar_one()
        )
        hello_sym_id = str(
            conn.execute(
                select(nodes_table.c.id).where(
                    nodes_table.c.name == "hello",
                    nodes_table.c.kind == "function",
                )
            ).scalar_one()
        )
        helper_file_id = derive_file_id(repo_id, "src/helper.py")
        module_file_id = derive_file_id(repo_id, "src/module.py")

        # Add an LSP reference edge from module.py -> helper and mark helper refreshed
        upsert_edges(
            conn,
            [
                {
                    "id": f"edge:{repo_id}:references:hello->helper",
                    "repo_id": repo_id,
                    "kind": "references",
                    "from_id": hello_sym_id,
                    "to_id": helper_sym_id,
                    "source": "lsp",
                    "confidence": 0.9,
                    "evidence_file_id": module_file_id,
                    "evidence_uri": (repo_root / "src" / "module.py").as_uri(),
                    "payload_json": "{}",
                    "last_indexed_at": "2026-01-01T00:00:00Z",
                }
            ],
        )
        upsert_reference_refresh(conn, helper_sym_id, True, "2026-01-01T00:00:00Z", "ok")
        upsert_reference_refresh(conn, hello_sym_id, True, "2026-01-01T00:00:00Z", "ok")
        conn.commit()

        # Invalidate reference summaries for module.py must also mark external helper stale
        removed_refs = invalidate_reference_summaries_for_file(conn, module_file_id)
        conn.commit()
        assert removed_refs == 1
        assert (
            conn.execute(
                select(reference_refresh_table.c.available).where(
                    reference_refresh_table.c.target_symbol_id == helper_sym_id
                )
            ).scalar_one()
            is False
        )

        # Delete src/helper.py and verify zero orphan edges remain pointing to helper_sym_id
        summary = handle_deleted_file(conn, repo_id, "src/helper.py")
        assert summary["status"] == "deleted"
        assert summary["deleted_node_count"] > 0

        orphan_count = int(
            conn.execute(
                select(func.count())
                .select_from(edges_table)
                .where(
                    or_(
                        edges_table.c.from_id == helper_sym_id,
                        edges_table.c.to_id == helper_sym_id,
                        edges_table.c.evidence_file_id == helper_file_id,
                    )
                )
            ).scalar_one()
        )
        assert orphan_count == 0

    def test_parse_error_rollback_preserves_prior_graph_and_rejects_traversal(
        self,
        watch_env: tuple[Connection, Path, Path, AppConfig],
        tmp_path: Path,
    ) -> None:
        conn, _, repo_root, cfg = watch_env
        repo_id = derive_repo_id(repo_root)
        file_id = derive_file_id(repo_id, "src/module.py")

        assert reindex_changed_file(conn, repo_root, "src/module.py", cfg)["status"] == "reindexed"
        nodes_before = list(
            conn.execute(
                select(nodes_table.c.id)
                .where(nodes_table.c.file_id == file_id)
                .order_by(nodes_table.c.id.asc())
            ).scalars()
        )
        assert len(nodes_before) >= 2

        # Write syntax error and verify parse_failed preserves previous nodes
        (repo_root / "src" / "module.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
        failed = reindex_changed_file(conn, repo_root, "src/module.py", cfg)
        assert failed["status"] == "parse_failed"
        nodes_after = list(
            conn.execute(
                select(nodes_table.c.id)
                .where(nodes_table.c.file_id == file_id)
                .order_by(nodes_table.c.id.asc())
            ).scalars()
        )
        assert nodes_after == nodes_before

        # Traversal outside repo_root is rejected
        outside = tmp_path / "outside.py"
        outside.write_text("X = 1\n", encoding="utf-8")
        err = reindex_changed_file(conn, repo_root, "../outside.py", cfg)
        assert err["status"] == "error"

    def test_custom_db_path_persistence_in_watch_batching(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """make_watch_connection_factory + _process_watch_batch persists to custom --db-path and emits JSON."""
        custom_db = tmp_path / "nested" / "custom_watch.db"
        repo_root = tmp_path / "watch_repo"
        repo_root.mkdir()
        py_file = repo_root / "app.py"
        py_file.write_text("def run():\n    return 42\n", encoding="utf-8")

        init_conn = get_connection(custom_db)
        try:
            initialize_database(init_conn)
        finally:
            close_connection(init_conn)

        cfg = AppConfig(db_path=custom_db)
        events = normalize_event(
            SimpleNamespace(event_type="created", src_path=str(py_file), is_directory=False),
            repo_root,
            cfg,
        )
        assert len(events) == 1

        factory = make_watch_connection_factory(custom_db)
        _process_watch_batch(factory, repo_root, events, cfg, verbose=False)
        watch_out = json.loads(capsys.readouterr().out)
        assert watch_out["event"] == "file_processed"
        assert watch_out["status"] == "reindexed"
        assert watch_out["file_path"] == "app.py"

        verify_conn = get_connection(custom_db, read_only=True)
        try:
            names = set(verify_conn.execute(select(nodes_table.c.name)).scalars())
            assert "run" in names
        finally:
            close_connection(verify_conn)

        # Ensure batch processing worked via process_event_batch too
        py_file.unlink()
        del_events = normalize_event(
            SimpleNamespace(event_type="deleted", src_path=str(py_file), is_directory=False),
            repo_root,
            cfg,
        )
        write_conn = get_connection(custom_db)
        try:
            res = process_event_batch(write_conn, repo_root, del_events, cfg)
            assert res[0]["status"] == "deleted"
        finally:
            close_connection(write_conn)

    def test_incremental_reindex_preserves_incoming_edges_and_resets_removed_symbol(
        self,
        tmp_path: Path,
    ) -> None:
        """Re-indexing b.py preserves a.py incoming edges and resets removed targets cleanly."""
        repo_root = tmp_path / "inc_repo"
        repo_root.mkdir()
        b_file = repo_root / "b.py"
        b_file.write_text(
            "def helper() -> int:\n    return 1\n\ndef keep() -> int:\n    return 2\n",
            encoding="utf-8",
        )
        a_file = repo_root / "a.py"
        a_file.write_text(
            "from b import helper\n"
            "from b import keep\n\n"
            "def caller() -> int:\n"
            "    return helper() + keep()\n",
            encoding="utf-8",
        )

        db_path = tmp_path / "inc_edges.db"
        cfg = AppConfig()
        conn = get_connection(db_path)
        try:
            initialize_database(conn)
            repo_id = derive_repo_id(repo_root)
            a_file_id = derive_file_id(repo_id, "a.py")

            assert reindex_changed_file(conn, repo_root, "b.py", cfg)["status"] == "reindexed"
            assert reindex_changed_file(conn, repo_root, "a.py", cfg)["status"] == "reindexed"

            def _fn_id(name: str) -> str:
                return str(
                    conn.execute(
                        select(nodes_table.c.id).where(
                            nodes_table.c.name == name,
                            nodes_table.c.kind == "function",
                        )
                    ).scalar_one()
                )

            helper_id = _fn_id("helper")
            keep_id = _fn_id("keep")
            caller_id = _fn_id("caller")

            # Seed an LSP reference edge from unmodified a.py -> b.helper
            upsert_edges(
                conn,
                [
                    {
                        "id": f"edge:{repo_id}:references:caller->helper",
                        "repo_id": repo_id,
                        "kind": "references",
                        "from_id": caller_id,
                        "to_id": helper_id,
                        "source": "lsp",
                        "confidence": 0.9,
                        "evidence_file_id": a_file_id,
                        "evidence_uri": a_file.as_uri(),
                        "payload_json": "{}",
                        "last_indexed_at": "2026-01-01T00:00:00Z",
                    }
                ],
            )
            conn.commit()

            # 1. Edit b.py while keeping helper & keep -> incoming a.py edges stay intact
            b_file.write_text(
                "def helper() -> int:\n    return 10\n\ndef keep() -> int:\n    return 2\n",
                encoding="utf-8",
            )
            assert reindex_changed_file(conn, repo_root, "b.py", cfg)["status"] == "reindexed"

            incoming_kinds = set(
                conn.execute(
                    select(edges_table.c.kind).where(
                        edges_table.c.repo_id == repo_id,
                        edges_table.c.to_id == helper_id,
                        edges_table.c.evidence_file_id == a_file_id,
                    )
                ).scalars()
            )
            assert incoming_kinds == {"imports", "calls", "references"}

            # 2. Remove helper from b.py -> a.py edges reset & dangling references pruned
            b_file.write_text("def keep() -> int:\n    return 2\n", encoding="utf-8")
            assert reindex_changed_file(conn, repo_root, "b.py", cfg)["status"] == "reindexed"

            a_edges = (
                conn.execute(
                    select(edges_table.c.kind, edges_table.c.to_id).where(
                        edges_table.c.repo_id == repo_id,
                        edges_table.c.evidence_file_id == a_file_id,
                    )
                )
                .mappings()
                .all()
            )
            by_kind_to = {(row["kind"], row["to_id"]) for row in a_edges}
            assert ("imports", "external_or_unresolved:b.helper") in by_kind_to
            assert ("calls", "unresolved_call:helper") in by_kind_to
            assert ("imports", keep_id) in by_kind_to
            assert ("calls", keep_id) in by_kind_to
            assert (
                int(
                    conn.execute(
                        select(func.count())
                        .select_from(edges_table)
                        .where(
                            edges_table.c.repo_id == repo_id,
                            edges_table.c.kind == "references",
                        )
                    ).scalar_one()
                )
                == 0
            )
        finally:
            close_connection(conn)
