"""ACP v1 agent-to-client elicitation, filesystem, and terminal wire models."""

from typing import Any, Literal

from pydantic import ConfigDict, Field, StrictBool, StrictInt, model_validator

from api.models.session import EnvVariable
from core.models.base import FrozenModel

_CLIENT_OPS_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class EnumOption(FrozenModel):
    """Titled option entry for single/multi-select elicitation properties."""

    model_config = _CLIENT_OPS_CONFIG

    const: str
    title: str


class ElicitationSchema(FrozenModel):
    """Restricted flat object JSON Schema for ``elicitation/create`` form mode."""

    model_config = _CLIENT_OPS_CONFIG

    type: Literal["object"] = "object"
    title: str | None = None
    description: str | None = None
    properties: dict[str, dict[str, Any]] = Field(default_factory=dict)
    required: tuple[str, ...] | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class CreateElicitationParams(FrozenModel):
    """Validated parameters for an agent-to-client ``elicitation/create`` request."""

    model_config = _CLIENT_OPS_CONFIG

    mode: str = Field(min_length=1)
    message: str = Field(min_length=1)
    session_id: str | None = Field(default=None, alias="sessionId", min_length=1)
    tool_call_id: str | None = Field(default=None, alias="toolCallId", min_length=1)
    request_id: StrictInt | str | None = Field(default=None, alias="requestId")
    requested_schema: ElicitationSchema | None = Field(default=None, alias="requestedSchema")
    elicitation_id: str | None = Field(default=None, alias="elicitationId", min_length=1)
    url: str | None = Field(default=None, min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")

    @model_validator(mode="after")
    def _validate_scope_and_mode_fields(self) -> "CreateElicitationParams":
        """Enforce sessionId XOR requestId scope and mode-required fields."""
        has_session = self.session_id is not None
        has_request = self.request_id is not None
        if has_session == has_request:
            raise ValueError("Elicitation must specify exactly one of 'sessionId' or 'requestId'")
        if self.tool_call_id is not None and not has_session:
            raise ValueError("'toolCallId' is only valid on session-scoped elicitations")
        if self.mode == "form" and self.requested_schema is None:
            raise ValueError("Form elicitation requires 'requestedSchema'")
        if self.mode == "url" and (not self.elicitation_id or not self.url):
            raise ValueError("URL elicitation requires 'elicitationId' and 'url'")
        return self


class CreateElicitationResponse(FrozenModel):
    """Validated client result payload answering ``elicitation/create``."""

    model_config = _CLIENT_OPS_CONFIG

    action: str = Field(min_length=1)
    content: dict[str, Any] | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class CompleteElicitationParams(FrozenModel):
    """Validated parameters for ``elicitation/complete`` notification."""

    model_config = _CLIENT_OPS_CONFIG

    elicitation_id: str = Field(alias="elicitationId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class PendingElicitationRequest(FrozenModel):
    """Tracked outstanding ``elicitation/create`` request awaiting client response."""

    request_id: StrictInt | str
    mode: str
    session_id: str | None = None
    elicitation_id: str | None = None
    requested_schema: ElicitationSchema | None = None


class ReadTextFileParams(FrozenModel):
    """Validated parameters for ``fs/read_text_file``."""

    model_config = _CLIENT_OPS_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    path: str = Field(min_length=1)
    line: StrictInt | None = Field(default=None, ge=0)
    limit: StrictInt | None = Field(default=None, ge=0)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ReadTextFileResult(FrozenModel):
    """Validated response payload for ``fs/read_text_file``."""

    model_config = _CLIENT_OPS_CONFIG

    content: str
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class WriteTextFileParams(FrozenModel):
    """Validated parameters for ``fs/write_text_file``."""

    model_config = _CLIENT_OPS_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    path: str = Field(min_length=1)
    content: str
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class WriteTextFileResult(FrozenModel):
    """Validated response payload for ``fs/write_text_file``."""

    model_config = _CLIENT_OPS_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class CreateTerminalParams(FrozenModel):
    """Validated parameters for ``terminal/create``."""

    model_config = _CLIENT_OPS_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    env: tuple[EnvVariable, ...] = ()
    cwd: str | None = None
    output_byte_limit: StrictInt | None = Field(default=None, alias="outputByteLimit", ge=0)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class CreateTerminalResult(FrozenModel):
    """Validated response payload for ``terminal/create``."""

    model_config = _CLIENT_OPS_CONFIG

    terminal_id: str = Field(alias="terminalId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class TerminalExitStatus(FrozenModel):
    """Process termination exit code and optional POSIX signal name."""

    model_config = _CLIENT_OPS_CONFIG

    exit_code: StrictInt | None = Field(default=None, alias="exitCode", ge=0)
    signal: str | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class TerminalOutputParams(FrozenModel):
    """Validated parameters for ``terminal/output``."""

    model_config = _CLIENT_OPS_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    terminal_id: str = Field(alias="terminalId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class TerminalOutputResult(FrozenModel):
    """Validated response payload for ``terminal/output``."""

    model_config = _CLIENT_OPS_CONFIG

    output: str
    truncated: StrictBool
    exit_status: TerminalExitStatus | None = Field(default=None, alias="exitStatus")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class WaitForTerminalExitParams(FrozenModel):
    """Validated parameters for ``terminal/wait_for_exit``."""

    model_config = _CLIENT_OPS_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    terminal_id: str = Field(alias="terminalId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class WaitForTerminalExitResult(FrozenModel):
    """Validated response payload for ``terminal/wait_for_exit``."""

    model_config = _CLIENT_OPS_CONFIG

    exit_code: StrictInt | None = Field(default=None, alias="exitCode", ge=0)
    signal: str | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class KillTerminalParams(FrozenModel):
    """Validated parameters for ``terminal/kill``."""

    model_config = _CLIENT_OPS_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    terminal_id: str = Field(alias="terminalId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class KillTerminalResult(FrozenModel):
    """Validated response payload for ``terminal/kill``."""

    model_config = _CLIENT_OPS_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ReleaseTerminalParams(FrozenModel):
    """Validated parameters for ``terminal/release``."""

    model_config = _CLIENT_OPS_CONFIG

    session_id: str = Field(alias="sessionId", min_length=1)
    terminal_id: str = Field(alias="terminalId", min_length=1)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ReleaseTerminalResult(FrozenModel):
    """Validated response payload for ``terminal/release``."""

    model_config = _CLIENT_OPS_CONFIG

    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


__all__ = [
    "CompleteElicitationParams",
    "CreateElicitationParams",
    "CreateElicitationResponse",
    "CreateTerminalParams",
    "CreateTerminalResult",
    "ElicitationSchema",
    "EnumOption",
    "KillTerminalParams",
    "KillTerminalResult",
    "PendingElicitationRequest",
    "ReadTextFileParams",
    "ReadTextFileResult",
    "ReleaseTerminalParams",
    "ReleaseTerminalResult",
    "TerminalExitStatus",
    "TerminalOutputParams",
    "TerminalOutputResult",
    "WaitForTerminalExitParams",
    "WaitForTerminalExitResult",
    "WriteTextFileParams",
    "WriteTextFileResult",
]
