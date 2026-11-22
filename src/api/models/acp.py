"""ACP v1 initialization, capability negotiation, and authentication wire models."""

from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, StrictInt

from api.models.jsonrpc import (
    ACP_AUTH_REQUIRED,
    ACP_REQUEST_CANCELLED,
    ACP_RESOURCE_NOT_FOUND,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    JsonRpcErrorObject,
    JsonRpcErrorResponse,
    JsonRpcSuccessResponse,
    SessionStepOutcome,
)
from core.models.base import FrozenModel

LATEST_ACP_VERSION: int = 1
SUPPORTED_ACP_VERSIONS: frozenset[int] = frozenset({1})

_WIRE_MODEL_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class AcpConnectionState(StrEnum):
    """Connection initialization lifecycle states enforced on every ``/acp`` socket."""

    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    READY = "ready"
    CLOSED = "closed"


class AcpAuthState(StrEnum):
    """Authentication state tracked across an initialized ACP connection."""

    UNKNOWN = "unknown"
    AUTHENTICATING = "authenticating"
    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"


class ImplementationInfo(FrozenModel):
    """Self-reported client or agent name and version metadata."""

    model_config = _WIRE_MODEL_CONFIG

    name: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=128)
    version: str = Field(min_length=1, max_length=64)


class ClientAuthCapabilities(FrozenModel):
    """Authentication capabilities declared by an ACP client."""

    model_config = _WIRE_MODEL_CONFIG

    terminal: bool = False
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ClientFsCapabilities(FrozenModel):
    """Client-side filesystem operation capabilities."""

    model_config = _WIRE_MODEL_CONFIG

    read_text_file: bool = Field(default=False, alias="readTextFile")
    write_text_file: bool = Field(default=False, alias="writeTextFile")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ElicitationFormCapabilities(FrozenModel):
    """Capability marker for form-based elicitation support."""

    model_config = _WIRE_MODEL_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ElicitationUrlCapabilities(FrozenModel):
    """Capability marker for URL-based elicitation support."""

    model_config = _WIRE_MODEL_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ElicitationCapabilities(FrozenModel):
    """Elicitation modes advertised by the client during initialization."""

    model_config = _WIRE_MODEL_CONFIG

    form: ElicitationFormCapabilities | None = None
    url: ElicitationUrlCapabilities | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class PlanCapabilities(FrozenModel):
    """Capability marker for ``plan_update`` and ``plan_removed`` session updates."""

    model_config = _WIRE_MODEL_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class CompactionCapabilities(FrozenModel):
    """Capability marker for ``compaction_update`` and ``compaction_summary_chunk``."""

    model_config = _WIRE_MODEL_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class NoticeCapabilities(FrozenModel):
    """Capability marker for live user-facing ``notice`` session updates."""

    model_config = _WIRE_MODEL_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SubagentCapabilities(FrozenModel):
    """Capability marker for ``subagent_update``, ``session_message``, and child rules."""

    model_config = _WIRE_MODEL_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class BooleanConfigOptionCapabilities(FrozenModel):
    """Capability marker for ``type: 'boolean'`` session configuration options."""

    model_config = _WIRE_MODEL_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionConfigOptionsCapabilities(FrozenModel):
    """Client config option extensions supported on session responses and updates."""

    model_config = _WIRE_MODEL_CONFIG

    boolean: BooleanConfigOptionCapabilities | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ClientSessionCapabilities(FrozenModel):
    """Session-scoped client capabilities advertised during ``initialize``."""

    model_config = _WIRE_MODEL_CONFIG

    notices: NoticeCapabilities | None = None
    compaction: CompactionCapabilities | None = None
    config_options: SessionConfigOptionsCapabilities | None = Field(
        default=None,
        alias="configOptions",
    )
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ClientCapabilities(FrozenModel):
    """Capabilities advertised by the client during ``initialize``."""

    model_config = _WIRE_MODEL_CONFIG

    auth: ClientAuthCapabilities | None = None
    fs: ClientFsCapabilities | None = None
    terminal: bool = False
    elicitation: ElicitationCapabilities | None = None
    plan: PlanCapabilities | None = None
    session: ClientSessionCapabilities | None = None
    subagents: SubagentCapabilities | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class InitializeParams(FrozenModel):
    """Validated request parameters for the ACP ``initialize`` handshake."""

    model_config = _WIRE_MODEL_CONFIG

    protocol_version: StrictInt = Field(alias="protocolVersion", ge=1)
    client_capabilities: ClientCapabilities = Field(
        default_factory=ClientCapabilities,
        alias="clientCapabilities",
    )
    client_info: ImplementationInfo | None = Field(default=None, alias="clientInfo")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class PromptCapabilities(FrozenModel):
    """Supported non-baseline content block types for ``session/prompt``."""

    model_config = _WIRE_MODEL_CONFIG

    image: bool = False
    audio: bool = False
    embedded_context: bool = Field(default=False, alias="embeddedContext")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class McpCapabilities(FrozenModel):
    """Supported MCP server transports advertised by an ACP agent."""

    model_config = _WIRE_MODEL_CONFIG

    http: bool = False
    sse: bool = False
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class AgentAuthCapabilities(FrozenModel):
    """Authentication-management capabilities advertised by an ACP agent."""

    model_config = _WIRE_MODEL_CONFIG

    logout: dict[str, Any] | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionSystemPromptOverrideCapabilities(FrozenModel):
    """Capability marker for ``systemPrompt.mode = 'override'``."""

    model_config = _WIRE_MODEL_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionSystemPromptModeCapabilities(FrozenModel):
    """Supported ``systemPrompt.mode`` variants beyond baseline ``append``."""

    model_config = _WIRE_MODEL_CONFIG

    override: SessionSystemPromptOverrideCapabilities | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionSystemPromptCapabilities(FrozenModel):
    """Agent ``sessionCapabilities.systemPrompt`` capability descriptor."""

    model_config = _WIRE_MODEL_CONFIG

    mode: SessionSystemPromptModeCapabilities | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionCapabilities(FrozenModel):
    """Optional session lifecycle capabilities advertised by an ACP agent."""

    model_config = _WIRE_MODEL_CONFIG

    delete: dict[str, Any] | None = None
    additional_directories: dict[str, Any] | None = Field(
        default=None,
        alias="additionalDirectories",
    )
    system_prompt: SessionSystemPromptCapabilities | None = Field(
        default=None,
        alias="systemPrompt",
    )
    resume: dict[str, Any] | None = None
    close: dict[str, Any] | None = None
    load: dict[str, Any] | None = None
    list: dict[str, Any] | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class AgentCapabilities(FrozenModel):
    """Capabilities returned by an ACP agent in ``initialize``."""

    model_config = _WIRE_MODEL_CONFIG

    load_session: bool = Field(default=False, alias="loadSession")
    prompt_capabilities: PromptCapabilities = Field(
        default_factory=PromptCapabilities,
        alias="promptCapabilities",
    )
    mcp_capabilities: McpCapabilities = Field(
        default_factory=McpCapabilities,
        alias="mcpCapabilities",
    )
    session_capabilities: SessionCapabilities = Field(
        default_factory=SessionCapabilities,
        alias="sessionCapabilities",
    )
    auth: AgentAuthCapabilities | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class AuthMethod(FrozenModel):
    """Authentication flow descriptor advertised during ``initialize`` (``agent``/``terminal``)."""

    model_config = _WIRE_MODEL_CONFIG

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    type: str = "agent"
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class InitializeResult(FrozenModel):
    """Negotiated ACP protocol version, capabilities, agent info, and auth methods."""

    model_config = _WIRE_MODEL_CONFIG

    protocol_version: StrictInt = Field(alias="protocolVersion", ge=1)
    agent_capabilities: AgentCapabilities = Field(
        default_factory=AgentCapabilities,
        alias="agentCapabilities",
    )
    agent_info: ImplementationInfo | None = Field(default=None, alias="agentInfo")
    auth_methods: tuple[AuthMethod, ...] = Field(default=(), alias="authMethods")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class AuthenticateParams(FrozenModel):
    """Validated parameters for the ACP ``authenticate`` request."""

    model_config = _WIRE_MODEL_CONFIG

    method_id: str = Field(alias="methodId", min_length=1, max_length=128)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class LogoutParams(FrozenModel):
    """Validated parameters for the ACP ``logout`` request."""

    model_config = _WIRE_MODEL_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


__all__ = [
    "ACP_AUTH_REQUIRED",
    "ACP_REQUEST_CANCELLED",
    "ACP_RESOURCE_NOT_FOUND",
    "AcpAuthState",
    "AcpConnectionState",
    "AgentAuthCapabilities",
    "AgentCapabilities",
    "AuthMethod",
    "AuthenticateParams",
    "BooleanConfigOptionCapabilities",
    "ClientAuthCapabilities",
    "ClientCapabilities",
    "ClientFsCapabilities",
    "ClientSessionCapabilities",
    "CompactionCapabilities",
    "ElicitationCapabilities",
    "ElicitationFormCapabilities",
    "ElicitationUrlCapabilities",
    "ImplementationInfo",
    "InitializeParams",
    "InitializeResult",
    "JSONRPC_INTERNAL_ERROR",
    "JSONRPC_INVALID_PARAMS",
    "JSONRPC_INVALID_REQUEST",
    "JSONRPC_METHOD_NOT_FOUND",
    "JSONRPC_PARSE_ERROR",
    "JsonRpcErrorObject",
    "JsonRpcErrorResponse",
    "JsonRpcSuccessResponse",
    "LATEST_ACP_VERSION",
    "LogoutParams",
    "McpCapabilities",
    "NoticeCapabilities",
    "PlanCapabilities",
    "PromptCapabilities",
    "SUPPORTED_ACP_VERSIONS",
    "SessionCapabilities",
    "SessionConfigOptionsCapabilities",
    "SessionStepOutcome",
    "SessionSystemPromptCapabilities",
    "SessionSystemPromptModeCapabilities",
    "SessionSystemPromptOverrideCapabilities",
    "SubagentCapabilities",
]
