"""Stateless parsers, patchers, and validators for ACP per-session runtime metadata."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError as PydanticValidationError

from api.models.session import (
    AntigravityTierInfo,
    AvailableCommand,
    SessionConfigOption,
    SessionConfigSelectChoice,
    SessionConfigSelectGroup,
    SessionModelsState,
    SessionModesState,
    SessionRuntimeState,
    SessionSetConfigOptionParams,
    SessionSetModeParams,
    SessionUsageSnapshot,
)
from api.utils.security import sanitize_meta_dict
from core.utils.logging import get_logger

logger = get_logger("api.utils.session_state")


def sanitize_https_uri(raw_uri: str | None) -> str | None:
    """Return trimmed ``https://`` URL or ``None`` for non-HTTPS/malformed URIs."""
    if not raw_uri:
        return None
    cleaned = raw_uri.strip()
    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        logger.debug("Dropping malformed tier upgradeUri %r: %s", raw_uri, exc)
        return None
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return None
    return cleaned


def extract_antigravity_tier(raw_meta: object) -> AntigravityTierInfo | None:
    """Extract and sanitize ``_meta.antigravity.tier`` from an ACP result payload."""
    if not isinstance(raw_meta, Mapping):
        return None
    agy_meta = raw_meta.get("antigravity")
    raw_tier = agy_meta.get("tier") if isinstance(agy_meta, Mapping) else None
    if not isinstance(raw_tier, Mapping):
        return None
    try:
        parsed = AntigravityTierInfo.model_validate(raw_tier)
    except PydanticValidationError as exc:
        logger.debug("Ignoring malformed _meta.antigravity.tier: %s", exc)
        return None
    return parsed.model_copy(update={"upgrade_uri": sanitize_https_uri(parsed.upgrade_uri)})


def _parse_models_state(raw_models: object) -> SessionModelsState | None:
    """Parse optional ``models`` object from session setup responses."""
    if not isinstance(raw_models, Mapping):
        return None
    try:
        return SessionModelsState.model_validate(raw_models)
    except PydanticValidationError as exc:
        logger.debug("Ignoring malformed session models state: %s", exc)
        return None


def _parse_modes_state(raw_modes: object) -> SessionModesState | None:
    """Parse optional ``modes`` object from session setup responses."""
    if not isinstance(raw_modes, Mapping):
        return None
    try:
        return SessionModesState.model_validate(raw_modes)
    except PydanticValidationError as exc:
        logger.debug("Ignoring malformed session modes state: %s", exc)
        return None


def _parse_config_options(raw_options: object) -> tuple[SessionConfigOption, ...]:
    """Parse valid ``SessionConfigOption`` items while skipping malformed entries."""
    if not isinstance(raw_options, list):
        return ()
    items: list[SessionConfigOption] = []
    for entry in raw_options:
        if not isinstance(entry, Mapping):
            continue
        try:
            items.append(SessionConfigOption.model_validate(entry))
        except PydanticValidationError as exc:
            logger.debug("Skipping malformed SessionConfigOption: %s", exc)
    return tuple(items)


def _parse_available_commands(raw_commands: object) -> tuple[AvailableCommand, ...]:
    """Parse valid ``AvailableCommand`` entries from ``available_commands_update``."""
    if not isinstance(raw_commands, list):
        return ()
    commands: list[AvailableCommand] = []
    for entry in raw_commands:
        if not isinstance(entry, Mapping):
            continue
        try:
            commands.append(AvailableCommand.model_validate(entry))
        except PydanticValidationError as exc:
            logger.debug("Skipping malformed AvailableCommand: %s", exc)
    return tuple(commands)


def _parse_usage_snapshot(update: Mapping[str, Any]) -> SessionUsageSnapshot | None:
    """Parse ``used``, ``size``, and optional ``cost`` from ``usage_update``."""
    try:
        return SessionUsageSnapshot.model_validate(update)
    except PydanticValidationError as exc:
        logger.debug("Ignoring malformed usage_update payload: %s", exc)
        return None


def _sync_modes_with_options(
    modes: SessionModesState | None,
    options: tuple[SessionConfigOption, ...],
) -> SessionModesState | None:
    """Synchronize ``modes.current_mode_id`` when a ``mode`` config option updates."""
    if modes is None:
        return None
    for opt in options:
        if (opt.category == "mode" or opt.id == "mode") and isinstance(opt.current_value, str):
            return modes.model_copy(update={"current_mode_id": opt.current_value})
    return modes


def apply_session_setup_result(
    existing: SessionRuntimeState | None,
    session_id: str,
    result: Mapping[str, Any],
) -> SessionRuntimeState:
    """Merge ``session/new``, ``load``, or ``resume`` setup metadata into runtime state."""
    base = existing or SessionRuntimeState(session_id=session_id)
    models = _parse_models_state(result.get("models"))
    modes = _parse_modes_state(result.get("modes"))
    has_options = "configOptions" in result
    config_options = (
        _parse_config_options(result.get("configOptions"))
        if has_options
        else base.config_options
    )
    tier = extract_antigravity_tier(result.get("_meta")) or base.tier
    next_modes = _sync_modes_with_options(modes or base.modes, config_options)
    return base.model_copy(
        update={
            "models": models if models is not None else base.models,
            "modes": next_modes,
            "config_options": config_options,
            "tier": tier,
        }
    )


def apply_config_options_result(
    existing: SessionRuntimeState | None,
    session_id: str,
    result: Mapping[str, Any],
) -> SessionRuntimeState:
    """Apply refreshed ``configOptions`` returned from ``session/set_config_option``."""
    base = existing or SessionRuntimeState(session_id=session_id)
    if "configOptions" not in result:
        return base
    options = _parse_config_options(result.get("configOptions"))
    next_modes = _sync_modes_with_options(base.modes, options)
    return base.model_copy(update={"config_options": options, "modes": next_modes})


def apply_mode_change(
    existing: SessionRuntimeState | None,
    session_id: str,
    mode_id: str,
) -> SessionRuntimeState:
    """Update ``modes.current_mode_id`` and any matching ``mode`` config option."""
    base = existing or SessionRuntimeState(session_id=session_id)
    next_modes = (
        base.modes.model_copy(update={"current_mode_id": mode_id})
        if base.modes is not None
        else SessionModesState(currentModeId=mode_id)
    )
    updated_options = tuple(
        opt.model_copy(update={"current_value": mode_id})
        if (opt.category == "mode" or opt.id == "mode") and opt.type == "select"
        else opt
        for opt in base.config_options
    )
    return base.model_copy(update={"modes": next_modes, "config_options": updated_options})


def _apply_session_info_patch(
    base: SessionRuntimeState,
    update: Mapping[str, Any],
) -> SessionRuntimeState:
    """Apply ACP ``session_info_update`` patch semantics (``null`` clears, omit retains)."""
    changes: dict[str, Any] = {}
    if "title" in update:
        raw_title = update["title"]
        if raw_title is None or isinstance(raw_title, str):
            changes["title"] = raw_title
    if "updatedAt" in update:
        raw_updated = update["updatedAt"]
        if raw_updated is None or isinstance(raw_updated, str):
            changes["updated_at"] = raw_updated
    return base.model_copy(update=changes) if changes else base


def apply_session_update_to_state(
    existing: SessionRuntimeState | None,
    session_id: str,
    update: Mapping[str, Any],
) -> SessionRuntimeState | None:
    """Patch ``SessionRuntimeState`` from a ``session/update`` payload when applicable."""
    kind = update.get("sessionUpdate")
    base = existing or SessionRuntimeState(session_id=session_id)
    patched: SessionRuntimeState | None = existing

    if kind == "available_commands_update":
        commands = _parse_available_commands(update.get("availableCommands"))
        patched = base.model_copy(update={"available_commands": commands})
    elif kind == "usage_update":
        usage = _parse_usage_snapshot(update)
        if usage is not None:
            patched = base.model_copy(update={"usage": usage})
    elif kind == "config_option_update":
        patched = apply_config_options_result(existing, session_id, update)
    elif kind == "current_mode_update":
        raw_mode = update.get("currentModeId")
        if isinstance(raw_mode, str) and raw_mode:
            patched = apply_mode_change(existing, session_id, raw_mode)
    elif kind == "session_info_update":
        patched = _apply_session_info_patch(base, update)
    return patched


def _collect_select_values(option: SessionConfigOption) -> frozenset[str]:
    """Flatten allowed string values across flat or grouped ``SessionConfigOption`` choices."""
    allowed: set[str] = set()
    for item in option.options:
        if isinstance(item, SessionConfigSelectChoice):
            allowed.add(item.value)
        elif isinstance(item, SessionConfigSelectGroup):
            allowed.update(choice.value for choice in item.options)
    return frozenset(allowed)


def _validate_against_known_option(
    option: SessionConfigOption,
    value: bool | str,
    declared_type: str | None,
) -> None:
    """Verify ``value`` and optional ``type`` match ``option``'s advertised schema."""
    if option.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Config option '{option.id}' requires a boolean value")
        return
    if declared_type == "boolean" or isinstance(value, bool):
        raise ValueError(f"Config option '{option.id}' requires a string select value")
    allowed = _collect_select_values(option)
    if allowed and value not in allowed:
        raise ValueError(f"Unsupported value {value!r} for config option '{option.id}'")


def prepare_set_config_option_params(
    raw_params: object,
    state: SessionRuntimeState | None,
) -> tuple[str, dict[str, Any]]:
    """Validate ``session/set_config_option`` against ``configOptions`` and scrub ``_meta``."""
    if not isinstance(raw_params, Mapping):
        raise ValueError("Invalid session/set_config_option params: expected JSON object")
    parsed = SessionSetConfigOptionParams.model_validate(raw_params)
    if isinstance(parsed.value, str) and not parsed.value:
        raise ValueError("Config option value cannot be empty")

    if state is not None and state.config_options:
        matched = next((opt for opt in state.config_options if opt.id == parsed.config_id), None)
        if matched is None:
            raise ValueError(f"Unknown session configId: '{parsed.config_id}'")
        _validate_against_known_option(matched, parsed.value, parsed.type)

    payload: dict[str, Any] = {
        "sessionId": parsed.session_id,
        "configId": parsed.config_id,
        "value": parsed.value,
    }
    if isinstance(parsed.value, bool):
        payload["type"] = "boolean"
    elif parsed.type is not None:
        payload["type"] = parsed.type
    scrubbed_meta = sanitize_meta_dict(parsed.meta)
    if scrubbed_meta is not None:
        payload["_meta"] = scrubbed_meta
    return parsed.session_id, payload


def prepare_set_mode_params(
    raw_params: object,
    state: SessionRuntimeState | None,
) -> tuple[str, dict[str, Any]]:
    """Validate ``session/set_mode`` against advertised ``availableModes`` and scrub ``_meta``."""
    if not isinstance(raw_params, Mapping):
        raise ValueError("Invalid session/set_mode params: expected JSON object")
    parsed = SessionSetModeParams.model_validate(raw_params)
    if state is not None and state.modes is not None and state.modes.available_modes:
        allowed_ids = {mode.id for mode in state.modes.available_modes}
        if parsed.mode_id not in allowed_ids:
            raise ValueError(f"Unknown session modeId: '{parsed.mode_id}'")

    payload: dict[str, Any] = {
        "sessionId": parsed.session_id,
        "modeId": parsed.mode_id,
    }
    scrubbed_meta = sanitize_meta_dict(parsed.meta)
    if scrubbed_meta is not None:
        payload["_meta"] = scrubbed_meta
    return parsed.session_id, payload


__all__ = [
    "apply_config_options_result",
    "apply_mode_change",
    "apply_session_setup_result",
    "apply_session_update_to_state",
    "extract_antigravity_tier",
    "prepare_set_config_option_params",
    "prepare_set_mode_params",
    "sanitize_https_uri",
]
