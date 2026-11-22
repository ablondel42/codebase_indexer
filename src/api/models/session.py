"""ACP v1 session setup, MCP server descriptors, discovery, config options, and modes."""

from typing import Any, Literal

from pydantic import ConfigDict, Field, StrictBool

from core.models.base import FrozenModel

_SESSION_WIRE_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class EnvVariable(FrozenModel):
    """Named environment variable entry for stdio MCP servers and terminals."""

    model_config = _SESSION_WIRE_CONFIG

    name: str = Field(min_length=1)
    value: str
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class HttpHeader(FrozenModel):
    """HTTP header name/value entry for HTTP or SSE MCP servers."""

    model_config = _SESSION_WIRE_CONFIG

    name: str = Field(min_length=1)
    value: str
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class McpServerStdio(FrozenModel):
    """Stdio transport MCP server descriptor."""

    model_config = _SESSION_WIRE_CONFIG

    name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    env: tuple[EnvVariable, ...] = ()
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class McpServerHttp(FrozenModel):
    """Streamable HTTP transport MCP server descriptor."""

    model_config = _SESSION_WIRE_CONFIG

    type: Literal["http"]
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    headers: tuple[HttpHeader, ...] = ()
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class McpServerSse(FrozenModel):
    """Legacy SSE transport MCP server descriptor."""

    model_config = _SESSION_WIRE_CONFIG

    type: Literal["sse"]
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    headers: tuple[HttpHeader, ...] = ()
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SystemPromptTextBlock(FrozenModel):
    """Text content block permitted inside ACP ``systemPrompt.content``."""

    model_config = _SESSION_WIRE_CONFIG

    type: Literal["text"]
    text: str
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SystemPromptConfig(FrozenModel):
    """Client-supplied system prompt configuration for session setup methods."""

    model_config = _SESSION_WIRE_CONFIG

    mode: str = Field(min_length=1, max_length=64)
    content: tuple[SystemPromptTextBlock, ...] = Field(min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionRootSet(FrozenModel):
    """Effective filesystem workspace root set ``[cwd, ...additionalDirectories]``."""

    cwd: str = Field(min_length=1)
    additional_directories: tuple[str, ...] = ()


class SessionNewParams(FrozenModel):
    """Validated parameters for ``session/new``."""

    model_config = _SESSION_WIRE_CONFIG

    cwd: str = Field(min_length=1)
    mcp_servers: tuple[dict[str, Any], ...] = Field(default=(), alias="mcpServers")
    additional_directories: tuple[str, ...] | None = Field(
        default=None,
        alias="additionalDirectories",
    )
    system_prompt: SystemPromptConfig | None = Field(default=None, alias="systemPrompt")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionLoadParams(FrozenModel):
    """Validated parameters for ``session/load``."""

    model_config = _SESSION_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    cwd: str = Field(min_length=1)
    mcp_servers: tuple[dict[str, Any], ...] = Field(default=(), alias="mcpServers")
    additional_directories: tuple[str, ...] | None = Field(
        default=None,
        alias="additionalDirectories",
    )
    system_prompt: SystemPromptConfig | None = Field(default=None, alias="systemPrompt")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionResumeParams(FrozenModel):
    """Validated parameters for ``session/resume``."""

    model_config = _SESSION_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    cwd: str = Field(min_length=1)
    mcp_servers: tuple[dict[str, Any], ...] = Field(default=(), alias="mcpServers")
    additional_directories: tuple[str, ...] | None = Field(
        default=None,
        alias="additionalDirectories",
    )
    system_prompt: SystemPromptConfig | None = Field(default=None, alias="systemPrompt")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionListParams(FrozenModel):
    """Validated optional filter and pagination parameters for ``session/list``."""

    model_config = _SESSION_WIRE_CONFIG

    cwd: str | None = None
    cursor: str | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionInfo(FrozenModel):
    """Single session summary entry returned inside ``session/list``."""

    model_config = _SESSION_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    cwd: str = Field(min_length=1)
    additional_directories: tuple[str, ...] = Field(default=(), alias="additionalDirectories")
    title: str | None = None
    updated_at: str | None = Field(default=None, alias="updatedAt")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionListResult(FrozenModel):
    """Paginated response payload for ``session/list``."""

    model_config = _SESSION_WIRE_CONFIG

    sessions: tuple[SessionInfo, ...] = ()
    next_cursor: str | None = Field(default=None, alias="nextCursor")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionCloseParams(FrozenModel):
    """Validated parameters for ``session/close``."""

    model_config = _SESSION_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionDeleteParams(FrozenModel):
    """Validated parameters for ``session/delete``."""

    model_config = _SESSION_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionModelOption(FrozenModel):
    """Single model descriptor returned inside ``models.availableModels``."""

    model_config = _SESSION_WIRE_CONFIG

    model_id: str = Field(alias="modelId", min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionModelsState(FrozenModel):
    """Session model catalog and active ``currentModelId`` returned on session setup."""

    model_config = _SESSION_WIRE_CONFIG

    available_models: tuple[SessionModelOption, ...] = Field(
        default=(),
        alias="availableModels",
    )
    current_model_id: str | None = Field(default=None, alias="currentModelId")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionModeOption(FrozenModel):
    """Single operating mode descriptor returned inside ``modes.availableModes``."""

    model_config = _SESSION_WIRE_CONFIG

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionModesState(FrozenModel):
    """Available session operating modes and active ``currentModeId``."""

    model_config = _SESSION_WIRE_CONFIG

    current_mode_id: str = Field(alias="currentModeId", min_length=1)
    available_modes: tuple[SessionModeOption, ...] = Field(
        default=(),
        alias="availableModes",
    )
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionConfigSelectChoice(FrozenModel):
    """Selectable option item inside a ``type='select'`` ``SessionConfigOption``."""

    model_config = _SESSION_WIRE_CONFIG

    value: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionConfigSelectGroup(FrozenModel):
    """Named group of ``SessionConfigSelectChoice`` items for grouped selectors."""

    model_config = _SESSION_WIRE_CONFIG

    group: str = Field(min_length=1)
    name: str = Field(min_length=1)
    options: tuple[SessionConfigSelectChoice, ...] = ()
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionConfigOption(FrozenModel):
    """ACP v1 session configuration option descriptor (``select`` or ``boolean``)."""

    model_config = _SESSION_WIRE_CONFIG

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    category: str | None = None
    type: str = Field(min_length=1)
    current_value: StrictBool | str = Field(alias="currentValue")
    options: tuple[SessionConfigSelectChoice | SessionConfigSelectGroup, ...] = ()
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class AvailableCommandInput(FrozenModel):
    """Optional input hint specification for an advertised slash command."""

    model_config = _SESSION_WIRE_CONFIG

    hint: str = Field(min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class AvailableCommand(FrozenModel):
    """Slash command or skill advertised via ``available_commands_update``."""

    model_config = _SESSION_WIRE_CONFIG

    name: str = Field(min_length=1)
    description: str
    input: AvailableCommandInput | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionCostUsage(FrozenModel):
    """Cumulative session monetary or credit usage reported in ``usage_update``."""

    model_config = _SESSION_WIRE_CONFIG

    amount: float
    currency: str = Field(min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionUsageSnapshot(FrozenModel):
    """Context-window token counts and optional cumulative cost for a session."""

    model_config = _SESSION_WIRE_CONFIG

    used: int = Field(ge=0)
    size: int = Field(ge=0)
    cost: SessionCostUsage | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionSetConfigOptionParams(FrozenModel):
    """Validated parameters for ``session/set_config_option``."""

    model_config = _SESSION_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    config_id: str = Field(alias="configId", min_length=1)
    type: str | None = None
    value: StrictBool | str
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionSetConfigOptionResult(FrozenModel):
    """Validated response for ``session/set_config_option``."""

    model_config = _SESSION_WIRE_CONFIG

    config_options: tuple[SessionConfigOption, ...] = Field(
        default=(),
        alias="configOptions",
    )
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionSetModeParams(FrozenModel):
    """Validated parameters for ``session/set_mode``."""

    model_config = _SESSION_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    mode_id: str = Field(alias="modeId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionRuntimeState(FrozenModel):
    """Live per-session runtime metadata collected from setup responses and updates."""

    session_id: str
    title: str | None = None
    updated_at: str | None = None
    models: SessionModelsState | None = None
    modes: SessionModesState | None = None
    config_options: tuple[SessionConfigOption, ...] = ()
    available_commands: tuple[AvailableCommand, ...] = ()
    usage: SessionUsageSnapshot | None = None


__all__ = [
    "AvailableCommand",
    "AvailableCommandInput",
    "EnvVariable",
    "HttpHeader",
    "McpServerHttp",
    "McpServerSse",
    "McpServerStdio",
    "SessionCloseParams",
    "SessionConfigOption",
    "SessionConfigSelectChoice",
    "SessionConfigSelectGroup",
    "SessionCostUsage",
    "SessionDeleteParams",
    "SessionInfo",
    "SessionListParams",
    "SessionListResult",
    "SessionLoadParams",
    "SessionModeOption",
    "SessionModelOption",
    "SessionModelsState",
    "SessionModesState",
    "SessionNewParams",
    "SessionResumeParams",
    "SessionRootSet",
    "SessionRuntimeState",
    "SessionSetConfigOptionParams",
    "SessionSetConfigOptionResult",
    "SessionSetModeParams",
    "SessionUsageSnapshot",
    "SystemPromptConfig",
    "SystemPromptTextBlock",
]
