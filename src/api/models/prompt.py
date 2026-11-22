"""ACP v1 prompt turn, content block, tool call, plan, compaction, notice, and subagent models."""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, StrictInt

from core.models.base import FrozenModel, MutableModel

_PROMPT_WIRE_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class StopReason(StrEnum):
    """Canonical ACP v1 reasons why an agent finishes a ``session/prompt`` turn."""

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"


class SessionUpdateKind(StrEnum):
    """Complete set of ACP v1 ``sessionUpdate`` discriminators for ``session/update``."""

    USER_MESSAGE_CHUNK = "user_message_chunk"
    AGENT_MESSAGE_CHUNK = "agent_message_chunk"
    AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
    SUBAGENT_UPDATE = "subagent_update"
    SESSION_MESSAGE = "session_message"
    SESSION_MESSAGE_CHUNK = "session_message_chunk"
    TOOL_CALL = "tool_call"
    TOOL_CALL_UPDATE = "tool_call_update"
    PLAN = "plan"
    PLAN_UPDATE = "plan_update"
    PLAN_REMOVED = "plan_removed"
    AVAILABLE_COMMANDS_UPDATE = "available_commands_update"
    CURRENT_MODE_UPDATE = "current_mode_update"
    CONFIG_OPTION_UPDATE = "config_option_update"
    SESSION_INFO_UPDATE = "session_info_update"
    USAGE_UPDATE = "usage_update"
    NOTICE = "notice"
    COMPACTION_UPDATE = "compaction_update"
    COMPACTION_SUMMARY_CHUNK = "compaction_summary_chunk"


class PermissionOptionKind(StrEnum):
    """Standard ACP v1 UI/scope hints for ``session/request_permission`` options."""

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    REJECT_ONCE = "reject_once"
    REJECT_ALWAYS = "reject_always"


class ToolKind(StrEnum):
    """Standard ACP v1 tool presentation categories."""

    READ = "read"
    EDIT = "edit"
    DELETE = "delete"
    MOVE = "move"
    SEARCH = "search"
    EXECUTE = "execute"
    THINK = "think"
    FETCH = "fetch"
    SWITCH_MODE = "switch_mode"
    OTHER = "other"


class ToolCallStatus(StrEnum):
    """Execution lifecycle statuses for tool calls."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class PlanEntryPriority(StrEnum):
    """Relative importance of an agent plan entry."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PlanEntryStatus(StrEnum):
    """Execution state of an agent plan entry."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class CompactionStatus(StrEnum):
    """Known ACP context-compaction lifecycle states."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NoticeSeverity(StrEnum):
    """Known presentation severity hints for live ``notice`` updates."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ContentAnnotations(FrozenModel):
    """Optional display and routing metadata attached to an ACP ``ContentBlock``."""

    model_config = _PROMPT_WIRE_CONFIG

    audience: tuple[Literal["user", "assistant"], ...] | None = None
    last_modified: str | None = Field(default=None, alias="lastModified")
    priority: float | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class TextContentBlock(FrozenModel):
    """Baseline plain-text or Markdown content block supported by all ACP agents."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["text"]
    text: str
    annotations: ContentAnnotations | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ImageContentBlock(FrozenModel):
    """Base64 image content block gated by ``promptCapabilities.image``."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["image"]
    data: str = Field(min_length=1)
    mime_type: str = Field(alias="mimeType", min_length=1)
    uri: str | None = None
    annotations: ContentAnnotations | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class AudioContentBlock(FrozenModel):
    """Base64 audio content block gated by ``promptCapabilities.audio``."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["audio"]
    data: str = Field(min_length=1)
    mime_type: str = Field(alias="mimeType", min_length=1)
    annotations: ContentAnnotations | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ResourceLinkContentBlock(FrozenModel):
    """Baseline URI reference content block supported by all ACP agents."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["resource_link"]
    uri: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mime_type: str | None = Field(default=None, alias="mimeType")
    title: str | None = None
    description: str | None = None
    size: int | None = Field(default=None, ge=0)
    annotations: ContentAnnotations | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class TextResourceContents(FrozenModel):
    """UTF-8 text resource payload embedded inside an ``EmbeddedResourceContentBlock``."""

    model_config = _PROMPT_WIRE_CONFIG

    uri: str = Field(min_length=1)
    text: str
    mime_type: str | None = Field(default=None, alias="mimeType")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class BlobResourceContents(FrozenModel):
    """Base64 binary resource payload embedded inside an ``EmbeddedResourceContentBlock``."""

    model_config = _PROMPT_WIRE_CONFIG

    uri: str = Field(min_length=1)
    blob: str = Field(min_length=1)
    mime_type: str | None = Field(default=None, alias="mimeType")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class EmbeddedResourceContentBlock(FrozenModel):
    """Embedded resource content block gated by ``promptCapabilities.embeddedContext``."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["resource"]
    resource: TextResourceContents | BlobResourceContents
    annotations: ContentAnnotations | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


PromptContentBlock = Annotated[
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceLinkContentBlock
    | EmbeddedResourceContentBlock,
    Field(discriminator="type"),
]


class ToolCallLocation(FrozenModel):
    """File location accessed or modified by a tool call."""

    model_config = _PROMPT_WIRE_CONFIG

    path: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=0)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ToolCallContentBlock(FrozenModel):
    """Standard ``ContentBlock`` embedded inside a tool call."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["content"]
    content: PromptContentBlock
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ToolCallDiffContent(FrozenModel):
    """File modification diff item inside a tool call."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["diff"]
    path: str = Field(min_length=1)
    old_text: str | None = Field(default=None, alias="oldText")
    new_text: str = Field(alias="newText")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ToolCallTerminalContent(FrozenModel):
    """Live terminal reference embedded inside a tool call."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["terminal"]
    terminal_id: str = Field(alias="terminalId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ToolCallSessionContent(FrozenModel):
    """Display-only reference linking a tool call to another session (subagent)."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["session"]
    session_id: str = Field(alias="sessionId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


ToolCallContentItem = Annotated[
    ToolCallContentBlock | ToolCallDiffContent | ToolCallTerminalContent | ToolCallSessionContent,
    Field(discriminator="type"),
]


class PlanEntry(FrozenModel):
    """Single task entry inside an agent execution plan."""

    model_config = _PROMPT_WIRE_CONFIG

    content: str
    priority: PlanEntryPriority | str
    status: PlanEntryStatus | str
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class PlanItems(FrozenModel):
    """Item-based identified plan payload for ``plan_update``."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["items"]
    plan_id: str = Field(alias="planId", min_length=1)
    entries: tuple[PlanEntry, ...] = ()
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class PlanMarkdown(FrozenModel):
    """Markdown-based identified plan payload for ``plan_update``."""

    model_config = _PROMPT_WIRE_CONFIG

    type: Literal["markdown"]
    plan_id: str = Field(alias="planId", min_length=1)
    content: str
    path: str | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class NoticeUpdate(FrozenModel):
    """Live user-facing ``notice`` session update (gated by ``session.notices``)."""

    model_config = _PROMPT_WIRE_CONFIG

    session_update: Literal["notice"] = Field(alias="sessionUpdate")
    severity: NoticeSeverity | str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class CompactionUpdate(FrozenModel):
    """ID-addressed ``compaction_update`` (gated by ``session.compaction``)."""

    model_config = _PROMPT_WIRE_CONFIG

    session_update: Literal["compaction_update"] = Field(alias="sessionUpdate")
    compaction_id: str = Field(alias="compactionId", min_length=1)
    status: CompactionStatus | str = Field(min_length=1)
    summary: tuple[PromptContentBlock, ...] | None = None
    error: str | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class CompactionSummaryChunk(FrozenModel):
    """Streamed summary chunk for an active compaction entity."""

    model_config = _PROMPT_WIRE_CONFIG

    session_update: Literal["compaction_summary_chunk"] = Field(alias="sessionUpdate")
    compaction_id: str = Field(alias="compactionId", min_length=1)
    content: PromptContentBlock
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionPromptParams(FrozenModel):
    """Validated parameters for ``session/prompt``."""

    model_config = _PROMPT_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    prompt: tuple[PromptContentBlock, ...] = Field(min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionPromptResult(FrozenModel):
    """Validated result payload returned when a ``session/prompt`` turn completes."""

    model_config = _PROMPT_WIRE_CONFIG

    stop_reason: StopReason | str = Field(alias="stopReason", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class SessionCancelParams(FrozenModel):
    """Validated parameters for the ``session/cancel`` notification."""

    model_config = _PROMPT_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class CancelRequestParams(FrozenModel):
    """Validated parameters for the ``$/cancel_request`` notification."""

    model_config = _PROMPT_WIRE_CONFIG

    request_id: StrictInt | str = Field(alias="requestId")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class PermissionOption(FrozenModel):
    """Single selectable option presented in ``session/request_permission``."""

    model_config = _PROMPT_WIRE_CONFIG

    option_id: str = Field(alias="optionId", min_length=1)
    name: str = Field(min_length=1)
    kind: PermissionOptionKind | str = Field(min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class RequestPermissionParams(FrozenModel):
    """Validated parameters for an agent-to-client ``session/request_permission`` call."""

    model_config = _PROMPT_WIRE_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    tool_call: dict[str, Any] = Field(alias="toolCall")
    options: tuple[PermissionOption, ...] = Field(min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class CancelledPermissionOutcome(FrozenModel):
    """Permission outcome indicating the prompt turn was cancelled."""

    model_config = _PROMPT_WIRE_CONFIG

    outcome: Literal["cancelled"]


class SelectedPermissionOutcome(FrozenModel):
    """Permission outcome selecting one advertised ``optionId``."""

    model_config = _PROMPT_WIRE_CONFIG

    outcome: Literal["selected"]
    option_id: str = Field(alias="optionId", min_length=1)


PermissionOutcome = Annotated[
    CancelledPermissionOutcome | SelectedPermissionOutcome,
    Field(discriminator="outcome"),
]


class RequestPermissionResult(FrozenModel):
    """Validated client ``result`` payload answering ``session/request_permission``."""

    model_config = _PROMPT_WIRE_CONFIG

    outcome: PermissionOutcome
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ActivePromptTurn(MutableModel):
    """Mutable bridge tracking record for an in-flight ``session/prompt`` turn."""

    request_id: StrictInt | str
    session_id: str
    cancel_requested: bool = False


class PendingPermissionRequest(FrozenModel):
    """Tracked agent-to-client ``session/request_permission`` awaiting user resolution."""

    request_id: StrictInt | str
    session_id: str
    option_ids: frozenset[str]


class PendingAgentRequest(FrozenModel):
    """Tracked in-flight agent-to-client request (relayed or host-executed)."""

    request_id: StrictInt | str
    method: str
    session_id: str | None = None
    started_at_mono: float = 0.0
    is_host_executed: bool = False


class SubagentSessionState(MutableModel):
    """Tracked subagent child session announced via ``subagent_update``."""

    session_id: str
    parent_session_id: str
    can_cancel: bool = False
    cwd: str | None = None
    additional_directories: tuple[str, ...] | None = None
    title: str | None = None
    description: str | None = None
    state: dict[str, Any] | None = None


class AgentFrameDecision(FrozenModel):
    """Bridge routing outcome after inspecting one outbound agent JSON-RPC frame."""

    forward_frame: str = ""
    agent_reply: dict[str, Any] | None = None


__all__ = [
    "ActivePromptTurn",
    "AgentFrameDecision",
    "AudioContentBlock",
    "BlobResourceContents",
    "CancelRequestParams",
    "CancelledPermissionOutcome",
    "CompactionStatus",
    "CompactionSummaryChunk",
    "CompactionUpdate",
    "ContentAnnotations",
    "EmbeddedResourceContentBlock",
    "ImageContentBlock",
    "NoticeSeverity",
    "NoticeUpdate",
    "PendingAgentRequest",
    "PendingPermissionRequest",
    "PermissionOption",
    "PermissionOptionKind",
    "PermissionOutcome",
    "PlanEntry",
    "PlanEntryPriority",
    "PlanEntryStatus",
    "PlanItems",
    "PlanMarkdown",
    "PromptContentBlock",
    "RequestPermissionParams",
    "RequestPermissionResult",
    "ResourceLinkContentBlock",
    "SelectedPermissionOutcome",
    "SessionCancelParams",
    "SessionPromptParams",
    "SessionPromptResult",
    "SessionUpdateKind",
    "StopReason",
    "SubagentSessionState",
    "TextContentBlock",
    "TextResourceContents",
    "ToolCallContentBlock",
    "ToolCallContentItem",
    "ToolCallDiffContent",
    "ToolCallLocation",
    "ToolCallSessionContent",
    "ToolCallStatus",
    "ToolCallTerminalContent",
    "ToolKind",
]
