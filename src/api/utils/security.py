"""Stateless security, auth, root-set containment, capability negotiation, and env guards."""

import hmac
import ipaddress
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from api.models.acp import (
    LATEST_ACP_VERSION,
    AuthenticateParams,
    AuthMethod,
    BooleanConfigOptionCapabilities,
    ClientAuthCapabilities,
    ClientCapabilities,
    ClientFsCapabilities,
    ClientSessionCapabilities,
    CompactionCapabilities,
    ElicitationCapabilities,
    ElicitationFormCapabilities,
    ElicitationUrlCapabilities,
    InitializeParams,
    NoticeCapabilities,
    PlanCapabilities,
    SessionConfigOptionsCapabilities,
    SubagentCapabilities,
)
from api.models.config import (
    DEFAULT_AGY_APP_DATA_DIR,
    AgentConfig,
    CapabilityPolicy,
    CapabilityPolicyConfig,
    RelayOnlyPolicy,
    TransportConfig,
)
from api.models.prompt import SubagentSessionState
from api.models.session import EnvVariable, SessionRootSet
from api.utils.formatting import FORBIDDEN_SECRET_KEYS
from core.models.exceptions import AcpConfigError, AcpSecurityError
from core.utils.logging import get_logger

logger = get_logger("api.utils.security")

FORBIDDEN_META_KEYS: frozenset[str] = FORBIDDEN_SECRET_KEYS

SAFE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "TERM",
        "COLORTERM",
        "SYSTEMROOT",
        "APPDATA",
        "LOCALAPPDATA",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
)

SAFE_ENV_PREFIXES: tuple[str, ...] = (
    "LC_",
    "XDG_",
    "GOOGLE_",
    "GEMINI_",
    "AGY_",
    "ANTIGRAVITY_",
    "CLOUDSDK_",
)

DANGEROUS_TERMINAL_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "IFS",
        "CDPATH",
        "ENV",
        "BASH_ENV",
        "ZDOTDIR",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "RUBYOPT",
        "PERL5OPT",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    }
)

DANGEROUS_TERMINAL_ENV_PREFIXES: tuple[str, ...] = ("LD_", "DYLD_")

SENSITIVE_DIR_NAMES: frozenset[str] = frozenset(
    {".gemini", ".vscode", ".cursor", ".idea"}
)
SENSITIVE_FILE_NAMES: frozenset[str] = frozenset(
    {"mcp.json", ".mcp.json", ".bashrc", ".zshrc", ".zprofile", ".bash_profile", ".profile"}
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def is_loopback_host(host: str) -> bool:
    """Return ``True`` when ``host`` is ``localhost`` or a loopback IP literal."""
    cleaned = host.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if not cleaned:
        return False
    if cleaned.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError as exc:
        logger.debug("Rejecting non-IP non-localhost host %r: %s", host, exc)
        return False


def validate_bind_host(host: str, *, allow_non_loopback: bool) -> str:
    """Validate that server bind ``host`` is loopback unless explicitly overridden."""
    cleaned = host.strip()
    if not cleaned:
        raise AcpConfigError("API server bind host cannot be empty.")
    if not allow_non_loopback and not is_loopback_host(cleaned):
        raise AcpConfigError(
            f"Refusing non-loopback bind host '{cleaned}' without allow_non_loopback=True.",
            context={"host": cleaned},
        )
    return cleaned


def extract_host_name(host_header: str) -> str:
    """Extract lowercase hostname without port from an HTTP ``Host`` header."""
    raw = host_header.strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(f"//{raw}")
    except ValueError as exc:
        logger.debug("Invalid Host header %r: %s", host_header, exc)
        return ""
    return (parsed.hostname or "").lower()


def is_allowed_ws_host(host_header: str | None, *, allow_non_loopback: bool = False) -> bool:
    """Validate WebSocket ``Host`` header against DNS-rebinding attacks."""
    if not host_header:
        return False
    hostname = extract_host_name(host_header)
    if not hostname:
        return False
    return True if allow_non_loopback else is_loopback_host(hostname)


def normalize_origin(origin: str) -> str:
    """Normalize an HTTP(S) ``Origin`` to ``scheme://netloc`` in lowercase."""
    raw = origin.strip().rstrip("/")
    if not raw or raw.lower() == "null":
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        logger.debug("Invalid Origin header %r: %s", origin, exc)
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def is_allowed_ws_origin(origin_header: str | None, allowed_origins: Sequence[str]) -> bool:
    """Return ``True`` when WebSocket ``Origin`` strictly matches an allowlisted origin."""
    if not origin_header:
        return False
    normalized = normalize_origin(origin_header)
    if not normalized:
        return False
    allowed = {item for entry in allowed_origins if (item := normalize_origin(entry))}
    return normalized in allowed


def _is_bearer_token_valid(auth_header: str | None, expected_secret: str) -> bool:
    """Verify ``Authorization: Bearer <token>`` in constant time."""
    if not auth_header:
        return False
    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    presented = parts[1].strip()
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected_secret.encode("utf-8"))


def verify_ws_upgrade(
    headers: Mapping[str, str],
    transport: TransportConfig,
) -> tuple[bool, str]:
    """Verify Host, hybrid Origin, and optional Bearer token on WebSocket upgrade."""
    if not is_allowed_ws_host(headers.get("host"), allow_non_loopback=transport.allow_non_loopback):
        return False, "disallowed_host"

    expected_token = transport.auth_token.get_secret_value().strip() if transport.auth_token else ""
    if expected_token and not _is_bearer_token_valid(headers.get("authorization"), expected_token):
        return False, "invalid_bearer_token"

    origin_header = headers.get("origin")
    if origin_header is not None:
        ok = is_allowed_ws_origin(origin_header, transport.allowed_origins)
        return (ok, "origin_verified" if ok else "disallowed_origin")

    if expected_token or transport.effective_allow_missing_origin:
        return True, "non_browser_verified"
    return False, "missing_origin"


def validate_agy_relative_dir(raw_dir: str, *, flag_name: str) -> str:
    """Validate that an ``agy`` app-data directory flag is non-empty, relative, & safe."""
    cleaned = raw_dir.strip()
    if not cleaned:
        raise AcpConfigError(f"{flag_name} cannot be empty.")
    path_obj = Path(cleaned)
    if path_obj.is_absolute():
        raise AcpConfigError(
            f"{flag_name} must be relative to the Gemini root (got absolute path '{cleaned}').",
            context={"flag": flag_name, "value": cleaned},
        )
    if ".." in path_obj.parts:
        raise AcpConfigError(
            f"{flag_name} cannot contain '..' traversal segments: '{cleaned}'.",
            context={"flag": flag_name, "value": cleaned},
        )
    return cleaned


def validate_agy_gemini_dir(raw_dir: str) -> str:
    """Validate that ``--agy-gemini-dir`` is a non-empty, traversal-free absolute path."""
    cleaned = raw_dir.strip()
    if not cleaned:
        raise AcpConfigError("--agy-gemini-dir cannot be empty.")
    raw_path = Path(cleaned)
    if ".." in raw_path.parts:
        raise AcpConfigError(
            f"--agy-gemini-dir cannot contain '..' traversal segments: '{cleaned}'.",
            context={"flag": "--agy-gemini-dir", "value": cleaned},
        )
    expanded = raw_path.expanduser()
    if not expanded.is_absolute():
        raise AcpConfigError(
            f"--agy-gemini-dir must be an absolute path (got '{cleaned}').",
            context={"flag": "--agy-gemini-dir", "value": cleaned},
        )
    return str(expanded)


def resolve_agy_profile_root(config: AgentConfig) -> Path:
    """Resolve the on-disk ``<gemini_dir>/<app_data_dir>`` profile directory for ``agy``."""
    gemini_root = (
        Path(validate_agy_gemini_dir(config.agy_gemini_dir))
        if config.agy_gemini_dir is not None
        else Path.home() / ".gemini"
    )
    app_dir = validate_agy_relative_dir(
        config.agy_app_data_dir or DEFAULT_AGY_APP_DATA_DIR,
        flag_name="--agy-app-data-dir",
    )
    return gemini_root / app_dir


def format_agy_profile_display(config: AgentConfig) -> str:
    """Format a compact profile path label (e.g. ``~/.gemini/antigravity``)."""
    app_dir = validate_agy_relative_dir(
        config.agy_app_data_dir or DEFAULT_AGY_APP_DATA_DIR,
        flag_name="--agy-app-data-dir",
    )
    if config.agy_gemini_dir is None:
        return f"~/.gemini/{app_dir}"
    gemini_dir = validate_agy_gemini_dir(config.agy_gemini_dir).rstrip("/")
    return f"{gemini_dir}/{app_dir}"


def build_agent_command(config: AgentConfig) -> tuple[str, ...]:
    """Build argument vector for launching the configured ACP stdio agent without shell."""
    if not config.command:
        raise AcpConfigError("ACP agent command cannot be empty.")
    cmd = list(config.command)
    if Path(cmd[0]).name != "agy":
        return tuple(cmd)
    gemini_dir = (
        validate_agy_gemini_dir(config.agy_gemini_dir)
        if config.agy_gemini_dir is not None
        else None
    )
    app_data_dir = (
        validate_agy_relative_dir(config.agy_app_data_dir, flag_name="--agy-app-data-dir")
        if config.agy_app_data_dir is not None
        else None
    )
    if config.agy_sandbox and "--sandbox" not in cmd:
        cmd.append("--sandbox")
    if gemini_dir is not None and "--gemini-dir" not in cmd:
        cmd.extend(["--gemini-dir", gemini_dir])
    if app_data_dir is not None and "--app-data-dir" not in cmd:
        cmd.extend(["--app-data-dir", app_data_dir])
    return tuple(cmd)


def build_sanitized_agent_env(
    extra_allowlist: Sequence[str] = (),
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an allowlisted environment mapping for the ACP stdio subprocess."""
    source = os.environ if base_env is None else base_env
    extra = set(extra_allowlist)
    return {
        key: value
        for key, value in source.items()
        if key in SAFE_ENV_KEYS or key.startswith(SAFE_ENV_PREFIXES) or key in extra
    }


def sanitize_meta_dict(meta: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Strip credentials from ``_meta`` while preserving W3C ``traceparent``/``tracestate``."""
    if not meta:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in meta.items():
        if not isinstance(key, str) or key.lower() in FORBIDDEN_SECRET_KEYS:
            continue
        nested = sanitize_meta_dict(value) if isinstance(value, Mapping) else value
        if nested is not None or not isinstance(value, Mapping):
            cleaned[key] = nested
    return cleaned or None


def extract_traceparent(meta: object) -> str | None:
    """Extract safe bounded W3C ``traceparent`` correlation header from ``_meta``."""
    if not isinstance(meta, Mapping):
        return None
    raw = meta.get("traceparent")
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > 128 or _CONTROL_CHAR_RE.search(cleaned):
        return None
    return cleaned


def _negotiate_elicitation_caps(
    raw: ClientCapabilities,
    policy: CapabilityPolicyConfig,
) -> ElicitationCapabilities | None:
    """Filter client elicitation capabilities according to policy (preserving explicitness)."""
    if raw.elicitation is None:
        return None
    form = (
        raw.elicitation.form
        if policy.allow_elicitation_form and raw.elicitation.form is not None
        else None
    )
    url = (
        raw.elicitation.url
        if policy.allow_elicitation_url and raw.elicitation.url is not None
        else None
    )
    return ElicitationCapabilities(
        form=ElicitationFormCapabilities(_meta=sanitize_meta_dict(form.meta)) if form else None,
        url=ElicitationUrlCapabilities(_meta=sanitize_meta_dict(url.meta)) if url else None,
        _meta=sanitize_meta_dict(raw.elicitation.meta),
    )


def _negotiate_session_caps(
    raw: ClientCapabilities,
    policy: CapabilityPolicyConfig,
) -> ClientSessionCapabilities | None:
    """Filter client ``session.*`` capabilities according to policy."""
    if raw.session is None:
        return None
    notices = (
        NoticeCapabilities(_meta=sanitize_meta_dict(raw.session.notices.meta))
        if policy.allow_session_notices and raw.session.notices is not None
        else None
    )
    compaction = (
        CompactionCapabilities(_meta=sanitize_meta_dict(raw.session.compaction.meta))
        if policy.allow_session_compaction and raw.session.compaction is not None
        else None
    )
    cfg_opts = raw.session.config_options
    negotiated_cfg = (
        SessionConfigOptionsCapabilities(
            boolean=BooleanConfigOptionCapabilities(
                _meta=sanitize_meta_dict(cfg_opts.boolean.meta)
            )
            if policy.allow_boolean_config_options and cfg_opts.boolean is not None
            else None,
            _meta=sanitize_meta_dict(cfg_opts.meta),
        )
        if cfg_opts is not None
        else None
    )
    return ClientSessionCapabilities(
        notices=notices,
        compaction=compaction,
        configOptions=negotiated_cfg,
        _meta=sanitize_meta_dict(raw.session.meta),
    )


def _build_ws_client_capabilities(
    raw: ClientCapabilities,
    policy: CapabilityPolicyConfig,
) -> ClientCapabilities:
    """Compute effective capabilities supported by the connected WebSocket client."""
    auth_term = bool(
        raw.auth is not None
        and raw.auth.terminal
        and policy.auth_terminal == RelayOnlyPolicy.RELAY
    )
    read_file = bool(
        raw.fs is not None
        and raw.fs.read_text_file
        and policy.fs_read == CapabilityPolicy.RELAY
    )
    write_file = bool(
        raw.fs is not None
        and raw.fs.write_text_file
        and policy.fs_write == CapabilityPolicy.RELAY
    )
    terminal_relay = bool(raw.terminal and policy.terminal == CapabilityPolicy.RELAY)
    plan = (
        PlanCapabilities(_meta=sanitize_meta_dict(raw.plan.meta))
        if policy.allow_plan_operations and raw.plan is not None
        else None
    )
    subagents = (
        SubagentCapabilities(_meta=sanitize_meta_dict(raw.subagents.meta))
        if policy.allow_subagents and raw.subagents is not None
        else None
    )
    return ClientCapabilities(
        auth=ClientAuthCapabilities(terminal=auth_term),
        fs=ClientFsCapabilities(readTextFile=read_file, writeTextFile=write_file),
        terminal=terminal_relay,
        elicitation=_negotiate_elicitation_caps(raw, policy),
        plan=plan,
        session=_negotiate_session_caps(raw, policy),
        subagents=subagents,
        _meta=sanitize_meta_dict(raw.meta),
    )


def _serialize_client_caps_for_agent(agent_caps: ClientCapabilities) -> dict[str, Any]:
    """Serialize ``agent_facing_caps`` preserving explicit ``{}`` capability objects."""
    return agent_caps.model_dump(by_alias=True, exclude_none=True)


def negotiate_client_capabilities(
    params: InitializeParams,
    policy: CapabilityPolicyConfig,
) -> tuple[dict[str, Any], ClientCapabilities, ClientCapabilities]:
    """Compute ``(outbound_init_params, agent_facing_caps, ws_client_caps)`` from policy."""
    ws_caps = _build_ws_client_capabilities(params.client_capabilities, policy)
    agent_fs = ClientFsCapabilities(
        readTextFile=bool(
            policy.fs_read == CapabilityPolicy.HOST
            or (ws_caps.fs is not None and ws_caps.fs.read_text_file)
        ),
        writeTextFile=bool(
            policy.fs_write == CapabilityPolicy.HOST
            or (ws_caps.fs is not None and ws_caps.fs.write_text_file)
        ),
    )
    agent_caps = ws_caps.model_copy(
        update={
            "fs": agent_fs,
            "terminal": bool(policy.terminal == CapabilityPolicy.HOST or ws_caps.terminal),
        }
    )
    outbound: dict[str, Any] = {
        "protocolVersion": min(params.protocol_version, LATEST_ACP_VERSION),
        "clientCapabilities": _serialize_client_caps_for_agent(agent_caps),
    }
    if params.client_info is not None:
        outbound["clientInfo"] = params.client_info.model_dump(by_alias=True, exclude_none=True)
    scrubbed_meta = sanitize_meta_dict(params.meta)
    if scrubbed_meta is not None:
        outbound["_meta"] = scrubbed_meta
    return outbound, agent_caps, ws_caps


def _collect_child_lineage(
    session_id: str,
    root_sets: Mapping[str, SessionRootSet],
    child_sessions: Mapping[str, SubagentSessionState],
) -> tuple[SessionRootSet | None, list[SubagentSessionState]]:
    """Walk subagent parent links up to the owning root session's ``SessionRootSet``."""
    chain: list[SubagentSessionState] = []
    cursor: str | None = session_id
    visited: set[str] = set()
    while cursor is not None and cursor not in visited:
        visited.add(cursor)
        parent_root = root_sets.get(cursor)
        if parent_root is not None:
            return parent_root, chain
        child = child_sessions.get(cursor)
        if child is None:
            return None, []
        chain.append(child)
        cursor = child.parent_session_id
    return None, []


def resolve_effective_root_set(
    session_id: str,
    root_sets: Mapping[str, SessionRootSet],
    child_sessions: Mapping[str, SubagentSessionState],
) -> SessionRootSet | None:
    """Resolve effective ``[cwd, ...additionalDirectories]`` with independent child inheritance."""
    root_parent, chain = _collect_child_lineage(session_id, root_sets, child_sessions)
    if root_parent is None:
        return None
    eff_cwd = root_parent.cwd
    eff_dirs = root_parent.additional_directories
    for node in reversed(chain):
        if node.cwd is not None:
            eff_cwd = node.cwd
        if node.additional_directories is not None:
            eff_dirs = node.additional_directories
    return SessionRootSet(cwd=eff_cwd, additional_directories=eff_dirs)


def _resolve_candidate_for_containment(raw_path: str, *, allow_missing_leaf: bool) -> Path:
    """Resolve ``raw_path`` (or nearest existing ancestor when creating a file) safely."""
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise AcpSecurityError(f"Path must be absolute: {raw_path!r}")
    if candidate.exists() or candidate.is_symlink() or not allow_missing_leaf:
        return candidate.resolve()
    missing_parts: list[str] = []
    probe = candidate
    while not probe.exists() and not probe.is_symlink() and probe != probe.parent:
        missing_parts.append(probe.name)
        probe = probe.parent
    resolved_base = probe.resolve()
    for part in reversed(missing_parts):
        resolved_base = resolved_base / part
    return resolved_base


def validate_path_in_root_set(
    raw_path: str,
    root_set: SessionRootSet | None,
    *,
    allow_missing_leaf: bool = False,
) -> Path:
    """Verify ``raw_path`` stays within ``root_set`` at time-of-use (no symlink escapes)."""
    if root_set is None:
        raise AcpSecurityError("No active workspace root set for session")
    resolved = _resolve_candidate_for_containment(raw_path, allow_missing_leaf=allow_missing_leaf)
    allowed_roots = [Path(root_set.cwd).resolve()]
    allowed_roots.extend(Path(extra).resolve() for extra in root_set.additional_directories)
    for root in allowed_roots:
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    raise AcpSecurityError(
        "Requested path is outside the session workspace root set",
        wire_data={"path": raw_path},
    )


def is_sensitive_write_path(resolved_path: Path, root_set: SessionRootSet) -> bool:
    """Return ``True`` when writing ``resolved_path`` targets hooks, IDE/agent, or MCP config."""
    name = resolved_path.name
    if name in SENSITIVE_FILE_NAMES:
        return True
    parts = set(resolved_path.parts)
    if parts & SENSITIVE_DIR_NAMES:
        return True
    if ".git" in resolved_path.parts:
        git_idx = resolved_path.parts.index(".git")
        tail = resolved_path.parts[git_idx + 1 :]
        if not tail or tail[0] in ("hooks", "config", "HEAD"):
            return True
    del root_set
    return False


def validate_terminal_env(
    env_vars: Sequence[EnvVariable],
    *,
    allow_dangerous_env: bool = False,
) -> dict[str, str]:
    """Validate and convert ``EnvVariable`` list into a subprocess environment override dict."""
    overrides: dict[str, str] = {}
    for entry in env_vars:
        key = entry.name
        if "=" in key or "\x00" in key or "\x00" in entry.value:
            raise AcpSecurityError("Terminal environment entry contains invalid characters")
        upper = key.upper()
        if not allow_dangerous_env and (
            upper in DANGEROUS_TERMINAL_ENV_KEYS
            or upper.startswith(DANGEROUS_TERMINAL_ENV_PREFIXES)
        ):
            raise AcpSecurityError(f"Blocked sensitive terminal environment variable: '{key}'")
        overrides[key] = entry.value
    return overrides


def validate_terminal_command(
    command: str,
    *,
    allowed_commands: Sequence[str],
    allow_unrestricted: bool = False,
) -> None:
    """Enforce ``terminal_allowed_commands`` allowlist unless unrestricted opt-in is active."""
    cleaned = command.strip()
    if not cleaned or "\x00" in cleaned:
        raise AcpSecurityError("Terminal command cannot be empty or contain NUL bytes")
    if allow_unrestricted:
        return
    allowed_set = set(allowed_commands)
    base = Path(cleaned).name
    if cleaned not in allowed_set and base not in allowed_set:
        raise AcpSecurityError(f"Terminal command '{cleaned}' is not in terminal_allowed_commands")


def validate_elicitation_url(raw_url: str, *, allow_http: bool = False) -> str:
    """Verify URL-mode elicitation target uses safe scheme and carries no embedded creds."""
    cleaned = raw_url.strip()
    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise AcpSecurityError(f"Malformed elicitation URL: {exc}") from exc
    scheme = parsed.scheme.lower()
    allowed_schemes = ("https", "http") if allow_http else ("https",)
    if scheme not in allowed_schemes or not parsed.netloc:
        raise AcpSecurityError("Elicitation URL must use an allowed HTTPS scheme and host")
    if parsed.username or parsed.password:
        raise AcpSecurityError("Elicitation URL must not embed credentials")
    return cleaned


def find_advertised_auth_method(
    method_id: str,
    auth_methods: Sequence[AuthMethod],
) -> AuthMethod | None:
    """Locate ``method_id`` among ``authMethods`` advertised during ``initialize``."""
    for method in auth_methods:
        if method.id == method_id:
            return method
    return None


def sanitize_authenticate_params(params: AuthenticateParams) -> dict[str, Any]:
    """Scrub ``authenticate`` ``_meta`` while allowing ``apiKey`` for ``gemini-api-key``."""
    scrubbed_meta = sanitize_meta_dict(params.meta) or {}
    if params.method_id == "gemini-api-key" and params.meta is not None:
        raw_key = params.meta.get("apiKey")
        if isinstance(raw_key, str) and raw_key.strip():
            scrubbed_meta["apiKey"] = raw_key.strip()
    sanitized: dict[str, Any] = {"methodId": params.method_id}
    if scrubbed_meta:
        sanitized["_meta"] = scrubbed_meta
    return sanitized


__all__ = [
    "DANGEROUS_TERMINAL_ENV_KEYS",
    "DANGEROUS_TERMINAL_ENV_PREFIXES",
    "FORBIDDEN_META_KEYS",
    "SAFE_ENV_KEYS",
    "SAFE_ENV_PREFIXES",
    "build_agent_command",
    "build_sanitized_agent_env",
    "extract_host_name",
    "extract_traceparent",
    "find_advertised_auth_method",
    "format_agy_profile_display",
    "is_allowed_ws_host",
    "is_allowed_ws_origin",
    "is_loopback_host",
    "is_sensitive_write_path",
    "negotiate_client_capabilities",
    "normalize_origin",
    "resolve_agy_profile_root",
    "resolve_effective_root_set",
    "sanitize_authenticate_params",
    "sanitize_meta_dict",
    "validate_agy_gemini_dir",
    "validate_agy_relative_dir",
    "validate_bind_host",
    "validate_elicitation_url",
    "validate_path_in_root_set",
    "validate_terminal_command",
    "validate_terminal_env",
    "verify_ws_upgrade",
]
