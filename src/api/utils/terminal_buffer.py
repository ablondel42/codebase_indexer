"""Stateless UTF-8 character-boundary ring-buffer and POSIX exit-status helpers."""

import signal

from core.utils.logging import get_logger

logger = get_logger("api.utils.terminal_buffer")


def append_bounded_utf8(
    existing: bytes,
    chunk: bytes,
    byte_limit: int,
) -> tuple[bytes, bool]:
    """Append ``chunk`` and truncate from the front on UTF-8 character boundaries."""
    combined = existing + chunk
    if byte_limit == 0:
        return b"", bool(combined)
    if len(combined) <= byte_limit:
        return combined, False
    start = len(combined) - byte_limit
    # Advance past UTF-8 continuation bytes (10xxxxxx) so truncation lands on a scalar start.
    while start < len(combined) and (combined[start] & 0xC0) == 0x80:
        start += 1
    return combined[start:], True


def decode_terminal_buffer(buffer: bytes) -> str:
    """Decode retained terminal bytes cleanly as UTF-8 text."""
    return buffer.decode("utf-8", errors="replace")


def signal_name_from_returncode(returncode: int) -> tuple[int | None, str | None]:
    """Translate a process returncode into ACP ``(exitCode, signal)`` fields."""
    if returncode >= 0:
        return returncode, None
    sig_num = -returncode
    try:
        return None, signal.Signals(sig_num).name
    except ValueError as exc:
        logger.debug("Unknown POSIX signal %s: %s", sig_num, exc)
        return None, f"SIG{sig_num}"


__all__ = [
    "append_bounded_utf8",
    "decode_terminal_buffer",
    "signal_name_from_returncode",
]
