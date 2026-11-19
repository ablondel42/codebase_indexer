"""Tests for watch event normalization, batch scheduling, edge recovery, and ``RepoWatcher``."""

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileSystemEventHandler
from watchdog.observers.api import BaseObserver

from core.models.base import FrozenModel
from core.models.config import AppConfig
from core.models.edge import Edge
from core.models.enums import EdgeKind, RefreshStatus, SymbolKind
from core.models.runs import ReferenceRefreshState
from core.ports.uow import UnitOfWorkProtocol
from core.utils.pathing import derive_file_id, derive_repo_id
from indexing.models.events import (
    FileChangeEvent,
    FileEventType,
    NormalizeContext,
    WatchFileProcessedEvent,
    WatchStartedEvent,
    WatchStoppedEvent,
)
from indexing.models.results import IncrementalStatus
from indexing.services.incremental import IncrementalIndexer
from indexing.services.linker_runner import RepoLinkCoordinator
from indexing.services.normalizer import WatchEventNormalizer
from indexing.services.scheduler import EventScheduler
from indexing.services.watcher import RepoWatcher
from indexing.utils.collapse import collapse_events, collapse_path_events
from indexing.utils.watch_paths import (
    extract_watchdog_paths,
    has_symlink_below_root,
    is_ignored_watch_path,
    is_supported_watch_file,
)
from parsing.models.linking import LinkResolutionReport
from storage.services.uow import SqlUnitOfWorkFactory


class _CountingLinkCoordinator(RepoLinkCoordinator):
    """Test spy counting ``RepoLinkCoordinator.link_repository`` invocations."""

    def __init__(self) -> None:
        """Initialize the counting link coordinator spy."""
        super().__init__()
        self.call_count = 0

    def link_repository(
        self,
        uow: UnitOfWorkProtocol,
        repo_id: str,
        *,
        file_id: str | None = None,
    ) -> LinkResolutionReport:
        """Delegate to ``super().link_repository`` while incrementing ``call_count``."""
        self.call_count += 1
        return super().link_repository(uow, repo_id, file_id=file_id)


class _FakeWatchObserver:
    """Deterministic in-memory observer driving ``RepoWatcher`` tests without OS race delays."""

    def __init__(self, on_started: Any) -> None:
        """Initialize the fake observer with a startup hook."""
        self.handler: FileSystemEventHandler | None = None
        self.watched_path: str | None = None
        self._alive = False
        self._on_started = on_started

    def schedule(
        self,
        event_handler: FileSystemEventHandler,
        path: str,
        *,
        recursive: bool = False,
    ) -> None:
        """Record the scheduled handler and watched root path."""
        _ = recursive
        self.handler = event_handler
        self.watched_path = path

    def start(self) -> None:
        """Mark observer alive and spawn the deterministic event driver thread."""
        self._alive = True
        driver = threading.Thread(target=self._on_started, args=(self,), daemon=True)
        driver.start()

    def stop(self) -> None:
        """Mark the fake observer stopped."""
        self._alive = False

    def is_alive(self) -> bool:
        """Return whether the fake observer is currently running."""
        return self._alive

    def join(self, timeout: float | None = None) -> None:
        """No-op join matching ``BaseObserver.join``."""
        _ = timeout


@pytest.fixture
def watch_env(tmp_path: Path) -> Iterator[tuple[SqlUnitOfWorkFactory, Path, Path, AppConfig]]:
    """Create a hermetic SQLite UoW factory and sample two-file repository in ``tmp_path``."""
    db_path = tmp_path / "watch.db"
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "helper.py").write_text(
        "def helper() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "module.py").write_text(
        "from src.helper import helper\n\ndef hello() -> int:\n    return helper()\n",
        encoding="utf-8",
    )

    cfg = AppConfig(db_path=db_path)
    factory = SqlUnitOfWorkFactory(db_path)
    with factory.open(initialize=True):
        pass
    try:
        yield factory, db_path, repo_root, cfg
    finally:
        factory.close()


def _find_symbol_id(uow: UnitOfWorkProtocol, repo_id: str, name: str, kind: SymbolKind) -> str:
    """Return the unique symbol ID matching ``(repo_id, name, kind)``."""
    matches = uow.nodes.find_by_name(repo_id, name, kind=kind)
    assert len(matches) == 1
    return matches[0].id


class TestEventNormalization:
    """Tests for ``WatchEventNormalizer`` and ``indexing.utils.watch_paths`` helpers."""

    def test_created_modified_deleted_return_list_with_repo_id(
        self,
        watch_env: tuple[SqlUnitOfWorkFactory, Path, Path, AppConfig],
    ) -> None:
        """Created, modified, and deleted events normalize to ``FileChangeEvent`` items."""
        _, _, repo_root, cfg = watch_env
        normalizer = WatchEventNormalizer(cfg)
        ctx = NormalizeContext.from_config(repo_root, cfg)
        expected_repo_id = derive_repo_id(repo_root)
        target = repo_root / "src" / "module.py"
        assert ctx.repo_id == expected_repo_id

        for ev_type in (FileEventType.CREATED, FileEventType.MODIFIED, FileEventType.DELETED):
            raw = SimpleNamespace(event_type=str(ev_type), src_path=str(target), is_directory=False)
            normalized = normalizer.normalize(raw, repo_root)
            assert len(normalized) == 1
            ev = normalized[0]
            assert ev.event_type == ev_type
            assert ev.repo_relative_path == "src/module.py"
            assert ev.repo_id == expected_repo_id

    def test_moved_event_rename_and_tmp_atomic_save(
        self,
        watch_env: tuple[SqlUnitOfWorkFactory, Path, Path, AppConfig],
    ) -> None:
        """Renames emit ``[deleted, modified]`` while ``.tmp`` atomic saves emit ``[modified]``."""
        _, _, repo_root, cfg = watch_env
        normalizer = WatchEventNormalizer(cfg)
        old_path = repo_root / "src" / "old_mod.py"
        new_path = repo_root / "src" / "module.py"

        move_ev = SimpleNamespace(
            event_type="moved",
            src_path=str(old_path),
            dest_path=str(new_path),
            is_directory=False,
        )
        events = normalizer.normalize(move_ev, repo_root)
        assert [(e.event_type, e.repo_relative_path) for e in events] == [
            (FileEventType.DELETED, "src/old_mod.py"),
            (FileEventType.MODIFIED, "src/module.py"),
        ]
        assert events[1].old_repo_relative_path == "src/old_mod.py"

        atomic_ev = SimpleNamespace(
            event_type="moved",
            src_path=str(repo_root / "src" / "module.py.tmp"),
            dest_path=str(new_path),
            is_directory=False,
        )
        atomic_events = normalizer.normalize(atomic_ev, repo_root)
        assert [(e.event_type, e.repo_relative_path) for e in atomic_events] == [
            (FileEventType.MODIFIED, "src/module.py"),
        ]

    def test_traversal_symlink_ignored_dirs_and_tests_filtering(
        self,
        watch_env: tuple[SqlUnitOfWorkFactory, Path, Path, AppConfig],
        tmp_path: Path,
    ) -> None:
        """Symlinks, traversals, ignored globs, and excluded test paths are rejected."""
        _, _, repo_root, cfg = watch_env
        normalizer = WatchEventNormalizer(cfg)
        outside = tmp_path / "outside.py"
        outside.write_text("SECRET = 1\n", encoding="utf-8")
        symlink_file = repo_root / "escape.py"
        symlink_file.symlink_to(outside)

        assert has_symlink_below_root(symlink_file, repo_root, repo_root.resolve()) is True
        assert is_ignored_watch_path("pkg.egg-info/a.py", cfg.ignored_dirs) is True
        assert is_supported_watch_file("src/module.py", cfg.supported_extensions) is True
        assert is_supported_watch_file("README.md", cfg.supported_extensions) is False
        assert extract_watchdog_paths(SimpleNamespace(is_directory=True, src_path="x")) == (
            None,
            None,
            None,
        )

        rejected_cases = [
            SimpleNamespace(event_type="modified", src_path=str(repo_root / ".." / "outside.py")),
            SimpleNamespace(event_type="created", src_path=str(symlink_file)),
            SimpleNamespace(
                event_type="created",
                src_path=str(repo_root / "pkg.egg-info" / "a.py"),
            ),
            SimpleNamespace(event_type="created", src_path=str(repo_root / "tests" / "test_a.py")),
        ]
        for raw in rejected_cases:
            assert normalizer.normalize(raw, repo_root) == []

        (repo_root / "tests").mkdir()
        test_file = repo_root / "tests" / "test_a.py"
        test_file.write_text("def test_x() -> None:\n    pass\n", encoding="utf-8")
        with_tests = WatchEventNormalizer(AppConfig(include_tests=True)).normalize(
            SimpleNamespace(event_type="created", src_path=str(test_file)),
            repo_root,
        )
        assert len(with_tests) == 1


class TestCollapseAndSerializedScheduler:
    """Tests for sequence-aware event collapsing and serialized ``EventScheduler`` batches."""

    def test_collapse_deleted_created_to_modified_and_ephemeral_create_delete(self) -> None:
        """``collapse_events`` and ``collapse_path_events`` preserve net lifecycle semantics."""
        assert collapse_path_events([]) is None

        replaced = collapse_events(
            [
                FileChangeEvent(
                    event_type=FileEventType.DELETED,
                    absolute_path="/r/a.py",
                    repo_relative_path="a.py",
                ),
                FileChangeEvent(
                    event_type=FileEventType.CREATED,
                    absolute_path="/r/a.py",
                    repo_relative_path="a.py",
                ),
            ]
        )
        assert [(e.event_type, e.repo_relative_path) for e in replaced] == [
            (FileEventType.MODIFIED, "a.py")
        ]

        ephemeral = collapse_events(
            [
                FileChangeEvent(
                    event_type=FileEventType.CREATED,
                    absolute_path="/r/b.py",
                    repo_relative_path="b.py",
                ),
                FileChangeEvent(
                    event_type=FileEventType.DELETED,
                    absolute_path="/r/b.py",
                    repo_relative_path="b.py",
                ),
            ]
        )
        assert ephemeral == []

        created_mod = collapse_events(
            [
                FileChangeEvent(
                    event_type=FileEventType.CREATED,
                    absolute_path="/r/c.py",
                    repo_relative_path="c.py",
                ),
                FileChangeEvent(
                    event_type=FileEventType.MODIFIED,
                    absolute_path="/r/c.py",
                    repo_relative_path="c.py",
                ),
            ]
        )
        assert [(e.event_type, e.repo_relative_path) for e in created_mod] == [
            (FileEventType.CREATED, "c.py")
        ]

    def test_scheduler_serializes_batches_on_single_worker_thread(self) -> None:
        """``EventScheduler`` never overlaps batch handlers and flushes pending items on exit."""
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

        with EventScheduler(debounce_ms=20, on_batch_ready=on_batch) as scheduler:
            scheduler.submit(
                FileChangeEvent(
                    event_type=FileEventType.MODIFIED,
                    absolute_path="/r/a.py",
                    repo_relative_path="a.py",
                )
            )
            assert first_started.wait(timeout=1.0)
            scheduler.submit(
                FileChangeEvent(
                    event_type=FileEventType.MODIFIED,
                    absolute_path="/r/b.py",
                    repo_relative_path="b.py",
                )
            )

        assert max_active == 1
        assert len(batches) == 2


class TestIncrementalBatchLinkingAndEdgeRecovery:
    """Tests for single-pass batch linking, non-destructive edge reset, and rollback."""

    def test_process_batch_runs_link_repository_once_for_multi_file_batch(
        self,
        watch_env: tuple[SqlUnitOfWorkFactory, Path, Path, AppConfig],
    ) -> None:
        """``IncrementalIndexer.process_batch`` invokes ``RepoLinkCoordinator`` once per batch."""
        factory, _, repo_root, cfg = watch_env
        extra_file = repo_root / "src" / "extra.py"
        extra_file.write_text(
            "from src.module import hello\n\ndef run_all() -> int:\n    return hello()\n",
            encoding="utf-8",
        )

        spy_linker = _CountingLinkCoordinator()
        incremental = IncrementalIndexer(config=cfg, link_coordinator=spy_linker)
        events = [
            FileChangeEvent(
                event_type=FileEventType.CREATED,
                absolute_path=str(repo_root / "src" / "helper.py"),
                repo_relative_path="src/helper.py",
            ),
            FileChangeEvent(
                event_type=FileEventType.CREATED,
                absolute_path=str(repo_root / "src" / "module.py"),
                repo_relative_path="src/module.py",
            ),
            FileChangeEvent(
                event_type=FileEventType.CREATED,
                absolute_path=str(extra_file),
                repo_relative_path="src/extra.py",
            ),
        ]

        with factory.open() as uow:
            batch_res = incremental.process_batch(uow, repo_root, events)

        assert spy_linker.call_count == 1
        assert [item.status for item in batch_res.results] == [
            IncrementalStatus.REINDEXED,
            IncrementalStatus.REINDEXED,
            IncrementalStatus.REINDEXED,
        ]
        assert batch_res.link_report is not None
        assert batch_res.link_report.total_resolved >= 4

    def test_deleted_target_file_resets_caller_ast_edges_and_relinks_on_restore(
        self,
        tmp_path: Path,
    ) -> None:
        """Deleting a target file resets caller AST edges to placeholders and re-links on return."""
        repo_root = tmp_path / "recovery_repo"
        (repo_root / "pkg").mkdir(parents=True)
        (repo_root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        target_source = (
            "class BaseService:\n"
            "    def ping(self) -> int:\n"
            "        return 1\n\n"
            "def compute() -> int:\n"
            "    return 42\n"
        )
        target_file = repo_root / "pkg" / "target.py"
        caller_file = repo_root / "pkg" / "caller.py"
        target_file.write_text(target_source, encoding="utf-8")
        caller_file.write_text(
            "from pkg.target import BaseService, compute\n\n"
            "class DerivedService(BaseService):\n"
            "    def execute(self) -> int:\n"
            "        return compute()\n",
            encoding="utf-8",
        )

        db_path = tmp_path / "recovery.db"
        cfg = AppConfig(db_path=db_path)
        incremental = IncrementalIndexer(config=cfg)
        repo_id = derive_repo_id(repo_root)
        caller_file_id = derive_file_id(repo_id, "pkg/caller.py")

        factory = SqlUnitOfWorkFactory(db_path)
        try:
            with factory.open(initialize=True) as uow:
                incremental.process_batch(
                    uow,
                    repo_root,
                    [
                        FileChangeEvent(
                            event_type=FileEventType.CREATED,
                            absolute_path=str(repo_root / "pkg" / "__init__.py"),
                            repo_relative_path="pkg/__init__.py",
                        ),
                        FileChangeEvent(
                            event_type=FileEventType.CREATED,
                            absolute_path=str(target_file),
                            repo_relative_path="pkg/target.py",
                        ),
                        FileChangeEvent(
                            event_type=FileEventType.CREATED,
                            absolute_path=str(caller_file),
                            repo_relative_path="pkg/caller.py",
                        ),
                    ],
                )

            with factory.open() as uow:
                base_id = _find_symbol_id(uow, repo_id, "BaseService", SymbolKind.CLASS)
                compute_id = _find_symbol_id(uow, repo_id, "compute", SymbolKind.FUNCTION)
                execute_id = _find_symbol_id(uow, repo_id, "execute", SymbolKind.METHOD)
                uow.edges.upsert(
                    Edge(
                        id=f"edge:{repo_id}:references:execute->compute",
                        repo_id=repo_id,
                        kind=EdgeKind.REFERENCES,
                        from_id=execute_id,
                        to_id=compute_id,
                        source="lsp",
                        confidence=0.95,
                        evidence_file_id=caller_file_id,
                        evidence_uri=caller_file.resolve().as_uri(),
                    )
                )
                uow.commit()

            # 1. Delete pkg/target.py -> caller AST edges reset to placeholders; LSP refs deleted
            target_file.unlink()
            with factory.open() as uow:
                del_batch = incremental.process_batch(
                    uow,
                    repo_root,
                    [
                        FileChangeEvent(
                            event_type=FileEventType.DELETED,
                            absolute_path=str(target_file),
                            repo_relative_path="pkg/target.py",
                            repo_id=repo_id,
                        )
                    ],
                )

            assert del_batch.link_report is not None
            assert del_batch.link_report.edges_reset >= 4
            assert del_batch.results[0].status == IncrementalStatus.DELETED
            assert del_batch.results[0].reset_edge_count >= 4

            with factory.open(read_only=True) as uow:
                assert uow.edges.list_for_repo(repo_id, kinds=[EdgeKind.REFERENCES]) == []
                caller_edges = uow.edges.list_for_file(caller_file_id)
                reset_pairs = {(edge.kind, edge.to_id) for edge in caller_edges}
                assert (EdgeKind.IMPORTS, "external_or_unresolved:pkg.target.BaseService") in (
                    reset_pairs
                )
                assert (EdgeKind.IMPORTS, "external_or_unresolved:pkg.target.compute") in (
                    reset_pairs
                )
                assert (EdgeKind.INHERITS, "unresolved_base:BaseService") in reset_pairs
                assert (EdgeKind.CALLS, "unresolved_call:compute") in reset_pairs

            # 2. Restore pkg/target.py -> surviving caller AST edges automatically re-link!
            target_file.write_text(target_source, encoding="utf-8")
            with factory.open() as uow:
                restore_batch = incremental.process_batch(
                    uow,
                    repo_root,
                    [
                        FileChangeEvent(
                            event_type=FileEventType.CREATED,
                            absolute_path=str(target_file),
                            repo_relative_path="pkg/target.py",
                            repo_id=repo_id,
                        )
                    ],
                )

            assert restore_batch.link_report is not None
            assert restore_batch.link_report.total_resolved >= 4

            with factory.open(read_only=True) as uow:
                restored_edges = uow.edges.list_for_file(caller_file_id)
                restored_pairs = {(edge.kind, edge.to_id) for edge in restored_edges}
                assert (EdgeKind.IMPORTS, base_id) in restored_pairs
                assert (EdgeKind.IMPORTS, compute_id) in restored_pairs
                assert (EdgeKind.INHERITS, base_id) in restored_pairs
                assert (EdgeKind.CALLS, compute_id) in restored_pairs
        finally:
            factory.close()

    def test_delete_file_marks_external_stale_and_handles_untracked(
        self,
        watch_env: tuple[SqlUnitOfWorkFactory, Path, Path, AppConfig],
    ) -> None:
        """Reference invalidation marks external targets stale and deletes LSP edges cleanly."""
        factory, _, repo_root, cfg = watch_env
        incremental = IncrementalIndexer(config=cfg)
        repo_id = derive_repo_id(repo_root)
        module_file_id = derive_file_id(repo_id, "src/module.py")

        with factory.open() as uow:
            assert incremental.reindex_file(uow, repo_root, "src/helper.py").status == (
                IncrementalStatus.REINDEXED
            )
            assert incremental.reindex_file(uow, repo_root, "src/module.py").status == (
                IncrementalStatus.REINDEXED
            )
            helper_id = _find_symbol_id(uow, repo_id, "helper", SymbolKind.FUNCTION)
            hello_id = _find_symbol_id(uow, repo_id, "hello", SymbolKind.FUNCTION)

            uow.edges.upsert(
                Edge(
                    id=f"edge:{repo_id}:references:hello->helper",
                    repo_id=repo_id,
                    kind=EdgeKind.REFERENCES,
                    from_id=hello_id,
                    to_id=helper_id,
                    source="lsp",
                    confidence=0.9,
                    evidence_file_id=module_file_id,
                    evidence_uri=(repo_root / "src" / "module.py").resolve().as_uri(),
                    last_indexed_at="2026-01-01T00:00:00Z",
                )
            )
            uow.refresh.upsert(
                ReferenceRefreshState(
                    target_symbol_id=helper_id,
                    available=True,
                    last_refreshed_at="2026-01-01T00:00:00Z",
                    refresh_status=RefreshStatus.OK,
                )
            )
            uow.refresh.upsert(
                ReferenceRefreshState(
                    target_symbol_id=hello_id,
                    available=True,
                    last_refreshed_at="2026-01-01T00:00:00Z",
                    refresh_status=RefreshStatus.OK,
                )
            )
            uow.commit()

            removed_refs = uow.refresh.invalidate_for_file(module_file_id)
            uow.commit()
            assert removed_refs == 1
            helper_state = uow.refresh.get(helper_id)
            assert helper_state.available is False

            del_res = incremental.delete_file(uow, repo_id, "src/helper.py")
            assert del_res.status == IncrementalStatus.DELETED
            assert del_res.deleted_node_count > 0
            assert uow.edges.list_incoming(helper_id) == []

            untracked = incremental.delete_file(uow, repo_id, "src/never_existed.py")
            assert untracked.status == IncrementalStatus.NOT_TRACKED

    def test_parse_error_rollback_preserves_prior_graph_and_rejects_traversal(
        self,
        watch_env: tuple[SqlUnitOfWorkFactory, Path, Path, AppConfig],
        tmp_path: Path,
    ) -> None:
        """Syntax failures keep prior persisted nodes intact and path escapes return ``ERROR``."""
        factory, _, repo_root, cfg = watch_env
        incremental = IncrementalIndexer(config=cfg)
        repo_id = derive_repo_id(repo_root)
        file_id = derive_file_id(repo_id, "src/module.py")

        with factory.open() as uow:
            res = incremental.reindex_file(uow, repo_root, "src/module.py")
            assert res.status == IncrementalStatus.REINDEXED
            nodes_before = [n.id for n in uow.nodes.list_for_file(file_id)]
            assert len(nodes_before) >= 2

            (repo_root / "src" / "module.py").write_text(
                "def broken(:\n    pass\n",
                encoding="utf-8",
            )
            failed = incremental.reindex_file(uow, repo_root, "src/module.py")
            assert failed.status == IncrementalStatus.PARSE_FAILED
            nodes_after = [n.id for n in uow.nodes.list_for_file(file_id)]
            assert nodes_after == nodes_before

            outside = tmp_path / "outside.py"
            outside.write_text("X = 1\n", encoding="utf-8")
            err = incremental.reindex_file(uow, repo_root, "../outside.py")
            assert err.status == IncrementalStatus.ERROR

    def test_incremental_reindex_preserves_incoming_edges_and_resets_removed_symbol(
        self,
        tmp_path: Path,
    ) -> None:
        """Editing ``b.py`` keeps valid incoming edges and resets removed symbol edges cleanly."""
        repo_root = tmp_path / "inc_repo"
        repo_root.mkdir()
        b_file = repo_root / "b.py"
        a_file = repo_root / "a.py"
        b_file.write_text(
            "def helper() -> int:\n    return 1\n\ndef keep() -> int:\n    return 2\n",
            encoding="utf-8",
        )
        a_file.write_text(
            "from b import helper\nfrom b import keep\n\ndef caller() -> int:\n"
            "    return helper() + keep()\n",
            encoding="utf-8",
        )

        db_path = tmp_path / "inc_edges.db"
        cfg = AppConfig(db_path=db_path)
        incremental = IncrementalIndexer(config=cfg)
        repo_id = derive_repo_id(repo_root)
        a_file_id = derive_file_id(repo_id, "a.py")

        factory = SqlUnitOfWorkFactory(db_path)
        try:
            with factory.open(initialize=True) as uow:
                assert incremental.reindex_file(uow, repo_root, "b.py").status == (
                    IncrementalStatus.REINDEXED
                )
                assert incremental.reindex_file(uow, repo_root, "a.py").status == (
                    IncrementalStatus.REINDEXED
                )
                helper_id = _find_symbol_id(uow, repo_id, "helper", SymbolKind.FUNCTION)
                keep_id = _find_symbol_id(uow, repo_id, "keep", SymbolKind.FUNCTION)
                caller_id = _find_symbol_id(uow, repo_id, "caller", SymbolKind.FUNCTION)

                uow.edges.upsert(
                    Edge(
                        id=f"edge:{repo_id}:references:caller->helper",
                        repo_id=repo_id,
                        kind=EdgeKind.REFERENCES,
                        from_id=caller_id,
                        to_id=helper_id,
                        source="lsp",
                        confidence=0.9,
                        evidence_file_id=a_file_id,
                        evidence_uri=a_file.resolve().as_uri(),
                    )
                )
                uow.commit()

                # 1. Edit b.py while keeping helper & keep -> incoming edges stay intact
                b_file.write_text(
                    "def helper() -> int:\n    return 10\n\ndef keep() -> int:\n    return 2\n",
                    encoding="utf-8",
                )
                assert incremental.reindex_file(uow, repo_root, "b.py").status == (
                    IncrementalStatus.REINDEXED
                )
                incoming_kinds = {
                    edge.kind
                    for edge in uow.edges.list_incoming(helper_id)
                    if edge.evidence_file_id == a_file_id
                }
                assert incoming_kinds == {EdgeKind.IMPORTS, EdgeKind.CALLS, EdgeKind.REFERENCES}

                # 2. Remove helper from b.py -> helper edges reset & LSP references pruned
                b_file.write_text("def keep() -> int:\n    return 2\n", encoding="utf-8")
                assert incremental.reindex_file(uow, repo_root, "b.py").status == (
                    IncrementalStatus.REINDEXED
                )
                by_kind_to = {(e.kind, e.to_id) for e in uow.edges.list_for_file(a_file_id)}
                assert (EdgeKind.IMPORTS, "external_or_unresolved:b.helper") in by_kind_to
                assert (EdgeKind.CALLS, "unresolved_call:helper") in by_kind_to
                assert (EdgeKind.IMPORTS, keep_id) in by_kind_to
                assert (EdgeKind.CALLS, keep_id) in by_kind_to
                assert uow.edges.list_for_repo(repo_id, kinds=[EdgeKind.REFERENCES]) == []
        finally:
            factory.close()


class TestRepoWatcherEventsAndSilence:
    """Tests for ``RepoWatcher`` callback events, custom DB persistence, and stdout silence."""

    def test_repo_watcher_emits_typed_events_to_custom_db_without_stdout_pollution(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``RepoWatcher`` emits typed events via ``EventCallback`` and writes nothing to stdout."""
        custom_db = tmp_path / "nested" / "custom_watch.db"
        repo_root = tmp_path / "watch_repo"
        repo_root.mkdir()
        py_file = repo_root / "app.py"

        cfg = AppConfig(db_path=custom_db)
        factory = SqlUnitOfWorkFactory(custom_db)
        stop_event = threading.Event()
        reindexed_gate = threading.Event()
        deleted_gate = threading.Event()
        emitted: list[FrozenModel] = []

        def on_event(ev: FrozenModel) -> None:
            emitted.append(ev)
            if isinstance(ev, WatchFileProcessedEvent):
                if ev.status == IncrementalStatus.REINDEXED:
                    reindexed_gate.set()
                elif ev.status == IncrementalStatus.DELETED:
                    deleted_gate.set()

        def drive_events(obs: _FakeWatchObserver) -> None:
            assert obs.handler is not None
            py_file.write_text("def run() -> int:\n    return 42\n", encoding="utf-8")
            obs.handler.on_created(FileCreatedEvent(str(py_file)))
            reindexed_gate.wait(timeout=2.0)
            py_file.unlink()
            obs.handler.on_deleted(FileDeletedEvent(str(py_file)))
            deleted_gate.wait(timeout=2.0)
            stop_event.set()

        watcher = RepoWatcher(
            cfg,
            observer_factory=lambda: cast(BaseObserver, _FakeWatchObserver(drive_events)),
        )
        try:
            watcher.watch(
                factory,
                repo_root,
                debounce_ms=20,
                verbose=False,
                on_event=on_event,
                stop_event=stop_event,
            )
            captured = capsys.readouterr()
            assert captured.out == ""

            assert isinstance(emitted[0], WatchStartedEvent)
            assert emitted[0].db_path == str(custom_db)
            assert isinstance(emitted[-1], WatchStoppedEvent)

            processed = [ev for ev in emitted if isinstance(ev, WatchFileProcessedEvent)]
            assert [(ev.file_path, ev.status) for ev in processed] == [
                ("app.py", IncrementalStatus.REINDEXED),
                ("app.py", IncrementalStatus.DELETED),
            ]
            assert processed[0].node_count >= 2
            assert processed[1].deleted_node_count >= 2

            repo_id = derive_repo_id(repo_root)
            with factory.open(read_only=True) as uow:
                assert uow.nodes.list_for_repo(repo_id) == []
        finally:
            factory.close()
