"""Repository path confinement, normalization, URI conversion, and ID derivation."""

import hashlib
import stat
from pathlib import Path

from core.models.enums import PREFIX_FILE, PREFIX_REPO
from core.models.exceptions import PathSecurityError
from core.utils.logging import get_logger

logger = get_logger("core.utils.pathing")


def normalize_repo_root(path: str | Path) -> Path:
    """Normalize and validate an existing repository root directory path.

    Args:
        path: Candidate path to the repository root directory.

    Returns:
        Resolved absolute ``Path`` to the repository root.

    Raises:
        FileNotFoundError: If ``path`` does not exist on disk.
        NotADirectoryError: If ``path`` exists but is not a directory.
    """
    resolved = Path(path).resolve()

    if not resolved.exists():
        raise FileNotFoundError(f"Repository path does not exist: {resolved}")

    if not resolved.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {resolved}")

    return resolved


def _find_base_for_parts(
    raw_root: Path,
    resolved_root: Path,
    candidate_path: Path,
) -> Path | None:
    """Find the root prefix matching ``candidate_path`` for intermediate symlink checks.

    Args:
        raw_root: Unresolved ``repo_root`` path as supplied by the caller.
        resolved_root: Resolved ``repo_root`` path.
        candidate_path: Absolute candidate path being inspected.

    Returns:
        Matching ancestor ``Path`` representing the repository root, or ``None``.
    """
    if candidate_path.is_relative_to(resolved_root):
        return resolved_root
    if candidate_path.is_relative_to(raw_root):
        return raw_root
    for parent in reversed(candidate_path.parents):
        try:
            if parent.resolve() == resolved_root:
                return parent
        except OSError as exc:
            logger.debug(
                "Skipping unresolvable ancestor %s while matching repo root %s: %s",
                parent,
                resolved_root,
                exc,
            )
            continue
    return None


def _reject_intermediate_symlinks(
    base_for_parts: Path,
    candidate_path: Path,
    resolved_root: Path,
) -> None:
    """Reject any intermediate symlink component below ``base_for_parts``.

    Args:
        base_for_parts: Matched repository root prefix for ``candidate_path``.
        candidate_path: Absolute candidate file path.
        resolved_root: Resolved repository root path for error context.

    Raises:
        PathSecurityError: If any component below ``base_for_parts`` is a symlink.
    """
    current = base_for_parts
    for part in candidate_path.relative_to(base_for_parts).parts:
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            raise PathSecurityError(
                f"Symlink path component rejected: {current}",
                context={"repo_root": str(resolved_root), "candidate": str(candidate_path)},
            )


def _resolve_and_verify_regular_file(candidate_path: Path, resolved_root: Path) -> Path:
    """Resolve ``candidate_path`` and verify root containment and regular-file mode.

    Args:
        candidate_path: Candidate file path after symlink component checks.
        resolved_root: Resolved repository root directory.

    Returns:
        Resolved ``Path`` to the verified regular file.

    Raises:
        PathSecurityError: If resolution fails, escapes ``resolved_root``, or is not a regular file.
    """
    try:
        resolved_candidate = candidate_path.resolve()
    except OSError as exc:
        raise PathSecurityError(
            f"Cannot resolve candidate path: {candidate_path}",
            context={"repo_root": str(resolved_root), "candidate": str(candidate_path)},
        ) from exc

    if not resolved_candidate.is_relative_to(resolved_root):
        raise PathSecurityError(
            f"Path escapes repository root: {resolved_candidate}",
            context={"repo_root": str(resolved_root), "resolved": str(resolved_candidate)},
        )

    try:
        file_stat = resolved_candidate.stat()
    except OSError as exc:
        raise PathSecurityError(
            f"Repository file is not accessible: {resolved_candidate}",
            context={"repo_root": str(resolved_root), "resolved": str(resolved_candidate)},
        ) from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise PathSecurityError(
            f"Repository path is not a regular file: {resolved_candidate}",
            context={"repo_root": str(resolved_root), "resolved": str(resolved_candidate)},
        )

    return resolved_candidate


def ensure_safe_repo_file(repo_root: Path, candidate: Path | str) -> Path:
    """Validate that ``candidate`` is a regular non-symlink file confined to ``repo_root``.

    Rejects symlinks (both the leaf file and any intermediate directory below
    ``repo_root``), paths escaping ``repo_root.resolve()``, missing targets, and
    non-regular files.

    Args:
        repo_root: Repository root directory.
        candidate: Candidate file path (absolute or relative to ``repo_root``).

    Returns:
        Resolved absolute ``Path`` to the verified regular file.

    Raises:
        PathSecurityError: If any confinement, symlink, existence, or mode check fails.
    """
    raw_root = Path(repo_root)
    resolved_root = raw_root.resolve()
    candidate_path = Path(candidate)
    if not candidate_path.is_absolute():
        candidate_path = resolved_root / candidate_path

    if candidate_path.is_symlink():
        raise PathSecurityError(
            f"Symlink file rejected: {candidate_path}",
            context={"repo_root": str(resolved_root), "candidate": str(candidate_path)},
        )

    base_for_parts = _find_base_for_parts(raw_root, resolved_root, candidate_path)
    if base_for_parts is not None:
        _reject_intermediate_symlinks(base_for_parts, candidate_path, resolved_root)

    return _resolve_and_verify_regular_file(candidate_path, resolved_root)


def derive_repo_id(repo_root: Path) -> str:
    """Derive a collision-resistant repository ID from its resolved root path.

    Args:
        repo_root: Path to the repository root directory.

    Returns:
        Stable identifier in format ``repo:<name>:<8-char-sha256>``.
    """
    resolved = Path(repo_root).resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{PREFIX_REPO}{resolved.name}:{digest}"


def derive_file_id(repo_id: str, rel_path: str) -> str:
    """Derive a deterministic repository-scoped file ID.

    Args:
        repo_id: Owning repository identifier.
        rel_path: POSIX repository-relative file path.

    Returns:
        Stable identifier in format ``file:<repo_id>:<rel_path>``.
    """
    return f"{PREFIX_FILE}{repo_id}:{rel_path}"


def _relative_to_root(repo_root: Path, file_path: Path) -> Path:
    """Compute ``file_path`` relative to ``repo_root``, handling symlinked parent roots.

    Args:
        repo_root: Repository root directory.
        file_path: Target file path under ``repo_root``.

    Returns:
        Relative ``Path`` from ``repo_root`` to ``file_path``.

    Raises:
        ValueError: If ``file_path`` does not reside under ``repo_root`` even after resolution.
    """
    try:
        return file_path.relative_to(repo_root)
    except ValueError as exc:
        logger.debug(
            "Direct relative_to failed for %s against %s (%s); retrying with resolved paths",
            file_path,
            repo_root,
            exc,
        )
        return file_path.resolve().relative_to(Path(repo_root).resolve())


def to_relative_path(repo_root: Path, file_path: Path) -> str:
    """Convert a file path inside ``repo_root`` into a POSIX-style relative string.

    Args:
        repo_root: Repository root directory.
        file_path: File path inside ``repo_root``.

    Returns:
        Repository-relative POSIX path string.

    Raises:
        ValueError: If ``file_path`` is outside ``repo_root``.
    """
    return _relative_to_root(repo_root, file_path).as_posix()


def to_file_uri(file_path: Path) -> str:
    """Convert a filesystem path into a canonical ``file://`` URI.

    Args:
        file_path: Target filesystem path (resolved if relative).

    Returns:
        RFC 8089 ``file://`` URI string.
    """
    path_obj = Path(file_path)
    if not path_obj.is_absolute():
        path_obj = path_obj.resolve()
    return path_obj.as_uri()


def derive_module_path(repo_root: Path, file_path: Path) -> str:
    """Derive a dotted Python module path from a file's repository-relative location.

    Args:
        repo_root: Repository root directory.
        file_path: Path to a Python ``.py`` file inside ``repo_root``.

    Returns:
        Dotted Python module path (e.g., ``"app.services.auth"``). A top-level
        ``__init__.py`` returns the resolved repository directory name (or ``"__init__"``).
    """
    relative = _relative_to_root(repo_root, file_path)
    parts = list(relative.parts)

    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]

    if parts and parts[-1] == "__init__":
        parts = parts[:-1]

    if not parts:
        return Path(repo_root).resolve().name or "__init__"

    return ".".join(parts)
