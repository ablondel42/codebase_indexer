"""ACP v1 session setup, discovery, lifecycle, and deletion wire models."""

from typing import Any, Literal

from pydantic import ConfigDict, Field

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


__all__ = [
    "SessionArtifactContent",
    "SessionArtifactList",
    "SessionArtifactSummary",
    "SessionCloseParams",
    "SessionDeleteParams",
    "SessionListParams",
    "SessionLoadParams",
    "SessionNewParams",
    "SessionResumeParams",
    "SystemPromptConfig",
    "SystemPromptTextBlock",
]
