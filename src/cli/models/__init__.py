"""Pydantic v2 session state and output envelope models for the CLI driving adapter."""

from cli.models.output import CliErrorDetail, CliErrorEnvelope, McpCallEnvelope
from cli.models.shell import ShellSessionState

__all__ = [
    "CliErrorDetail",
    "CliErrorEnvelope",
    "McpCallEnvelope",
    "ShellSessionState",
]
