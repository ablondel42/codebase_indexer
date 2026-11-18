"""Path manipulation and repository security utilities."""

import hashlib
import stat
from pathlib import Path

from repo_context.exceptions import PathSecurityError


def normalize_repo_root(path: str | Path) -> Path:
    """Normalize and validate a repository root path.

    Args:
        path: Path to the repository root.

    Returns:
        Resolved absolute path.

    Raises:
        FileNotFoundError: If the path does not exist.
        NotADirectoryError: If the path is not a directory.
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
    """Find the root prefix matching ``candidate_path`` for intermediate symlink checks."""
    if candidate_path.is_relative_to(resolved_root):
        return resolved_root
    if candidate_path.is_relative_to(raw_root):
        return raw_root
    for parent in reversed(candidate_path.parents):
        try:
            if parent.resolve() == resolved_root:
                return parent
        except OSError:
            continue
    return None


def _reject_intermediate_symlinks(
    base_for_parts: Path,
    candidate_path: Path,
    resolved_root: Path,
) -> None:
    """Reject intermediate symlink components below ``base_for_parts``."""
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
    """Resolve ``candidate_path`` and verify containment and regular-file mode."""
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
        st = resolved_candidate.stat()
    except OSError as exc:
        raise PathSecurityError(
            f"Repository file is not accessible: {resolved_candidate}",
            context={"repo_root": str(resolved_root), "resolved": str(resolved_candidate)},
        ) from exc

    if not stat.S_ISREG(st.st_mode):
        raise PathSecurityError(
            f"Repository path is not a regular file: {resolved_candidate}",
            context={"repo_root": str(resolved_root), "resolved": str(resolved_candidate)},
        )

    return resolved_candidate


def ensure_safe_repo_file(repo_root: Path, candidate: Path | str) -> Path:
    """Validate that a candidate file safely resides inside ``repo_root``.

    Rejects symlinks (both the candidate itself and any symlink component below
    ``repo_root``), paths escaping ``repo_root.resolve()``, missing targets, and
    non-regular files.

    Args:
        repo_root: Repository root directory.
        candidate: Candidate file path (absolute or relative to ``repo_root``).

    Returns:
        Resolved absolute ``Path`` to the verified regular file.

    Raises:
        PathSecurityError: If any security or regular-file check fails.
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

    # Reject intermediate symlink components below repo_root without flagging
    # system-level symlinks above repo_root (e.g. /tmp -> /private/tmp on macOS).
    base_for_parts = _find_base_for_parts(raw_root, resolved_root, candidate_path)
    if base_for_parts is not None:
        _reject_intermediate_symlinks(base_for_parts, candidate_path, resolved_root)

    return _resolve_and_verify_regular_file(candidate_path, resolved_root)


def derive_repo_id(repo_root: Path) -> str:
    """Derive a collision-resistant repository ID from its resolved root path.

    Args:
        repo_root: Path to the repository root.

    Returns:
        Stable ID in format ``repo:<name>:<8-char-sha256>``.
    """
    resolved = Path(repo_root).resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"repo:{resolved.name}:{digest}"


def derive_file_id(repo_id: str, rel_path: str) -> str:
    """Derive a repo-scoped file ID.

    Args:
        repo_id: Repository identifier.
        rel_path: POSIX repo-relative file path.

    Returns:
        Stable ID in format ``file:<repo_id>:<rel_path>``.
    """
    return f"file:{repo_id}:{rel_path}"


def _relative_to_root(repo_root: Path, file_path: Path) -> Path:
    """Compute relative path handling unresolved/resolved root mismatches."""
    try:
        return file_path.relative_to(repo_root)
    except ValueError:
        return file_path.resolve().relative_to(Path(repo_root).resolve())


def to_relative_path(repo_root: Path, file_path: Path) -> str:
    """Convert an absolute file path to a repo-relative POSIX-style path.

    Args:
        repo_root: The repository root directory.
        file_path: The absolute file path.

    Returns:
        Repo-relative path as a POSIX-style string.
    """
    return _relative_to_root(repo_root, file_path).as_posix()


def to_file_uri(file_path: Path) -> str:
    """Convert a file path to a ``file://`` URI.

    Args:
        file_path: The file path.

    Returns:
        A valid ``file://`` URI string.
    """
    path_obj = Path(file_path)
    if not path_obj.is_absolute():
        path_obj = path_obj.resolve()
    return path_obj.as_uri()


def derive_module_path(repo_root: Path, file_path: Path) -> str:
    """Derive a Python module path from a file's filesystem location.

    Args:
        repo_root: The repository root directory.
        file_path: The absolute file path to a ``.py`` file.

    Returns:
        Python module path (e.g., ``app.services.auth``). Root ``__init__.py``
        returns the resolved repository directory name (or ``__init__``).
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
