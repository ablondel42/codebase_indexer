"""Safe repository file reading, live disk freshness verification, and line formatting."""

from collections.abc import Sequence
from pathlib import Path

from core.models.enums import FreshnessStatus
from core.models.exceptions import ParseError, PathSecurityError
from core.models.file import FileRecord, FreshnessCheckResult, VerifiedFileText
from core.utils.hashing import sha256_bytes, sha256_file
from core.utils.logging import get_logger
from core.utils.pathing import ensure_safe_repo_file

logger = get_logger("core.utils.file_io")


def _validate_freshness_inputs(
    repo_root: Path | str | None,
    file_record: FileRecord | None,
) -> tuple[Path | None, FreshnessCheckResult | None]:
    """Validate ``repo_root`` and ``file_record`` presence prior to disk hashing.

    Args:
        repo_root: Repository root directory path or ``None``.
        file_record: Indexed ``FileRecord`` or ``None``.

    Returns:
        Tuple of ``(resolved_root_dir, early_failure_result)``.
    """
    if file_record is None:
        return None, FreshnessCheckResult(
            is_fresh=False,
            stale_reason="file_record_missing",
            freshness=FreshnessStatus.MISSING,
        )
    if repo_root is None or (isinstance(repo_root, str) and not repo_root.strip()):
        return None, FreshnessCheckResult(
            is_fresh=False,
            stale_reason="repo_record_missing",
            freshness=FreshnessStatus.MISSING,
        )
    root_path = Path(repo_root)
    if not root_path.is_dir():
        return None, FreshnessCheckResult(
            is_fresh=False,
            stale_reason="repo_root_missing",
            freshness=FreshnessStatus.MISSING,
        )
    return root_path, None


def check_file_disk_freshness(
    repo_root: Path | str | None,
    file_record: FileRecord | None,
    *,
    max_file_size_bytes: int = 2 * 1024 * 1024,
) -> FreshnessCheckResult:
    """Verify an indexed ``FileRecord`` against disk confinement, readability, and SHA-256 hash.

    Preserves exact ``stale_reason`` identifiers: ``"file_record_missing"``,
    ``"repo_record_missing"``, ``"repo_root_missing"``, ``"file_missing"``,
    ``"path_security_violation"``, ``"file_unreadable"``, ``"content_hash_mismatch"``,
    or ``None`` when fresh.

    Args:
        repo_root: Repository root directory path or ``None``.
        file_record: Indexed ``FileRecord`` to verify or ``None``.
        max_file_size_bytes: Maximum permitted file size in bytes before marking unreadable.

    Returns:
        ``FreshnessCheckResult`` describing freshness boolean, reason, and enum status.
    """
    root_path, early_result = _validate_freshness_inputs(repo_root, file_record)
    if early_result is not None or root_path is None or file_record is None:
        return early_result or FreshnessCheckResult(
            is_fresh=False,
            stale_reason="file_record_missing",
            freshness=FreshnessStatus.MISSING,
        )

    try:
        safe_path = ensure_safe_repo_file(root_path, file_record.file_path)
    except PathSecurityError as exc:
        candidate = root_path / file_record.file_path
        is_missing = not candidate.exists() and not candidate.is_symlink()
        reason = "file_missing" if is_missing else "path_security_violation"
        status = FreshnessStatus.MISSING if is_missing else FreshnessStatus.SECURITY_VIOLATION
        logger.debug(
            "Disk freshness path check failed for %s under %s (reason=%s): %s",
            file_record.file_path,
            root_path,
            reason,
            exc,
        )
        return FreshnessCheckResult(is_fresh=False, stale_reason=reason, freshness=status)

    try:
        disk_hash = sha256_file(safe_path, max_file_size_bytes=max_file_size_bytes)
    except (OSError, ParseError) as exc:
        logger.debug(
            "Disk freshness hash check failed for %s (file_id=%s): %s",
            safe_path,
            file_record.id,
            exc,
        )
        return FreshnessCheckResult(
            is_fresh=False,
            stale_reason="file_unreadable",
            freshness=FreshnessStatus.UNREADABLE,
        )

    is_matching = disk_hash == file_record.content_hash
    return FreshnessCheckResult(
        is_fresh=is_matching,
        stale_reason=None if is_matching else "content_hash_mismatch",
        freshness=FreshnessStatus.FRESH if is_matching else FreshnessStatus.STALE,
    )


def _read_bounded_bytes(safe_path: Path, max_file_size_bytes: int) -> bytes:
    """Read raw bytes from ``safe_path`` enforcing ``max_file_size_bytes``.

    Args:
        safe_path: Verified in-repository regular file path.
        max_file_size_bytes: Maximum allowed file size in bytes.

    Returns:
        Raw file bytes.

    Raises:
        ParseError: If reading fails or the read payload exceeds ``max_file_size_bytes``.
    """
    try:
        stat_size = safe_path.stat().st_size
        if stat_size > max_file_size_bytes:
            raise ParseError(
                f"File exceeds max_file_size_bytes ({max_file_size_bytes}): {safe_path}",
                context={"file_path": str(safe_path), "size_bytes": stat_size},
            )
        raw_bytes = safe_path.read_bytes()
    except OSError as exc:
        raise ParseError(
            f"Unable to read file {safe_path}: {exc}",
            context={"file_path": str(safe_path)},
        ) from exc

    if len(raw_bytes) > max_file_size_bytes:
        raise ParseError(
            f"File exceeds max_file_size_bytes ({max_file_size_bytes}): {safe_path}",
            context={"file_path": str(safe_path), "size_bytes": len(raw_bytes)},
        )
    return raw_bytes


def read_verified_file_text(
    repo_root: Path | str,
    rel_file_path: str,
    *,
    expected_content_hash: str | None = None,
    max_file_size_bytes: int = 2 * 1024 * 1024,
) -> VerifiedFileText:
    """Validate confinement, verify size/hash, and read UTF-8 text from a repository file.

    Computes ``content_hash`` via ``sha256_file`` on raw disk bytes so CRLF and LF
    files produce digests identical to scanner hashes, while decoding text with
    ``errors="replace"``.

    Args:
        repo_root: Repository root directory.
        rel_file_path: Candidate file path relative to ``repo_root`` (or confined absolute).
        expected_content_hash: Optional indexed ``sha256:...`` digest to compare against.
        max_file_size_bytes: Maximum allowed file size in bytes.

    Returns:
        ``VerifiedFileText`` containing decoded text, split lines, digest, and freshness.

    Raises:
        PathSecurityError: If ``rel_file_path`` violates confinement or symlink rules.
        ParseError: If the file cannot be read or exceeds ``max_file_size_bytes``.
    """
    safe_path = ensure_safe_repo_file(Path(repo_root), rel_file_path)
    raw_bytes = _read_bounded_bytes(safe_path, max_file_size_bytes)
    content_hash = sha256_bytes(raw_bytes)
    text = raw_bytes.decode("utf-8", errors="replace")
    is_fresh = True if expected_content_hash is None else (content_hash == expected_content_hash)

    return VerifiedFileText(
        path=safe_path,
        text=text,
        lines=text.splitlines(),
        content_hash=content_hash,
        is_fresh=is_fresh,
        stale_reason=None if is_fresh else "content_hash_mismatch",
    )


def extract_line_preview(
    lines: Sequence[str],
    line_1_based: int,
    *,
    max_chars: int = 160,
) -> str | None:
    """Extract a trimmed single-line source preview at a 1-based line number.

    Args:
        lines: Sequence of source lines for a file.
        line_1_based: One-based target line number.
        max_chars: Maximum preview length before truncating with ``"..."``.

    Returns:
        Trimmed preview string, or ``None`` if out of bounds or blank.
    """
    if line_1_based < 1 or line_1_based > len(lines):
        return None
    trimmed = lines[line_1_based - 1].strip()
    if not trimmed:
        return None
    if len(trimmed) <= max_chars:
        return trimmed
    if max_chars <= 3:
        return trimmed[:max_chars]
    return f"{trimmed[: max_chars - 3]}..."


def format_line_span(start_line: int | None, end_line: int | None) -> str | None:
    """Format a 1-based ``"start-end"`` line span string when both bounds are present.

    Args:
        start_line: One-based inclusive start line or ``None``.
        end_line: One-based inclusive end line or ``None``.

    Returns:
        Formatted ``"{start_line}-{end_line}"`` string, or ``None`` if either is ``None``.
    """
    if start_line is not None and end_line is not None:
        return f"{start_line}-{end_line}"
    return None
