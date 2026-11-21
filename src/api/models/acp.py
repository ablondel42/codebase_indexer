"""ACP v1 initialization, capability negotiation, and JSON-RPC 2.0 wire models."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import ConfigDict, Field, StrictInt

from core.models.base import FrozenModel

LATEST_ACP_VERSION: int = 1
SUPPORTED_ACP_VERSIONS: frozenset[int] = frozenset({1})

JSONRPC_PARSE_ERROR: int = -32700
JSONRPC_INVALID_REQUEST: int = -32600
JSONRPC_METHOD_NOT_FOUND: int = -32601
JSONRPC_INVALID_PARAMS: int = -32602
JSONRPC_INTERNAL_ERROR: int = -32603
ACP_REQUEST_CANCELLED: int = -32800
ACP_AUTH_REQUIRED: int = -32000
ACP_RESOURCE_NOT_FOUND: int = -32002

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


class ClientFsCapabilities(FrozenModel):
    """Client-side filesystem operation capabilities."""

    model_config = _WIRE_MODEL_CONFIG

    read_text_file: bool = Field(default=False, alias="readTextFile")
    write_text_file: bool = Field(default=False, alias="writeTextFile")


class ClientCapabilities(FrozenModel):
    """Capabilities advertised by the client during ``initialize``."""

    model_config = _WIRE_MODEL_CONFIG

    auth: ClientAuthCapabilities | None = None
    fs: ClientFsCapabilities | None = None
    terminal: bool = False
    elicitation: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    session: dict[str, Any] | None = None
    subagents: dict[str, Any] | None = None
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


class McpCapabilities(FrozenModel):
    """Supported MCP server transports advertised by an ACP agent."""

    model_config = _WIRE_MODEL_CONFIG

    http: bool = False
    sse: bool = False


class AgentAuthCapabilities(FrozenModel):
    """Authentication-management capabilities advertised by an ACP agent."""

    model_config = _WIRE_MODEL_CONFIG

    logout: dict[str, Any] | None = None


class SessionCapabilities(FrozenModel):
    """Optional session lifecycle capabilities advertised by an ACP agent."""

    model_config = _WIRE_MODEL_CONFIG

    delete: dict[str, Any] | None = None
    additional_directories: dict[str, Any] | None = Field(
        default=None,
        alias="additionalDirectories",
    )
    system_prompt: dict[str, Any] | None = Field(default=None, alias="systemPrompt")
    resume: dict[str, Any] | None = None
    close: dict[str, Any] | None = None
    load: dict[str, Any] | None = None
    list: dict[str, Any] | None = None


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
    """Authentication flow descriptor advertised during ``initialize``."""

    model_config = _WIRE_MODEL_CONFIG

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    type: str = "agent"
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None


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


class JsonRpcErrorObject(FrozenModel):
    """JSON-RPC 2.0 error payload."""

    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcErrorResponse(FrozenModel):
    """JSON-RPC 2.0 error envelope."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    error: JsonRpcErrorObject


class SessionStepOutcome(FrozenModel):
    """Outbound WebSocket frames and optional close directive from one session step."""

    frames: tuple[str, ...] = ()
    close_code: int | None = None


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
    "ClientAuthCapabilities",
    "ClientCapabilities",
    "ClientFsCapabilities",
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
    "LATEST_ACP_VERSION",
    "LogoutParams",
    "McpCapabilities",
    "PromptCapabilities",
    "SUPPORTED_ACP_VERSIONS",
    "SessionCapabilities",
    "SessionStepOutcome",
]
