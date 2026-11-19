"""ISO 8601 UTC timestamp generation, validation, and comparison utilities."""

from datetime import UTC, datetime

from core.utils.logging import get_logger

logger = get_logger("core.utils.timestamps")


def utc_now_iso() -> str:
    """Return the current UTC timestamp formatted as an ISO 8601 string.

    Returns:
        Current UTC timestamp string (e.g., ``"2026-11-19T13:20:09.123456+00:00"``).
    """
    return datetime.now(UTC).isoformat()


def validate_iso_timestamp(value: str | None) -> str | None:
    """Validate that a string is a well-formed ISO 8601 date or timestamp.

    Args:
        value: Candidate timestamp string or ``None``.

    Returns:
        ``value`` unchanged when valid, or ``None`` when ``value`` is ``None``.

    Raises:
        ValueError: If ``value`` is non-``None`` and is empty or invalid ISO 8601.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Timestamp must be ISO 8601 format (e.g., '2026-01-01T00:00:00Z')")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "Timestamp must be ISO 8601 format (e.g., '2026-01-01T00:00:00Z')"
        ) from exc
    return value


def _parse_comparable_utc(raw: str) -> datetime | None:
    """Parse an ISO 8601 string into a timezone-aware UTC ``datetime`` for comparison.

    Args:
        raw: Candidate ISO 8601 timestamp string.

    Returns:
        Timezone-aware ``datetime`` or ``None`` if parsing fails (after logging a warning).
    """
    if not raw.strip():
        logger.warning("Ignoring blank timestamp string during latest_timestamp comparison")
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        logger.warning("Ignoring invalid ISO timestamp %r during comparison: %s", raw, exc)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def latest_timestamp(*timestamps: str | None) -> str | None:
    """Return the chronologically latest timestamp string among the given candidates.

    Invalid non-``None`` strings are logged at ``WARNING`` level and skipped rather
    than silently swallowed.

    Args:
        *timestamps: Variable number of ISO 8601 timestamp strings or ``None``.

    Returns:
        The original string representing the latest instant, or ``None`` if no valid
        timestamps were provided.
    """
    best_dt: datetime | None = None
    best_raw: str | None = None

    for candidate in timestamps:
        if candidate is None:
            continue
        parsed = _parse_comparable_utc(candidate)
        if parsed is None:
            continue
        if best_dt is None or parsed > best_dt:
            best_dt = parsed
            best_raw = candidate

    return best_raw
