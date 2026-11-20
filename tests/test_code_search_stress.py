"""Adversarial differential-oracle, dirty-disk chaos, pagination, and MCP/CLI stress tests."""

import asyncio
import json
from pathlib import Path
from typing import NamedTuple

import pytest

from cli.services.app import CliApplication
from context.utils.trigram import compile_search_pattern, match_document_lines
from core.models.config import AppConfig
from core.models.search import CodeSearchQuery
from core.utils.text_files import read_text_document, walk_repo_text_stats
from engine.services.facade import IndexerEngine
from indexing.models.events import FileChangeEvent, FileEventType
from indexing.models.results import BatchIndexOptions
from mcp_server.services.registry import McpToolRegistry
from storage.services.uow import SqlUnitOfWorkFactory

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class OracleCase(NamedTuple):
    """Single differential-oracle test vector."""

    query: str
    regex: bool = False
    case_sensitive: bool = False
    path_prefix: str | None = None
    language: str | None = None
    include_tests: bool = True


ORACLE_CASES: tuple[OracleCase, ...] = (
    OracleCase("codebase_indexer.db"),
    OracleCase("cbi> "),
    OracleCase('extra="forbid"'),
    OracleCase("%(prog)s"),
    OracleCase("# noqa: F401"),
    OracleCase("TextSearchRepositoryProtocol"),
    OracleCase("SQLITE_PRAGMA"),
    OracleCase("UnitOfWorkProtocol", case_sensitive=True),
    OracleCase("unitofworkprotocol", case_sensitive=True),
    OracleCase("unitofworkprotocol", case_sensitive=False),
    OracleCase("rc"),
    OracleCase("id", path_prefix="src/core"),
    OracleCase("->", path_prefix="src/mcp_server"),
    OracleCase("==", path_prefix="src/context"),
    OracleCase(r"\bcbi\b", regex=True),
    OracleCase(r"codebase[-_]indexer", regex=True),
    OracleCase(r"Generator\[.*, None, None\]", regex=True),
    OracleCase(r"FrozenModel|MutableModel", regex=True),
    OracleCase(r"^class \w+\(Protocol\):$", regex=True),
    OracleCase(r"(?i)schema_version", regex=True),
    OracleCase(r"\x53CHEMA_VERSION", regex=True, case_sensitive=True),
    OracleCase(r"def _[a-z]+_search", regex=True),
    OracleCase('"unclosed'),
    OracleCase("AND OR NOT"),
    OracleCase("NEAR/3"),
    OracleCase("repo_id: str"),
    OracleCase("a*b^c"),
    OracleCase("100%"),
    OracleCase("build-backend", language="toml"),
    OracleCase("hexagonal-boundaries", path_prefix=".agents"),
    OracleCase("#!/usr/bin/env bash"),
    OracleCase("*.egg-info/"),
)


def _clean_oracle_prefix(prefix: str | None) -> str:
    """Normalize ``path_prefix`` identically to ``CodeSearchService``."""
    if not prefix:
        return ""
    cleaned = prefix.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned.removeprefix("./")
    return cleaned.rstrip("/")


def _run_disk_oracle(
    root: Path,
    case: OracleCase,
    config: AppConfig,
) -> tuple[list[tuple[str, int, int, str]], dict[str, int]]:
    """Scan ``root`` directly on disk with Python ``re`` as ground-truth reference oracle."""
    pattern = compile_search_pattern(
        case.query,
        regex=case.regex,
        case_sensitive=case.case_sensitive,
    )
    prefix = _clean_oracle_prefix(case.path_prefix)
    stats = walk_repo_text_stats(
        root,
        max_file_size_bytes=config.max_file_size_bytes,
        test_dir_names=config.test_dir_names,
    )
    hits: list[tuple[str, int, int, str]] = []
    counts: dict[str, int] = {}
    for st in stats:
        if not case.include_tests and st.is_test:
            continue
        if prefix and not st.file_path.startswith(prefix):
            continue
        doc = read_text_document(
            "repo:oracle",
            root,
            st.file_path,
            max_file_size_bytes=config.max_file_size_bytes,
            test_dir_names=config.test_dir_names,
        )
        if doc is None:
            continue
        if case.language and doc.language != case.language.strip().lower():
            continue
        doc_matches = match_document_lines(doc, pattern, context_lines=0)
        if doc_matches:
            counts[doc.file_path] = len(doc_matches)
            hits.extend((m.file_path, m.line, m.column, m.preview) for m in doc_matches)
    return hits, counts


def _collect_all_paginated_hits(
    engine: IndexerEngine,
    case: OracleCase,
    page_size: int = 200,
) -> tuple[list[tuple[str, int, int, str]], dict[str, int]]:
    """Fetch all pages from ``engine.queries.search_code`` and return hits + file counts."""
    collected: list[tuple[str, int, int, str]] = []
    file_counts: dict[str, int] = {}
    offset = 0
    while True:
        report = engine.queries.search_code(
            CodeSearchQuery(
                query=case.query,
                regex=case.regex,
                case_sensitive=case.case_sensitive,
                path_prefix=case.path_prefix,
                language=case.language,
                include_tests=case.include_tests,
                verify_disk=True,
                limit=page_size,
                offset=offset,
            )
        )
        if offset == 0:
            file_counts = {fc.file_path: fc.count for fc in report.file_counts}
        collected.extend((m.file_path, m.line, m.column, m.preview) for m in report.matches)
        if not report.has_more:
            break
        offset += page_size
    return collected, file_counts


@pytest.fixture(scope="module")
def live_repo_engine(tmp_path_factory: pytest.TempPathFactory) -> tuple[IndexerEngine, Path]:
    """Index ``PROJECT_ROOT`` (including tests, skipping LSP) once for oracle parity checks."""
    db_dir = tmp_path_factory.mktemp("live_oracle")
    db_path = db_dir / "live_oracle.db"
    engine = IndexerEngine(AppConfig(db_path=db_path))
    result = engine.commands.index_repository(
        PROJECT_ROOT,
        BatchIndexOptions(include_tests=True, enrich_lsp=False),
    )
    assert result.files_indexed > 50
    return engine, db_path


def test_live_repo_fts_integrity_and_32_oracle_queries(
    live_repo_engine: tuple[IndexerEngine, Path],
) -> None:
    """Every one of 32 literal/regex/operator/short queries has 100.0% parity with disk ``re``."""
    engine, db_path = live_repo_engine
    uow_factory = SqlUnitOfWorkFactory(db_path)
    with uow_factory.open(read_only=False, must_exist=True, initialize=False) as uow:
        assert uow.text_search.check_fts_integrity() is True

    for case in ORACLE_CASES:
        expected_hits, expected_counts = _run_disk_oracle(PROJECT_ROOT, case, engine.config)
        actual_hits, actual_counts = _collect_all_paginated_hits(engine, case)
        assert actual_counts == expected_counts, f"file_counts mismatch for {case}"
        assert actual_hits == expected_hits, f"line hits mismatch for {case}"


def _seed_chaos_repo(repo_root: Path, outside_dir: Path) -> None:
    """Create a polyglot repository with Unicode, hidden dirs, binary, and external symlink."""
    (repo_root / "pkg").mkdir(parents=True)
    (repo_root / ".agents" / "rules").mkdir(parents=True)
    (repo_root / "assets").mkdir(parents=True)
    (repo_root / "tests").mkdir(parents=True)

    (repo_root / "pkg" / "calc.py").write_text(
        '"""Math module."""\n\n'
        'MODULE_TAG = "ALPHA_V1_MARKER"\n\n\n'
        "class MathBox:\n"
        '    """Calculator container."""\n\n'
        "    def compute_alpha(self, x: int) -> int:\n"
        '        """Compute alpha."""\n'
        "        tag = 'ALPHA_V1_MARKER'\n\n"
        "        def inner_step(v: int) -> int:\n"
        "            return v + len(tag)  # INNER_CLOSURE_TAG\n\n"
        "        return inner_step(x)\n",
        encoding="utf-8",
    )
    (repo_root / "README.md").write_text("# Café Été → 42\nPolyglot docs.\n", encoding="utf-8")
    (repo_root / "pyproject.toml").write_text('[project]\nname = "chaos-pkg"\n', encoding="utf-8")
    (repo_root / "config.json").write_text('{"key": "LEGACY_JSON_MARKER"}\n', encoding="utf-8")
    (repo_root / "cbi").write_text(
        "#!/usr/bin/env bash\necho CBI_SCRIPT_MARKER\n",
        encoding="utf-8",
    )
    (repo_root / ".agents" / "rules" / "guide.md").write_text(
        "Rule: HIDDEN_AGENT_RULE_MARKER\n",
        encoding="utf-8",
    )
    (repo_root / ".gitignore").write_text("*.pyc\n.venv/\n", encoding="utf-8")
    (repo_root / "tests" / "test_calc.py").write_text(
        "def test_ok() -> None:\n    assert 'TEST_ONLY_MARKER'\n",
        encoding="utf-8",
    )
    (repo_root / "assets" / "blob.bin").write_bytes(b"\x00\x01ELF_SECRET_TOKEN\n")

    secret_file = outside_dir / "outside_secret.txt"
    secret_file.write_text("OUTSIDE_LEAK_TOKEN\n", encoding="utf-8")
    (repo_root / "escape_link.txt").symlink_to(secret_file)


def test_dirty_disk_chaos_unicode_security_and_incremental_sync(tmp_path: Path) -> None:
    """Unindexed edits, new files, and deletions achieve 0% stale misses and 0% ghost hits."""
    repo_root = tmp_path / "chaos_repo"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    _seed_chaos_repo(repo_root, outside_dir)

    db_path = tmp_path / "chaos.db"
    engine = IndexerEngine(AppConfig(db_path=db_path, include_tests=True))
    engine.commands.index_repository(
        repo_root,
        BatchIndexOptions(include_tests=True, enrich_lsp=False),
    )

    # 1. Binary & external-symlink confinement + Unicode + .agents path filter
    assert engine.queries.search_code(CodeSearchQuery(query="ELF_SECRET_TOKEN")).total_matches == 0
    assert (
        engine.queries.search_code(CodeSearchQuery(query="OUTSIDE_LEAK_TOKEN")).total_matches == 0
    )
    unicode_rep = engine.queries.search_code(CodeSearchQuery(query="été"))
    assert unicode_rep.total_matches == 1
    assert unicode_rep.matches[0].file_path == "README.md"

    agent_rep = engine.queries.search_code(
        CodeSearchQuery(query="HIDDEN_AGENT_RULE_MARKER", path_prefix=".agents/rules")
    )
    assert agent_rep.total_matches == 1
    assert agent_rep.matches[0].file_path == ".agents/rules/guide.md"

    # 2. Mutate disk WITHOUT re-indexing: edit calc.py, add NEW.md, delete config.json
    calc_path = repo_root / "pkg" / "calc.py"
    calc_path.write_text(
        calc_path.read_text(encoding="utf-8").replace("ALPHA_V1_MARKER", "ALPHA_V2_LIVE_MARKER"),
        encoding="utf-8",
    )
    new_md = repo_root / "NEW.md"
    new_md.write_text("Fresh untracked note: ALPHA_V2_LIVE_MARKER\n", encoding="utf-8")
    json_path = repo_root / "config.json"
    json_path.unlink()

    assert engine.queries.search_code(CodeSearchQuery(query="ALPHA_V1_MARKER")).total_matches == 0
    gone_rep = engine.queries.search_code(CodeSearchQuery(query="LEGACY_JSON_MARKER"))
    assert gone_rep.total_matches == 0
    assert gone_rep.stats.deleted_overlay_files == 1

    live_rep = engine.queries.search_code(CodeSearchQuery(query="ALPHA_V2_LIVE_MARKER"))
    assert live_rep.total_matches == 3
    assert {fc.file_path for fc in live_rep.file_counts} == {"NEW.md", "pkg/calc.py"}
    assert live_rep.stats.live_overlay_files >= 2

    # 3. Incremental sync clears dirty overlay and preserves FTS5 integrity
    engine.commands.process_watch_events(
        repo_root,
        [
            FileChangeEvent(
                event_type=FileEventType.MODIFIED,
                absolute_path=str(calc_path),
                repo_relative_path="pkg/calc.py",
            ),
            FileChangeEvent(
                event_type=FileEventType.CREATED,
                absolute_path=str(new_md),
                repo_relative_path="NEW.md",
            ),
            FileChangeEvent(
                event_type=FileEventType.DELETED,
                absolute_path=str(json_path),
                repo_relative_path="config.json",
            ),
        ],
    )

    synced_rep = engine.queries.search_code(CodeSearchQuery(query="ALPHA_V2_LIVE_MARKER"))
    assert synced_rep.total_matches == 3
    assert synced_rep.stats.live_overlay_files == 0
    assert synced_rep.stats.deleted_overlay_files == 0
    assert synced_rep.stats.trigram_pruned is True

    with SqlUnitOfWorkFactory(db_path).open(
        read_only=False, must_exist=True, initialize=False
    ) as uow:
        assert uow.text_search.check_fts_integrity() is True


def test_pagination_completeness_file_counts_and_nested_symbol_attribution(
    tmp_path: Path,
) -> None:
    """``file_counts`` spans all 25 files on page 1, pagination is lossless, and symbols nest."""
    repo_root = tmp_path / "pag_repo"
    (repo_root / "pkg").mkdir(parents=True)

    for idx in range(25):
        body_lines = [
            f'"""Module {idx:02d}."""',
            "",
            f"class Service{idx:02d}:",
            "    def execute(self) -> int:",
            "        def nested_helper() -> int:",
            "            return 1  # PAGINATION_NEEDLE_TOKEN",
        ]
        body_lines.extend(
            f"        val_{line_i} = {line_i}  # PAGINATION_NEEDLE_TOKEN" for line_i in range(9)
        )
        body_lines.append("        return nested_helper()  # PAGINATION_NEEDLE_TOKEN")
        (repo_root / "pkg" / f"mod_{idx:02d}.py").write_text(
            "\n".join(body_lines) + "\n",
            encoding="utf-8",
        )

    db_path = tmp_path / "pag.db"
    engine = IndexerEngine(AppConfig(db_path=db_path, include_tests=True))
    engine.commands.index_repository(
        repo_root,
        BatchIndexOptions(include_tests=True, enrich_lsp=False),
    )

    first_page = engine.queries.search_code(
        CodeSearchQuery(query="PAGINATION_NEEDLE_TOKEN", limit=15, offset=0, context_lines=1)
    )
    assert first_page.total_matches == 275
    assert first_page.matched_file_count == 25
    assert len(first_page.file_counts) == 25
    assert sum(fc.count for fc in first_page.file_counts) == 275
    assert first_page.returned_count == 15
    assert first_page.has_more is True

    # First match in mod_00.py is inside nested_helper (innermost symbol wins over execute/class!)
    nested_hit = first_page.matches[0]
    assert nested_hit.file_path == "pkg/mod_00.py"
    assert nested_hit.qualified_name == "pkg.mod_00.Service00.execute.nested_helper"
    assert nested_hit.symbol_kind == "local_function"
    assert len(nested_hit.context_before) == 1
    assert len(nested_hit.context_after) == 1

    # Second match in mod_00.py is directly inside Service00.execute
    method_hit = first_page.matches[1]
    assert method_hit.qualified_name == "pkg.mod_00.Service00.execute"
    assert method_hit.symbol_kind == "method"

    # Reconstruct all 275 matches via pagination with zero gaps or duplicates
    all_coords: list[tuple[str, int]] = []
    offset = 0
    while True:
        page = engine.queries.search_code(
            CodeSearchQuery(query="PAGINATION_NEEDLE_TOKEN", limit=40, offset=offset)
        )
        all_coords.extend((m.file_path, m.line) for m in page.matches)
        if not page.has_more:
            break
        offset += 40
    assert len(all_coords) == 275
    assert len(set(all_coords)) == 275


def test_mcp_and_cli_search_code_surface(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """MCP ``search_code``, ``cbi search``, and ``cbi mcp-call search_code`` work end-to-end."""
    repo_root = tmp_path / "surface_repo"
    outside_dir = tmp_path / "outside_surface"
    outside_dir.mkdir()
    _seed_chaos_repo(repo_root, outside_dir)

    db_path = tmp_path / "surface.db"
    engine = IndexerEngine(AppConfig(db_path=db_path, include_tests=True))
    engine.commands.index_repository(
        repo_root,
        BatchIndexOptions(include_tests=True, enrich_lsp=False),
    )
    registry = McpToolRegistry(engine)

    raw_ok = asyncio.run(
        registry.call_tool(
            "search_code",
            {"query": "INNER_CLOSURE_TAG", "context_lines": 1},
        )
    )
    payload = json.loads(raw_ok)
    assert payload["ok"] is True
    res = payload["data"]
    assert res["total_matches"] == 1
    assert res["matches"][0]["qualified_name"] == "pkg.calc.MathBox.compute_alpha.inner_step"
    assert "context_before" in res["matches"][0]

    raw_bad_regex = asyncio.run(
        registry.call_tool("search_code", {"query": "[unclosed", "regex": True})
    )
    err_payload = json.loads(raw_bad_regex)
    assert err_payload["ok"] is False
    assert err_payload["error"]["code"] == "invalid_input"

    app = CliApplication(engine=engine)
    capsys.readouterr()
    rc = app.run(
        [
            "--db-path",
            str(db_path),
            "search",
            "TEST_ONLY_MARKER",
            "--no-tests",
        ]
    )
    assert rc == 0
    no_test_out = json.loads(capsys.readouterr().out)
    assert no_test_out["total_matches"] == 0

    rc2 = app.run(
        [
            "--db-path",
            str(db_path),
            "mcp-call",
            "search_code",
            "CBI_SCRIPT_MARKER",
        ]
    )
    assert rc2 == 0
    mcp_call_out = json.loads(capsys.readouterr().out)
    assert mcp_call_out["result"]["ok"] is True
    assert mcp_call_out["result"]["data"]["matches"][0]["file_path"] == "cbi"
