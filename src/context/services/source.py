"""Safe, line-bounded single and batched source snippet reader service."""

from collections.abc import Sequence
from pathlib import Path

from context.models.source import SnippetOptions, SourceSlice, SymbolSnippet
from context.utils.formatting import resolve_symbol_line_bounds, slice_source_window
from core.models.config import AppConfig
from core.models.exceptions import DatabaseError, SymbolNotFoundError, ValidationError
from core.models.file import FileRecord, VerifiedFileText
from core.models.node import SymbolNode, SymbolSummary
from core.models.repo import RepoRecord
from core.ports.uow import UnitOfWorkProtocol
from core.utils.file_io import format_line_span, read_verified_file_text
from core.utils.logging import get_logger

logger = get_logger("context.services.source")


class SourceReader:
    """Reads confined, hash-verified file slices and single/batched symbol snippets."""

    def __init__(self, config: AppConfig | None = None) -> None:
        """Initialize the reader with optional runtime configuration.

        Args:
            config: Optional ``AppConfig`` controlling ``max_file_size_bytes``.
        """
        self._config = config if config is not None else AppConfig()

    @staticmethod
    def _validate_line_bounds(start_line: int, end_line: int) -> None:
        """Validate 1-based inclusive ``start_line`` and ``end_line`` invariants.

        Args:
            start_line: One-based inclusive start line.
            end_line: One-based inclusive end line.

        Raises:
            ValidationError: If ``start_line < 1`` or ``end_line < start_line``.
        """
        if start_line < 1:
            raise ValidationError("start_line must be >= 1", context={"start_line": start_line})
        if end_line < start_line:
            raise ValidationError(
                "end_line must be >= start_line",
                context={"start_line": start_line, "end_line": end_line},
            )

    def read_file_snippet(
        self,
        repo_root: Path | str,
        file_path: str,
        start_line: int,
        end_line: int,
        options: SnippetOptions | None = None,
    ) -> SourceSlice:
        """Read a confined, hash-verified source slice from ``file_path`` under ``repo_root``.

        Args:
            repo_root: Repository root directory path.
            file_path: Repository-relative POSIX file path.
            start_line: One-based inclusive start line.
            end_line: One-based inclusive end line.
            options: Optional pagination, padding, and content-hash settings.

        Returns:
            ``SourceSlice`` containing both ``raw_source`` and ``numbered_source``.

        Raises:
            ValidationError: If ``start_line`` or ``end_line`` is invalid.
            PathSecurityError: If ``file_path`` escapes ``repo_root`` or traverses symlinks.
            ParseError: If reading fails or the file exceeds ``max_file_size_bytes``.
        """
        self._validate_line_bounds(start_line, end_line)
        opts = options if options is not None else SnippetOptions()
        verified = read_verified_file_text(
            repo_root,
            file_path,
            expected_content_hash=opts.expected_content_hash,
            max_file_size_bytes=self._config.max_file_size_bytes,
        )
        window = slice_source_window(verified.lines, start_line, end_line, opts)
        return SourceSlice(
            file_path=file_path,
            start_line=window.start_line,
            end_line=window.end_line,
            window_start_line=window.window_start_line,
            window_end_line=window.window_end_line,
            raw_source=window.raw_source,
            numbered_source=window.numbered_source,
            truncated=window.truncated,
            total_lines=window.total_lines,
            returned_lines=window.returned_lines,
            next_offset=window.next_offset,
            is_fresh=verified.is_fresh,
            stale_reason=verified.stale_reason,
        )

    @staticmethod
    def _assemble_symbol_snippet(
        symbol: SymbolNode | SymbolSummary,
        file_record: FileRecord,
        source_slice: SourceSlice,
    ) -> SymbolSnippet:
        """Combine symbol declaration metadata with a ``SourceSlice`` into ``SymbolSnippet``.

        Args:
            symbol: ``SymbolNode`` or ``SymbolSummary`` entity.
            file_record: Containing ``FileRecord``.
            source_slice: Sliced file source window.

        Returns:
            Populated ``SymbolSnippet`` model.
        """
        span_str = (
            format_line_span(symbol.start_line, symbol.end_line)
            or f"{source_slice.start_line}-{source_slice.end_line}"
        )
        return SymbolSnippet(
            symbol_id=symbol.id,
            name=symbol.name,
            qualified_name=symbol.qualified_name,
            kind=symbol.kind,
            file_path=file_record.file_path,
            module_path=file_record.module_path,
            language=str(symbol.language),
            signature=symbol.signature,
            doc_summary=symbol.doc_summary,
            start_line=source_slice.start_line,
            end_line=source_slice.end_line,
            decl_range=symbol.decl_range,
            selection_range=symbol.selection_range,
            lines=span_str,
            window_start_line=source_slice.window_start_line,
            window_end_line=source_slice.window_end_line,
            raw_source=source_slice.raw_source,
            numbered_source=source_slice.numbered_source,
            truncated=source_slice.truncated,
            total_lines=source_slice.total_lines,
            returned_lines=source_slice.returned_lines,
            next_offset=source_slice.next_offset,
            is_fresh=source_slice.is_fresh,
            stale_reason=source_slice.stale_reason,
        )

    @staticmethod
    def _require_file_and_repo(
        uow: UnitOfWorkProtocol,
        symbol_id: str,
        file_id: str,
        repo_id: str,
    ) -> tuple[FileRecord, RepoRecord]:
        """Fetch and validate ``FileRecord`` and ``RepoRecord`` for ``symbol_id``.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Target symbol identifier for error context.
            file_id: Containing file identifier.
            repo_id: Owning repository identifier.

        Returns:
            Tuple ``(file_record, repo_record)``.

        Raises:
            DatabaseError: If either record is missing from persistence.
        """
        file_record = uow.files.get_by_id(file_id)
        if file_record is None:
            raise DatabaseError(
                f"File record not found for symbol: {symbol_id}",
                error_code="file_not_found",
                context={"symbol_id": symbol_id, "file_id": file_id},
            )
        repo_record = uow.repos.get_by_id(repo_id)
        if repo_record is None:
            raise DatabaseError(
                f"Repository not found: {repo_id}",
                error_code="repo_not_found",
                context={"repo_id": repo_id},
            )
        return file_record, repo_record

    def read_symbol_snippet(
        self,
        uow: UnitOfWorkProtocol,
        symbol_id: str,
        options: SnippetOptions | None = None,
    ) -> SymbolSnippet:
        """Read a confined, hash-verified ``SymbolSnippet`` for ``symbol_id``.

        Args:
            uow: Active read Unit of Work.
            symbol_id: Canonical symbol identifier.
            options: Optional pagination, padding, and hash override settings.

        Returns:
            Populated ``SymbolSnippet`` carrying both ``raw_source`` and ``numbered_source``.

        Raises:
            SymbolNotFoundError: If ``symbol_id`` does not exist.
            DatabaseError: If the owning file or repository record is missing.
            ValidationError: If the symbol lacks line range metadata.
            PathSecurityError: If the backing file violates repository confinement.
            ParseError: If reading fails or exceeds ``max_file_size_bytes``.
        """
        node = uow.nodes.get_by_id(symbol_id)
        if node is None:
            raise SymbolNotFoundError(
                f"Symbol not found: {symbol_id}",
                context={"symbol_id": symbol_id},
            )

        file_record, repo_record = self._require_file_and_repo(
            uow,
            symbol_id,
            node.file_id,
            node.repo_id,
        )
        start_line, end_line = resolve_symbol_line_bounds(node)
        base_opts = options if options is not None else SnippetOptions()
        effective_opts = (
            base_opts
            if base_opts.expected_content_hash is not None
            else base_opts.model_copy(update={"expected_content_hash": file_record.content_hash})
        )
        source_slice = self.read_file_snippet(
            repo_record.root_path,
            file_record.file_path,
            start_line,
            end_line,
            effective_opts,
        )
        return self._assemble_symbol_snippet(node, file_record, source_slice)

    def _read_verified_cached(
        self,
        file_record: FileRecord,
        repo_record: RepoRecord,
        options: SnippetOptions,
        verified_cache: dict[str, VerifiedFileText],
    ) -> VerifiedFileText:
        """Read and verify ``file_record`` from disk at most once per batch call.

        Args:
            file_record: Target ``FileRecord``.
            repo_record: Owning ``RepoRecord``.
            options: Batch ``SnippetOptions``.
            verified_cache: Mutable per-batch cache keyed by ``file_id``.

        Returns:
            Cached ``VerifiedFileText`` instance.
        """
        cached = verified_cache.get(file_record.id)
        if cached is not None:
            return cached
        expected_hash = (
            options.expected_content_hash
            if options.expected_content_hash is not None
            else file_record.content_hash
        )
        verified = read_verified_file_text(
            repo_record.root_path,
            file_record.file_path,
            expected_content_hash=expected_hash,
            max_file_size_bytes=self._config.max_file_size_bytes,
        )
        verified_cache[file_record.id] = verified
        return verified

    @staticmethod
    def _load_batch_metadata(
        uow: UnitOfWorkProtocol,
        ordered_ids: Sequence[str],
    ) -> tuple[
        dict[str, SymbolSummary],
        dict[str, FileRecord],
        dict[str, RepoRecord],
    ]:
        """Batch-load summaries, files, and repositories for ``ordered_ids``.

        Args:
            uow: Active read Unit of Work.
            ordered_ids: Deduplicated symbol IDs in requested order.

        Returns:
            Tuple ``(summaries_by_id, files_by_id, repos_by_id)``.

        Raises:
            SymbolNotFoundError: If any requested symbol ID is missing.
            DatabaseError: If any referenced file or repository record is missing.
        """
        summaries_by_id = uow.nodes.get_summaries_by_ids(ordered_ids)
        missing = [sid for sid in ordered_ids if sid not in summaries_by_id]
        if missing:
            raise SymbolNotFoundError(
                f"Symbol not found: {missing[0]}",
                context={"symbol_id": missing[0], "missing_symbol_ids": missing},
            )

        files_by_id = uow.files.get_by_ids([summaries_by_id[sid].file_id for sid in ordered_ids])
        repos_by_id: dict[str, RepoRecord] = {}
        for sid in ordered_ids:
            summary = summaries_by_id[sid]
            if summary.file_id not in files_by_id:
                raise DatabaseError(
                    f"File record not found for symbol: {sid}",
                    error_code="file_not_found",
                    context={"symbol_id": sid, "file_id": summary.file_id},
                )
            if summary.repo_id not in repos_by_id:
                repo = uow.repos.get_by_id(summary.repo_id)
                if repo is None:
                    raise DatabaseError(
                        f"Repository not found: {summary.repo_id}",
                        error_code="repo_not_found",
                        context={"repo_id": summary.repo_id},
                    )
                repos_by_id[summary.repo_id] = repo
        return summaries_by_id, files_by_id, repos_by_id

    @staticmethod
    def _slice_verified_symbol(
        summary: SymbolSummary,
        file_record: FileRecord,
        verified: VerifiedFileText,
        options: SnippetOptions,
    ) -> SymbolSnippet:
        """Slice ``verified.lines`` for ``summary`` into a ``SymbolSnippet``.

        Args:
            summary: Target ``SymbolSummary``.
            file_record: Containing ``FileRecord``.
            verified: Cached ``VerifiedFileText`` for ``file_record``.
            options: Batch ``SnippetOptions``.

        Returns:
            Populated ``SymbolSnippet``.
        """
        start_line, end_line = resolve_symbol_line_bounds(summary)
        window = slice_source_window(verified.lines, start_line, end_line, options)
        source_slice = SourceSlice(
            file_path=file_record.file_path,
            start_line=window.start_line,
            end_line=window.end_line,
            window_start_line=window.window_start_line,
            window_end_line=window.window_end_line,
            raw_source=window.raw_source,
            numbered_source=window.numbered_source,
            truncated=window.truncated,
            total_lines=window.total_lines,
            returned_lines=window.returned_lines,
            next_offset=window.next_offset,
            is_fresh=verified.is_fresh,
            stale_reason=verified.stale_reason,
        )
        return SourceReader._assemble_symbol_snippet(summary, file_record, source_slice)

    def read_symbol_snippets(
        self,
        uow: UnitOfWorkProtocol,
        symbol_ids: Sequence[str],
        options: SnippetOptions | None = None,
    ) -> list[SymbolSnippet]:
        """Batch-read ``SymbolSnippet`` items reading each unique file from disk at most once.

        Args:
            uow: Active read Unit of Work.
            symbol_ids: Ordered sequence of canonical symbol identifiers.
            options: Optional pagination, padding, and hash settings applied to each snippet.

        Returns:
            Ordered deduplicated list of ``SymbolSnippet`` models.

        Raises:
            SymbolNotFoundError: If any requested symbol ID does not exist.
            DatabaseError: If any owning file or repository record is missing.
            ValidationError: If any symbol lacks usable line range metadata.
            PathSecurityError: If any backing file violates repository confinement.
            ParseError: If any backing file cannot be read or exceeds ``max_file_size_bytes``.
        """
        ordered_ids = list(dict.fromkeys(sid.strip() for sid in symbol_ids if sid and sid.strip()))
        if not ordered_ids:
            return []

        opts = options if options is not None else SnippetOptions()
        summaries_by_id, files_by_id, repos_by_id = self._load_batch_metadata(uow, ordered_ids)
        verified_cache: dict[str, VerifiedFileText] = {}
        snippets: list[SymbolSnippet] = []

        for sid in ordered_ids:
            summary = summaries_by_id[sid]
            file_record = files_by_id[summary.file_id]
            repo_record = repos_by_id[summary.repo_id]
            verified = self._read_verified_cached(file_record, repo_record, opts, verified_cache)
            snippets.append(self._slice_verified_symbol(summary, file_record, verified, opts))

        logger.debug(
            "Batch-read %d symbol snippet(s) across %d unique disk file(s)",
            len(snippets),
            len(verified_cache),
        )
        return snippets


__all__ = [
    "SourceReader",
]
