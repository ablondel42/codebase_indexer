"""Pure stateless UTF-8 text corpus discovery, language classification, and reading utilities."""

import fnmatch
import os
import stat
from pathlib import Path
from types import MappingProxyType

from core.models.config import IGNORED_DIRS
from core.models.exceptions import PathSecurityError
from core.models.search import TextDocumentRecord, TextFileStat
from core.utils.hashing import sha256_bytes
from core.utils.logging import get_logger
from core.utils.pathing import (
    derive_file_id,
    ensure_safe_repo_file,
    normalize_repo_root,
    to_relative_path,
)

logger = get_logger("core.utils.text_files")

_IGNORED_EXACT_FILENAMES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db"})

_IGNORED_FILE_SUFFIXES: tuple[str, ...] = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".sqlite",
    ".sqlite3",
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".o",
    ".a",
    ".whl",
    ".tar",
    ".gz",
    ".zip",
    ".bz2",
    ".xz",
    ".7z",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".mp3",
    ".mp4",
    ".mov",
    ".tmp",
    ".swp",
    ".swo",
    ".bak",
)

_EXT_LANGUAGE_MAP: MappingProxyType[str, str] = MappingProxyType({
    ".py": "python",
    ".pyi": "python",
    ".md": "markdown",
    ".mdx": "markdown",
    ".toml": "toml",
    ".json": "json",
    ".jsonc": "json",
    ".jsonl": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".xml": "xml",
    ".svg": "xml",
    ".ini": "ini",
    ".cfg": "ini",
})

_BINARY_SNIFF_BYTES = 8192


def is_ignored_text_dir(name: str, ignored_dirs: tuple[str, ...] = IGNORED_DIRS) -> bool:
    """Return ``True`` when directory ``name`` matches ``ignored_dirs`` globs."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in ignored_dirs)


def is_ignored_text_filename(name: str) -> bool:
    """Return ``True`` for binary/SQLite/swap/OS-metadata filenames."""
    if name in _IGNORED_EXACT_FILENAMES or name.endswith("~") or name.startswith(".#"):
        return True
    lower = name.lower()
    return lower.endswith(_IGNORED_FILE_SUFFIXES)


def is_ignored_text_rel_path(
    rel_path: str,
    ignored_dirs: tuple[str, ...] = IGNORED_DIRS,
) -> bool:
    """Return ``True`` when any directory segment or filename in ``rel_path`` is ignored."""
    parts = [segment for segment in rel_path.replace("\\", "/").split("/") if segment]
    if not parts:
        return True
    if any(is_ignored_text_dir(part, ignored_dirs) for part in parts[:-1]):
        return True
    return is_ignored_text_filename(parts[-1])


def is_test_rel_path(
    rel_path: str,
    test_dir_names: tuple[str, ...] = ("tests", "test"),
) -> bool:
    """Return ``True`` when ``rel_path`` resides under a test directory."""
    parts = [segment for segment in rel_path.replace("\\", "/").split("/") if segment]
    return any(is_ignored_text_dir(part, test_dir_names) for part in parts[:-1])


def classify_text_language(rel_path: str, first_line: str = "") -> str:
    """Infer canonical language tag from file suffix, basename, or shebang line."""
    name = Path(rel_path).name
    suffix = Path(name).suffix.lower()
    mapped = _EXT_LANGUAGE_MAP.get(suffix)
    if mapped is not None:
        return mapped
    if name in ("Dockerfile", "Makefile"):
        return name.lower()
    if first_line.startswith("#!"):
        lower_shebang = first_line.lower()
        if "python" in lower_shebang:
            return "python"
        if any(sh in lower_shebang for sh in ("bash", "zsh", "/sh", " sh")):
            return "shell"
    return "text"


def _stat_candidate_file(
    root: Path,
    candidate: Path,
    *,
    max_file_size_bytes: int,
    test_dir_names: tuple[str, ...],
) -> TextFileStat | None:
    """Stat a single candidate path without following symlinks."""
    if is_ignored_text_filename(candidate.name):
        return None
    try:
        st = candidate.lstat()
    except OSError as exc:
        logger.debug("Skipping unreadable candidate %s: %s", candidate, exc)
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size > max_file_size_bytes:
        return None
    rel_path = to_relative_path(root, candidate)
    return TextFileStat(
        file_path=rel_path,
        absolute_path=candidate,
        size_bytes=st.st_size,
        mtime_ns=st.st_mtime_ns,
        is_test=is_test_rel_path(rel_path, test_dir_names),
    )


def walk_repo_text_stats(
    repo_root: Path | str,
    *,
    max_file_size_bytes: int = 2 * 1024 * 1024,
    ignored_dirs: tuple[str, ...] = IGNORED_DIRS,
    test_dir_names: tuple[str, ...] = ("tests", "test"),
) -> list[TextFileStat]:
    """Walk ``repo_root`` safely and return sorted ``TextFileStat`` items (~5ms)."""
    root = normalize_repo_root(repo_root)
    collected: list[TextFileStat] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_dir = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not is_ignored_text_dir(d, ignored_dirs) and not (current_dir / d).is_symlink()
        )
        for filename in sorted(filenames):
            stat_entry = _stat_candidate_file(
                root,
                current_dir / filename,
                max_file_size_bytes=max_file_size_bytes,
                test_dir_names=test_dir_names,
            )
            if stat_entry is not None:
                collected.append(stat_entry)

    collected.sort(key=lambda item: item.file_path)
    return collected


def _decode_utf8_non_binary(
    safe_path: Path,
    max_file_size_bytes: int,
) -> tuple[bytes, str] | None:
    """Read ``safe_path`` bytes and return ``(raw_bytes, utf8_text)`` when non-binary UTF-8."""
    try:
        raw_bytes = safe_path.read_bytes()
    except OSError as exc:
        logger.debug("Unable to read bytes from %s: %s", safe_path, exc)
        return None
    if len(raw_bytes) > max_file_size_bytes or b"\x00" in raw_bytes[:_BINARY_SNIFF_BYTES]:
        return None
    try:
        return raw_bytes, raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        logger.debug("Skipping non-UTF-8 file %s: %s", safe_path, exc)
        return None


def build_text_document_from_source(
    file_id: str,
    repo_id: str,
    rel_path: str,
    text: str,
    *,
    meta: tuple[str, int, int, bool],
) -> TextDocumentRecord:
    """Construct a ``TextDocumentRecord`` from already-loaded UTF-8 source text.

    Args:
        file_id: Deterministic ``file:<repo_id>:<rel_path>`` identifier.
        repo_id: Owning repository identifier.
        rel_path: Repository-relative POSIX path string.
        text: Decoded UTF-8 text content.
        meta: Tuple ``(content_hash, size_bytes, mtime_ns, is_test)``.

    Returns:
        Populated ``TextDocumentRecord``.
    """
    content_hash, size_bytes, mtime_ns, is_test = meta
    lines = text.splitlines()
    first_line = lines[0] if lines else ""
    return TextDocumentRecord(
        id=file_id,
        repo_id=repo_id,
        file_path=rel_path,
        language=classify_text_language(rel_path, first_line),
        content_hash=content_hash,
        size_bytes=size_bytes,
        line_count=len(lines),
        mtime_ns=mtime_ns,
        is_test=is_test,
        content=text,
    )


def read_text_document(
    repo_id: str,
    repo_root: Path | str,
    file_path: Path | str,
    *,
    max_file_size_bytes: int = 2 * 1024 * 1024,
    ignored_dirs: tuple[str, ...] = IGNORED_DIRS,
    test_dir_names: tuple[str, ...] = ("tests", "test"),
) -> TextDocumentRecord | None:
    """Confine, binary-check, UTF-8 decode, and hash a single repository text file."""
    root = normalize_repo_root(repo_root)
    try:
        safe_path = ensure_safe_repo_file(root, file_path)
        st = safe_path.stat()
    except (PathSecurityError, OSError) as exc:
        logger.debug("Skipping unsafe or missing text file %s: %s", file_path, exc)
        return None

    rel_path = to_relative_path(root, safe_path)
    if st.st_size > max_file_size_bytes or is_ignored_text_rel_path(rel_path, ignored_dirs):
        return None

    decoded = _decode_utf8_non_binary(safe_path, max_file_size_bytes)
    if decoded is None:
        return None
    raw_bytes, text = decoded

    return build_text_document_from_source(
        derive_file_id(repo_id, rel_path),
        repo_id,
        rel_path,
        text,
        meta=(
            sha256_bytes(raw_bytes),
            len(raw_bytes),
            st.st_mtime_ns,
            is_test_rel_path(rel_path, test_dir_names),
        ),
    )


__all__ = [
    "build_text_document_from_source",
    "classify_text_language",
    "is_ignored_text_dir",
    "is_ignored_text_filename",
    "is_ignored_text_rel_path",
    "is_test_rel_path",
    "read_text_document",
    "walk_repo_text_stats",
]
