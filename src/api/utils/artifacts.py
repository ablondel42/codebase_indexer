"""Stateless read-only helpers for inspecting ``brain/<sessionId>/*.md`` artifacts."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from api.models.session import (
    SessionArtifactContent,
    SessionArtifactList,
    SessionArtifactSummary,
)
from core.models.exceptions import AcpError
from core.utils.logging import get_logger

logger = get_logger("api.utils.artifacts")

MAX_ARTIFACT_BYTES: int = 524_288
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.md$")


def validate_artifact_session_id(session_id: str) -> str:
    """Validate that ``session_id`` is a safe single-segment directory identifier."""
    cleaned = session_id.strip()
    if not _SAFE_SESSION_ID_RE.fullmatch(cleaned) or ".." in cleaned:
        raise AcpError(
            f"Invalid session identifier '{session_id}'.",
            context={"session_id": session_id},
        )
    return cleaned


def validate_artifact_filename(artifact_name: str) -> str:
    """Validate that ``artifact_name`` is a safe top-level ``.md`` filename."""
    cleaned = artifact_name.strip()
    if not _SAFE_ARTIFACT_NAME_RE.fullmatch(cleaned) or ".." in cleaned:
        raise AcpError(
            f"Invalid markdown artifact filename '{artifact_name}'.",
            context={"artifact_name": artifact_name},
        )
    return cleaned


def _resolve_session_brain_dir(profile_root: Path, session_id: str) -> Path | None:
    """Resolve ``<profile_root>/brain/<session_id>`` while rejecting symlink escapes."""
    clean_id = validate_artifact_session_id(session_id)
    brain_root = (profile_root / "brain").resolve()
    candidate = profile_root / "brain" / clean_id
    if candidate.is_symlink() or not candidate.is_dir():
        return None
    resolved = candidate.resolve()
    if not resolved.is_relative_to(brain_root):
        raise AcpError(
            f"Session brain directory escaped containment for '{clean_id}'.",
            context={"session_id": clean_id},
        )
    return resolved


def _format_mtime_iso(mtime_seconds: float) -> str:
    """Format POSIX modification timestamp as UTC ISO-8601 string."""
    return datetime.fromtimestamp(mtime_seconds, tz=UTC).isoformat().replace("+00:00", "Z")


def _parse_sidecar_json(raw_json: str, sidecar_path: Path) -> tuple[str | None, str | None, bool]:
    """Extract ``(summary, updated_at, user_facing)`` from sidecar JSON text."""
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.debug("Ignoring malformed artifact sidecar %s: %s", sidecar_path, exc)
        return None, None, True
    if not isinstance(parsed, dict):
        return None, None, True
    summary = parsed.get("summary") if isinstance(parsed.get("summary"), str) else None
    updated_at = parsed.get("updatedAt") if isinstance(parsed.get("updatedAt"), str) else None
    user_facing = parsed.get("userFacing") is not False
    return summary, updated_at, user_facing


def _read_artifact_sidecar(md_path: Path) -> tuple[str | None, str | None, bool]:
    """Read optional ``<name>.metadata.json`` sidecar next to an artifact file."""
    sidecar = md_path.with_name(f"{md_path.name}.metadata.json")
    if sidecar.is_symlink() or not sidecar.is_file():
        return None, None, True
    try:
        raw_text = sidecar.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("Could not read artifact sidecar %s: %s", sidecar, exc)
        return None, None, True
    return _parse_sidecar_json(raw_text, sidecar)


def _is_contained_markdown_file(entry: Path, session_dir: Path) -> bool:
    """Return ``True`` when ``entry`` is a regular non-symlink ``.md`` file in ``session_dir``."""
    if entry.is_symlink() or not entry.is_file():
        return False
    if not _SAFE_ARTIFACT_NAME_RE.fullmatch(entry.name) or ".." in entry.name:
        return False
    return entry.resolve().is_relative_to(session_dir)


def _build_artifact_summary(entry: Path, session_dir: Path) -> SessionArtifactSummary | None:
    """Build ``SessionArtifactSummary`` for a validated ``.md`` file or ``None`` if skipped."""
    if not _is_contained_markdown_file(entry, session_dir):
        return None
    try:
        stat_res = entry.stat()
    except OSError as exc:
        logger.debug("Skipping unreadable artifact %s: %s", entry, exc)
        return None
    summary, updated_at, user_facing = _read_artifact_sidecar(entry)
    return SessionArtifactSummary(
        name=entry.name,
        summary=summary,
        updated_at=updated_at or _format_mtime_iso(stat_res.st_mtime),
        size_bytes=stat_res.st_size,
        user_facing=user_facing,
    )


def list_session_artifacts(profile_root: Path, session_id: str) -> SessionArtifactList:
    """List top-level markdown artifacts under ``<profile_root>/brain/<session_id>/``."""
    clean_id = validate_artifact_session_id(session_id)
    session_dir = _resolve_session_brain_dir(profile_root, clean_id)
    if session_dir is None:
        return SessionArtifactList(session_id=clean_id)
    summaries = [
        item
        for entry in session_dir.iterdir()
        if (item := _build_artifact_summary(entry, session_dir)) is not None
    ]
    summaries.sort(key=lambda item: (item.updated_at, item.name), reverse=True)
    return SessionArtifactList(session_id=clean_id, artifacts=tuple(summaries))


def read_session_artifact(
    profile_root: Path,
    session_id: str,
    artifact_name: str,
) -> SessionArtifactContent | None:
    """Read a validated markdown artifact from ``<profile_root>/brain/<session_id>/``."""
    clean_id = validate_artifact_session_id(session_id)
    clean_name = validate_artifact_filename(artifact_name)
    session_dir = _resolve_session_brain_dir(profile_root, clean_id)
    if session_dir is None:
        return None
    target = session_dir / clean_name
    if not _is_contained_markdown_file(target, session_dir):
        return None
    stat_res = target.stat()
    if stat_res.st_size > MAX_ARTIFACT_BYTES:
        raise AcpError(
            f"Artifact '{clean_name}' exceeds maximum readable size ({MAX_ARTIFACT_BYTES} bytes).",
            context={"artifact_name": clean_name, "size_bytes": stat_res.st_size},
        )
    summary, updated_at, _ = _read_artifact_sidecar(target)
    content = target.read_text(encoding="utf-8", errors="replace")
    return SessionArtifactContent(
        session_id=clean_id,
        name=clean_name,
        summary=summary,
        updated_at=updated_at or _format_mtime_iso(stat_res.st_mtime),
        size_bytes=stat_res.st_size,
        content=content,
    )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "list_session_artifacts",
    "read_session_artifact",
    "validate_artifact_filename",
    "validate_artifact_session_id",
]
