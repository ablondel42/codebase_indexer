"""Single and batch symbol selector resolution service."""

from collections.abc import Sequence
from typing import Any

from core.models.enums import PREFIX_SYMBOL
from core.models.exceptions import SymbolNotFoundError, ValidationError
from core.models.node import SymbolSummary
from core.ports.uow import UnitOfWorkProtocol
from core.utils.file_io import format_line_span
from core.utils.logging import get_logger
from graph.utils.predicates import derive_target_lines

logger = get_logger("engine.services.resolver")


class SymbolTargetResolver:
    """Resolves user symbol selectors to unique ``SymbolSummary`` graph projections."""

    def __init__(self, *, candidate_limit: int = 25) -> None:
        """Initialize the symbol resolver.

        Args:
            candidate_limit: Maximum disambiguation candidates fetched per lookup.

        Raises:
            ValidationError: If ``candidate_limit < 2``.
        """
        if candidate_limit < 2:
            raise ValidationError(
                "candidate_limit must be >= 2",
                context={"candidate_limit": candidate_limit},
            )
        self._candidate_limit = candidate_limit

    @staticmethod
    def _format_candidate(candidate: SymbolSummary) -> dict[str, Any]:
        """Format a ``SymbolSummary`` into a compact disambiguation dictionary.

        Args:
            candidate: Candidate ``SymbolSummary`` record.

        Returns:
            Dictionary carrying ``id``, ``kind``, ``qualified_name``, and non-null
            ``file_path``, ``lines``, and ``signature`` attributes.
        """
        start_line, end_line = derive_target_lines(
            candidate.start_line,
            candidate.end_line,
            candidate.decl_range,
        )
        entry: dict[str, Any] = {
            "id": candidate.id,
            "kind": str(candidate.kind),
            "qualified_name": candidate.qualified_name,
        }
        if candidate.location is not None:
            entry["location"] = candidate.location
        if candidate.file_path:
            entry["file_path"] = candidate.file_path
        lines = format_line_span(start_line, end_line)
        if lines is not None:
            entry["lines"] = lines
        if candidate.signature is not None:
            entry["signature"] = candidate.signature
        return entry

    def resolve_repo_id(
        self,
        uow: UnitOfWorkProtocol,
        repo_selector: str | None,
        *,
        symbols: Sequence[str] | None = None,
    ) -> str | None:
        """Resolve an explicit or sole-repository ID when needed by symbol selectors.

        Skips repository resolution only when ``repo_selector`` is empty/``None`` and
        every provided selector already uses a canonical ``sym:...`` identifier.

        Args:
            uow: Active ``UnitOfWorkProtocol`` session.
            repo_selector: Optional repository ID, short name, or root path.
            symbols: Optional symbol selectors being resolved.

        Returns:
            Canonical ``repo_id`` string, or ``None`` when ``sym:...`` lookup is unscoped.

        Raises:
            DatabaseError: If repository resolution fails or is ambiguous.
        """
        has_explicit_repo = bool(repo_selector is not None and repo_selector.strip())
        has_name_selector = bool(
            symbols and any(not item.strip().startswith(PREFIX_SYMBOL) for item in symbols)
        )
        if has_explicit_repo or has_name_selector:
            normalized_selector = (
                repo_selector.strip() if has_explicit_repo and repo_selector else None
            )
            return uow.repos.resolve_id(normalized_selector)
        return None

    def _resolve_with_repo_id(
        self,
        uow: UnitOfWorkProtocol,
        cleaned: str,
        repo_id: str | None,
    ) -> SymbolSummary:
        """Resolve a trimmed non-empty selector within optional ``repo_id`` scope.

        Args:
            uow: Active ``UnitOfWorkProtocol`` session.
            cleaned: Non-empty trimmed symbol selector.
            repo_id: Optional canonical ``repo_id`` filter.

        Returns:
            Uniquely resolved ``SymbolSummary`` projection.

        Raises:
            SymbolNotFoundError: With ``error_code="symbol_not_found"`` when zero
                candidates match, or ``error_code="ambiguous_symbol"`` with structured
                ``candidates`` when multiple symbols match.
        """
        candidates = uow.nodes.resolve_candidates(
            repo_id,
            cleaned,
            limit=self._candidate_limit,
        )
        if not candidates:
            logger.debug("Symbol not found for selector %r (repo_id=%s)", cleaned, repo_id)
            raise SymbolNotFoundError(
                f"Symbol not found: {cleaned}",
                context={"symbol": cleaned, "repo_id": repo_id},
            )
        if len(candidates) == 1:
            return candidates[0]

        logger.debug(
            "Ambiguous symbol selector %r matched %d candidates (repo_id=%s)",
            cleaned,
            len(candidates),
            repo_id,
        )
        raise SymbolNotFoundError(
            f"Ambiguous symbol '{cleaned}' ({len(candidates)} matches).",
            error_code="ambiguous_symbol",
            context={"candidates": [self._format_candidate(cand) for cand in candidates]},
        )

    def resolve_one(
        self,
        uow: UnitOfWorkProtocol,
        selector: str,
        *,
        repo_selector: str | None = None,
    ) -> SymbolSummary:
        """Resolve a single symbol selector to its unique ``SymbolSummary``.

        Args:
            uow: Active ``UnitOfWorkProtocol`` session.
            selector: Symbol ID (``sym:...``), ``path/file.py:symbol``, qualified name,
                or unique short name.
            repo_selector: Optional repository ID, short name, or root path.

        Returns:
            Uniquely resolved ``SymbolSummary`` projection.

        Raises:
            SymbolNotFoundError: If ``selector`` is blank, unmatched, or ambiguous.
            DatabaseError: If repository lookup fails or is ambiguous.
        """
        cleaned = selector.strip()
        if not cleaned:
            raise SymbolNotFoundError("Symbol identifier must not be empty.")
        repo_id = self.resolve_repo_id(uow, repo_selector, symbols=[cleaned])
        return self._resolve_with_repo_id(uow, cleaned, repo_id)

    def resolve_many(
        self,
        uow: UnitOfWorkProtocol,
        selectors: Sequence[str],
        *,
        repo_selector: str | None = None,
    ) -> list[SymbolSummary]:
        """Resolve multiple symbol selectors in order while deduplicating by ``id``.

        Args:
            uow: Active ``UnitOfWorkProtocol`` session.
            selectors: Non-empty sequence of symbol selector strings.
            repo_selector: Optional repository ID, short name, or root path.

        Returns:
            Ordered, deduplicated list of resolved ``SymbolSummary`` projections.

        Raises:
            ValidationError: If ``selectors`` is empty.
            SymbolNotFoundError: If any selector is blank, unmatched, or ambiguous.
            DatabaseError: If repository lookup fails or is ambiguous.
        """
        if not selectors:
            raise ValidationError(
                "At least one symbol selector must be provided.",
                context={"selectors": []},
            )
        cleaned_list: list[str] = []
        for raw in selectors:
            cleaned = raw.strip()
            if not cleaned:
                raise SymbolNotFoundError("Symbol identifier must not be empty.")
            cleaned_list.append(cleaned)

        repo_id = self.resolve_repo_id(uow, repo_selector, symbols=cleaned_list)
        results: list[SymbolSummary] = []
        seen_ids: set[str] = set()
        for cleaned in cleaned_list:
            target = self._resolve_with_repo_id(uow, cleaned, repo_id)
            if target.id not in seen_ids:
                seen_ids.add(target.id)
                results.append(target)
        return results


__all__ = [
    "SymbolTargetResolver",
]
