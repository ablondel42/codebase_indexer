"""PostToolUse incremental single-file SQLite sync into repo_context.db."""

import json
import sys
from pathlib import Path
from typing import Any

from core.models.config import AppConfig
from core.models.exceptions import IndexerError
from engine.services.facade import IndexerEngine
from indexing.models.events import FileChangeEvent, FileEventType

REPO_ROOT = Path(__file__).resolve().parents[4]


def _parse_payload() -> dict[str, Any] | None:
    """Read and decode PostToolUse JSON payload from ``stdin``."""
    try:
        loaded = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"post_write_sync: invalid JSON stdin: {exc}\n")
        return None
    if not isinstance(loaded, dict) or loaded.get("error"):
        return None
    return loaded


def _resolve_target_path(payload: dict[str, Any], raw_path: str) -> tuple[Path, str] | None:
    """Resolve ``raw_path`` inside ``REPO_ROOT`` if it is a tracked Python file."""
    path_obj = Path(raw_path)
    if not path_obj.is_absolute():
        base_dir = payload.get("cwd")
        base_path = Path(base_dir) if isinstance(base_dir, str) and base_dir else REPO_ROOT
        path_obj = base_path / path_obj

    try:
        resolved = path_obj.resolve()
        repo_rel = resolved.relative_to(REPO_ROOT).as_posix()
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"post_write_sync: skipping non-repo path {raw_path!r}: {exc}\n")
        return None

    if resolved.suffix != ".py" or not resolved.is_file():
        return None
    in_src = repo_rel.startswith("src/")
    in_tests = repo_rel.startswith("tests/") and not repo_rel.startswith("tests/fixtures/")
    return (resolved, repo_rel) if (in_src or in_tests) else None


def _extract_repo_python_path(payload: dict[str, Any]) -> tuple[Path, str] | None:
    """Return ``(resolved_path, repo_relative_posix)`` if target is an indexed repo ``.py``."""
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return None
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return None
    raw_path = args.get("Path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    return _resolve_target_path(payload, raw_path)


def _sync_file(db_path: Path, resolved: Path, repo_rel: str) -> None:
    """Incrementally reindex ``resolved`` inside ``repo_context.db``."""
    event = FileChangeEvent(
        event_type=FileEventType.MODIFIED,
        absolute_path=str(resolved),
        repo_relative_path=repo_rel,
    )
    with IndexerEngine(AppConfig(db_path=db_path, include_tests=True)) as engine:
        engine.commands.process_watch_events(REPO_ROOT, [event])


def main() -> int:
    """Incrementally reindex the modified repository Python file and emit ``{}``."""
    try:
        payload = _parse_payload()
        db_path = REPO_ROOT / "repo_context.db"
        target_info = (
            _extract_repo_python_path(payload)
            if payload is not None and db_path.is_file()
            else None
        )
        if target_info is not None:
            _sync_file(db_path, target_info[0], target_info[1])
    except (IndexerError, OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"post_write_sync: incremental sync skipped: {exc}\n")

    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
