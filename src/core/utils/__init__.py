"""Public re-exports for ``core.utils``."""

from core.models.file import FreshnessCheckResult, VerifiedFileText
from core.utils.file_io import (
    check_file_disk_freshness,
    extract_line_preview,
    format_line_span,
    read_verified_file_text,
)
from core.utils.hashing import compute_symbol_content_hash, sha256_file, sha256_text
from core.utils.json_codec import from_json, to_json
from core.utils.logging import (
    ROOT_LOGGER_NAME,
    configure_logging,
    get_logger,
    is_logging_configured,
    reset_logging,
)
from core.utils.pathing import (
    derive_file_id,
    derive_module_path,
    derive_repo_id,
    ensure_safe_repo_file,
    normalize_repo_root,
    to_file_uri,
    to_relative_path,
)
from core.utils.timestamps import latest_timestamp, utc_now_iso, validate_iso_timestamp

__all__ = [
    "FreshnessCheckResult",
    "ROOT_LOGGER_NAME",
    "VerifiedFileText",
    "check_file_disk_freshness",
    "compute_symbol_content_hash",
    "configure_logging",
    "derive_file_id",
    "derive_module_path",
    "derive_repo_id",
    "ensure_safe_repo_file",
    "extract_line_preview",
    "format_line_span",
    "from_json",
    "get_logger",
    "is_logging_configured",
    "latest_timestamp",
    "normalize_repo_root",
    "read_verified_file_text",
    "reset_logging",
    "sha256_file",
    "sha256_text",
    "to_file_uri",
    "to_json",
    "to_relative_path",
    "utc_now_iso",
    "validate_iso_timestamp",
]
