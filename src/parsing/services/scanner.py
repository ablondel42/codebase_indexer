"""Repository directory and single-file scanner service."""

import os
from datetime import UTC, datetime
from pathlib import Path

from core.models.config import AppConfig
from core.models.enums import Language
from core.models.exceptions import ParseError, PathSecurityError
from core.models.file import FileRecord
from core.models.repo import RepoRecord
from core.utils.hashing import sha256_bytes
from core.utils.logging import get_logger
from core.utils.pathing import (
    derive_file_id,
    derive_module_path,
    derive_repo_id,
    ensure_safe_repo_file,
    normalize_repo_root,
    to_file_uri,
    to_relative_path,
)
from core.utils.timestamps import utc_now_iso
from parsing.models.scan import ScannedFile, ScanOptions, ScanReport
from parsing.utils.filters import is_supported_source_file, is_test_path, should_ignore_dir

logger = get_logger("parsing.services.scanner")


class RepoScanner:
    """Safely scans a repository root or individual file into typed records."""

    def __init__(self, config: AppConfig | None = None) -> None:
        """Initialize the repository scanner with optional application configuration."""
        resolved_config = config if config is not None else AppConfig()
        self._config = resolved_config
        self._options = ScanOptions.from_config(resolved_config)

    @property
    def options(self) -> ScanOptions:
        """Return the immutable ``ScanOptions`` active on this scanner."""
        return self._options

    @staticmethod
    def _read_bounded_bytes(safe_path: Path, max_bytes: int) -> tuple[bytes, os.stat_result]:
        """Stat and read ``safe_path`` in one pass while enforcing ``max_bytes``."""
        stat_res = safe_path.stat()
        if stat_res.st_size > max_bytes:
            logger.warning(
                "Skipping oversized file %s (%d bytes > %d max_file_size_bytes)",
                safe_path,
                stat_res.st_size,
                max_bytes,
            )
            raise ParseError(
                f"File exceeds max_file_size_bytes ({max_bytes}): {safe_path}",
                context={"file_path": str(safe_path), "size_bytes": stat_res.st_size},
            )
        raw_bytes = safe_path.read_bytes()
        if len(raw_bytes) > max_bytes:
            raise ParseError(
                f"File exceeds max_file_size_bytes ({max_bytes}): {safe_path}",
                context={"file_path": str(safe_path), "size_bytes": len(raw_bytes)},
            )
        return raw_bytes, stat_res

    @staticmethod
    def _decode_and_count_lines(safe_path: Path, raw_bytes: bytes) -> tuple[str | None, int]:
        """Decode UTF-8 source text if valid and count lines matching ``splitlines()``."""
        try:
            source_text = raw_bytes.decode("utf-8")
            return source_text, len(source_text.splitlines())
        except UnicodeDecodeError as exc:
            logger.debug("Non-UTF-8 Python candidate %s during scan: %s", safe_path, exc)
            replaced = raw_bytes.decode("utf-8", errors="replace")
            return None, len(replaced.splitlines())

    def build_repo_record(self, repo_root: Path | str) -> RepoRecord:
        """Build a canonical ``RepoRecord`` from a repository root path."""
        resolved = normalize_repo_root(repo_root)
        now = utc_now_iso()
        return RepoRecord(
            id=derive_repo_id(resolved),
            root_path=str(resolved),
            name=resolved.name,
            default_language=Language.PYTHON,
            created_at=now,
            last_indexed_at=now,
        )

    def scan_file_with_source(
        self,
        repo_id: str,
        repo_root: Path | str,
        file_path: Path | str,
        *,
        safe_path: Path | None = None,
    ) -> ScannedFile:
        """Read ``file_path`` once and return its ``FileRecord`` plus decoded ``source_text``."""
        root_path = Path(repo_root)
        verified_path = (
            safe_path if safe_path is not None else ensure_safe_repo_file(root_path, file_path)
        )
        raw_bytes, stat_res = self._read_bounded_bytes(
            verified_path,
            self._options.max_file_size_bytes,
        )
        source_text, line_count = self._decode_and_count_lines(verified_path, raw_bytes)
        rel_path = to_relative_path(root_path, verified_path)
        record = FileRecord(
            id=derive_file_id(repo_id, rel_path),
            repo_id=repo_id,
            file_path=rel_path,
            uri=to_file_uri(verified_path),
            module_path=derive_module_path(root_path, verified_path),
            language=Language.PYTHON.value,
            content_hash=sha256_bytes(raw_bytes),
            size_bytes=len(raw_bytes),
            line_count=line_count,
            is_test=is_test_path(rel_path, self._options.test_dir_names),
            last_modified_at=datetime.fromtimestamp(stat_res.st_mtime, tz=UTC).isoformat(),
            last_indexed_at=utc_now_iso(),
        )
        return ScannedFile(
            record=record,
            source_text=source_text,
            mtime_ns=stat_res.st_mtime_ns,
        )

    def scan_file(
        self,
        repo_id: str,
        repo_root: Path | str,
        file_path: Path | str,
    ) -> FileRecord:
        """Validate and build a ``FileRecord`` for a single file inside ``repo_root``."""
        return self.scan_file_with_source(repo_id, repo_root, file_path).record

    def _is_allowed_subdir(self, current_dir: Path, dirname: str) -> bool:
        """Check whether ``dirname`` under ``current_dir`` should be traversed."""
        if (current_dir / dirname).is_symlink():
            logger.debug("Skipping symlinked directory during scan: %s", current_dir / dirname)
            return False
        if should_ignore_dir(dirname, self._options.ignored_dirs):
            return False
        return self._options.include_tests or not should_ignore_dir(
            dirname, self._options.test_dir_names
        )

    def _scan_candidate(
        self,
        repo_id: str,
        root: Path,
        candidate: Path,
    ) -> ScannedFile | None:
        """Scan one candidate path, logging a warning and returning ``None`` when skipped."""
        if not is_supported_source_file(candidate, self._options.supported_extensions):
            return None
        try:
            return self.scan_file_with_source(repo_id, root, candidate)
        except (PathSecurityError, ParseError, OSError) as exc:
            logger.warning("Skipping file %s during scan of %s: %s", candidate, repo_id, exc)
            return None

    def scan_repository(self, repo_root: Path | str) -> ScanReport:
        """Walk ``repo_root`` safely and return a ``ScanReport`` sorted by ``file_path``."""
        root = normalize_repo_root(repo_root)
        repo = self.build_repo_record(root)
        files: list[FileRecord] = []
        source_by_file_id: dict[str, str] = {}

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current_dir = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if self._is_allowed_subdir(current_dir, d))
            for filename in sorted(filenames):
                scanned = self._scan_candidate(repo.id, root, current_dir / filename)
                if scanned is None:
                    continue
                files.append(scanned.record)
                if scanned.source_text is not None:
                    source_by_file_id[scanned.record.id] = scanned.source_text

        files.sort(key=lambda item: item.file_path)
        logger.debug("Scanned repository %s (%d files matched)", repo.id, len(files))
        return ScanReport(
            repo=repo,
            files=files,
            source_by_file_id=source_by_file_id,
        )


__all__ = [
    "RepoScanner",
]
