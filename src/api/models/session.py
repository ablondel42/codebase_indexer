"""ACP v1 session setup, discovery, lifecycle, and deletion wire models."""

from typing import Any, Literal

from pydantic import ConfigDict, Field, StrictBool

from core.models.base import FrozenModel

_SESSION_WIRE_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class SystemPromptTextBlock(FrozenModel):
    """Text content block permitted inside ACP ``systemPrompt.content``."""

    type: Literal["text"]
    text: str


class SystemPromptConfig(FrozenModel):
    """Client-supplied system prompt configuration for session setup methods."""

    mode: str = Field(min_length=1, max_length=64)
    content: tuple[SystemPromptTextBlock, ...] = Field(min_length=1)


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


class SessionArtifactSummary(FrozenModel):
    """Metadata summary for a single markdown artifact in ``brain/<sessionId>/``."""

    name: str
    summary: str | None = None
    updated_at: str
    size_bytes: int = Field(ge=0)
    user_facing: bool = True


class SessionArtifactList(FrozenModel):
    """Read-only listing of markdown artifacts belonging to an ACP session."""

    session_id: str
    artifacts: tuple[SessionArtifactSummary, ...] = ()


class SessionArtifactContent(FrozenModel):
    """Read-only markdown content and metadata for a single session artifact."""

    session_id: str
    name: str
    summary: str | None = None
    updated_at: str
    size_bytes: int = Field(ge=0)
    content: str


class SessionModelOption(FrozenModel):
    """Single model descriptor returned inside ``models.availableModels``."""

    model_config = _SESSION_WIRE_CONFIG

    model_id: str = Field(alias="modelId", min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None


class SessionModelsState(FrozenModel):
    """Session model catalog and active ``currentModelId`` returned on session setup."""

    model_config = _SESSION_WIRE_CONFIG

    available_models: tuple[SessionModelOption, ...] = Field(
        default=(),
        alias="availableModels",
    )
    current_model_id: str | None = Field(default=None, alias="currentModelId")


class SessionModeOption(FrozenModel):
    """Single operating mode descriptor returned inside ``modes.availableModes``."""

    model_config = _SESSION_WIRE_CONFIG

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None


class SessionModesState(FrozenModel):
    """Available session operating modes and active ``currentModeId``."""

    model_config = _SESSION_WIRE_CONFIG

    current_mode_id: str = Field(alias="currentModeId", min_length=1)
    available_modes: tuple[SessionModeOption, ...] = Field(
        default=(),
        alias="availableModes",
    )


class SessionConfigSelectChoice(FrozenModel):
    """Selectable option item inside a ``type='select'`` ``SessionConfigOption``."""

    model_config = _SESSION_WIRE_CONFIG

    value: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None


class SessionConfigSelectGroup(FrozenModel):
    """Named group of ``SessionConfigSelectChoice`` items for grouped selectors."""

    model_config = _SESSION_WIRE_CONFIG

    group: str = Field(min_length=1)
    name: str = Field(min_length=1)
    options: tuple[SessionConfigSelectChoice, ...] = ()


class SessionConfigOption(FrozenModel):
    """ACP v1 session configuration option descriptor (``model``, ``thought_level``, ``mode``)."""

    model_config = _SESSION_WIRE_CONFIG

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    category: str | None = None
    type: str = Field(min_length=1)
    current_value: StrictBool | str = Field(alias="currentValue")
    options: tuple[SessionConfigSelectChoice | SessionConfigSelectGroup, ...] = ()


class AntigravityTierInfo(FrozenModel):
    """Account subscription tier metadata extracted from ``_meta.antigravity.tier``."""

    model_config = _SESSION_WIRE_CONFIG

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    upgrade_uri: str | None = Field(default=None, alias="upgradeUri")


class AvailableCommandInput(FrozenModel):
    """Optional input hint specification for an advertised slash command."""

    model_config = _SESSION_WIRE_CONFIG

    hint: str = Field(min_length=1)


class AvailableCommand(FrozenModel):
    """Slash command or skill advertised via ``available_commands_update``."""

    model_config = _SESSION_WIRE_CONFIG

    name: str = Field(min_length=1)
    description: str
    input: AvailableCommandInput | None = None


class SessionCostUsage(FrozenModel):
    """Cumulative session monetary or credit usage reported in ``usage_update``."""

    model_config = _SESSION_WIRE_CONFIG

    amount: float
    currency: str = Field(min_length=1)


class SessionUsageSnapshot(FrozenModel):
    """Context-window token counts and optional cumulative cost for a session."""

    model_config = _SESSION_WIRE_CONFIG

    used: int = Field(ge=0)
    size: int = Field(ge=0)
    cost: SessionCostUsage | None = None


class SessionSetConfigOptionParams(FrozenModel):
    """Validated parameters for ``session/set_config_option``."""

    model_config = _SESSION_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    config_id: str = Field(alias="configId", min_length=1)
    type: str | None = None
    value: StrictBool | str
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
    tier: AntigravityTierInfo | None = None


__all__ = [
    "AntigravityTierInfo",
    "AvailableCommand",
    "AvailableCommandInput",
    "SessionArtifactContent",
    "SessionArtifactList",
    "SessionArtifactSummary",
    "SessionCloseParams",
    "SessionConfigOption",
    "SessionConfigSelectChoice",
    "SessionConfigSelectGroup",
    "SessionCostUsage",
    "SessionDeleteParams",
    "SessionListParams",
    "SessionLoadParams",
    "SessionModeOption",
    "SessionModelsState",
    "SessionModesState",
    "SessionModelOption",
    "SessionNewParams",
    "SessionResumeParams",
    "SessionRuntimeState",
    "SessionSetConfigOptionParams",
    "SessionSetModeParams",
    "SessionUsageSnapshot",
    "SystemPromptConfig",
    "SystemPromptTextBlock",
]
