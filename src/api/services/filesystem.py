"""Host-mode sandboxed ``fs/read_text_file`` and ``fs/write_text_file`` execution."""

import asyncio
import os
import tempfile
from pathlib import Path

from api.models.client_ops import (
    ReadTextFileParams,
    ReadTextFileResult,
    WriteTextFileParams,
    WriteTextFileResult,
)
from api.models.config import CapabilityPolicyConfig
from api.models.session import SessionRootSet
from api.services.logging import AcpAuditLogger
from api.utils.security import is_sensitive_write_path, validate_path_in_root_set
from core.models.exceptions import (
    AcpInvalidParamsError,
    AcpResourceNotFoundError,
    AcpSecurityError,
)
from core.utils.logging import get_logger

logger = get_logger("api.services.filesystem")


class HostFileSystemService:
    """Executes ``fs/read_text_file`` and ``fs/write_text_file`` confined to session root sets."""

    def __init__(self, policy: CapabilityPolicyConfig, audit: AcpAuditLogger) -> None:
        """Initialize the host filesystem service with policy caps and audit logger."""
        self._policy = policy
        self._audit = audit

    @staticmethod
    def _read_sliced_lines(
        path: Path,
        line: int | None,
        limit: int | None,
    ) -> str:
        """Read UTF-8 text from ``path`` and slice by 1-based ``line`` and ``limit``."""
        text = path.read_text(encoding="utf-8", errors="replace")
        if line is None and limit is None:
            return text
        lines = text.splitlines(keepends=True)
        start = max(0, line - 1) if line is not None and line >= 1 else 0
        end = start + limit if limit is not None else None
        return "".join(lines[start:end])

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        """Create parent directories and atomically replace ``path`` with UTF-8 ``content``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".acp_write_", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(tmp_path, path)
        except OSError as exc:
            if tmp_path.exists():
                tmp_path.unlink()
            raise AcpInvalidParamsError(f"Failed writing file '{path.name}': {exc}") from exc

    async def read_text_file(
        self,
        params: ReadTextFileParams,
        root_set: SessionRootSet | None,
    ) -> ReadTextFileResult:
        """Read bounded text lines from ``params.path`` inside ``root_set``."""
        resolved = validate_path_in_root_set(params.path, root_set, allow_missing_leaf=False)
        if not resolved.exists():
            raise AcpResourceNotFoundError(
                f"File not found: '{params.path}'",
                wire_data={"path": params.path},
            )
        if not resolved.is_file():
            raise AcpInvalidParamsError("Target path is not a regular file")
        try:
            size_bytes = resolved.stat().st_size
        except OSError as exc:
            raise AcpResourceNotFoundError(
                f"Cannot stat file: '{params.path}'",
                wire_data={"path": params.path},
            ) from exc
        if size_bytes > self._policy.fs_max_read_bytes:
            self._audit.record(
                "fs_read_blocked",
                outcome="blocked",
                details={"path": params.path, "size_bytes": size_bytes},
            )
            raise AcpSecurityError("File exceeds configured fs_max_read_bytes limit")

        content = await asyncio.to_thread(
            self._read_sliced_lines,
            resolved,
            params.line,
            params.limit,
        )
        self._audit.record("fs_read", outcome="allowed", details={"path": str(resolved)})
        return ReadTextFileResult(content=content)

    async def write_text_file(
        self,
        params: WriteTextFileParams,
        root_set: SessionRootSet | None,
    ) -> WriteTextFileResult:
        """Atomically write ``params.content`` to ``params.path`` inside ``root_set``."""
        content_bytes = len(params.content.encode("utf-8"))
        if content_bytes > self._policy.fs_max_write_bytes:
            self._audit.record(
                "fs_write_blocked",
                outcome="blocked",
                details={"path": params.path, "size_bytes": content_bytes},
            )
            raise AcpSecurityError("Write payload exceeds configured fs_max_write_bytes limit")

        resolved = validate_path_in_root_set(params.path, root_set, allow_missing_leaf=True)
        if (
            root_set is not None
            and not self._policy.fs_allow_sensitive_writes
            and is_sensitive_write_path(resolved, root_set)
        ):
            self._audit.record(
                "sensitive_write_blocked",
                outcome="blocked",
                details={"path": str(resolved)},
            )
            raise AcpSecurityError(
                "Refusing write to sensitive hook, IDE, or MCP configuration path",
                wire_data={"path": params.path},
            )

        await asyncio.to_thread(self._atomic_write_text, resolved, params.content)
        self._audit.record("fs_write", outcome="allowed", details={"path": str(resolved)})
        return WriteTextFileResult()


__all__ = [
    "HostFileSystemService",
]
