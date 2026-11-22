"""Stateless parsers, patchers, and validators for ACP session runtime metadata."""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from api.models.acp import ClientCapabilities
from api.models.session import (
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
from api.utils.errors import pydantic_error_to_acp_error
from api.utils.security import sanitize_meta_dict
from core.models.exceptions import AcpInvalidParamsError
from core.utils.logging import get_logger

logger = get_logger("api.utils.session_state")


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


def _sync_models_with_options(
    models: SessionModelsState | None,
    options: tuple[SessionConfigOption, ...],
) -> SessionModelsState | None:
    """Synchronize ``models.current_model_id`` when a ``model`` config option updates."""
    if models is None:
        return None
    for opt in options:
        if (opt.category == "model" or opt.id == "model") and isinstance(opt.current_value, str):
            return models.model_copy(update={"current_model_id": opt.current_value})
    return models


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
    next_models = (
        models if models is not None else _sync_models_with_options(base.models, config_options)
    )
    next_modes = _sync_modes_with_options(modes or base.modes, config_options)
    return base.model_copy(
        update={
            "models": next_models,
            "modes": next_modes,
            "config_options": config_options,
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
    next_models = _sync_models_with_options(base.models, options)
    next_modes = _sync_modes_with_options(base.modes, options)
    return base.model_copy(
        update={"config_options": options, "models": next_models, "modes": next_modes}
    )


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


def _supports_boolean_config_options(ws_caps: ClientCapabilities) -> bool:
    """Return ``True`` when the client advertised ``session.configOptions.boolean``."""
    return bool(
        ws_caps.session is not None
        and ws_caps.session.config_options is not None
        and ws_caps.session.config_options.boolean is not None
    )


def _strip_boolean_options_list(options: object) -> tuple[list[Any], bool]:
    """Filter out ``type == 'boolean'`` entries and return ``(filtered, changed)``."""
    if not isinstance(options, list):
        return [], False
    filtered = [
        item for item in options
        if not (isinstance(item, Mapping) and item.get("type") == "boolean")
    ]
    return filtered, len(filtered) != len(options)


def _rewrite_update_config_options(
    loaded: Mapping[str, Any],
    params_obj: Mapping[str, Any],
    update_obj: Mapping[str, Any],
) -> str | None:
    """Strip boolean options from ``session/update`` ``config_option_update`` if needed."""
    filtered, changed = _strip_boolean_options_list(update_obj.get("configOptions"))
    if not changed:
        return None
    rewritten_update = {**update_obj, "configOptions": filtered}
    rewritten_params = {**params_obj, "update": rewritten_update}
    return json.dumps(
        {**loaded, "params": rewritten_params},
        separators=(",", ":"),
        ensure_ascii=False,
    )


def filter_boolean_config_options(
    raw_line: str,
    loaded: Mapping[str, Any],
    ws_caps: ClientCapabilities,
) -> str:
    """Strip unadvertised ``type: 'boolean'`` config options from outbound frames."""
    if _supports_boolean_config_options(ws_caps):
        return raw_line
    result_obj = loaded.get("result")
    if isinstance(result_obj, Mapping) and "configOptions" in result_obj:
        filtered, changed = _strip_boolean_options_list(result_obj.get("configOptions"))
        if changed:
            rewritten = {**loaded, "result": {**result_obj, "configOptions": filtered}}
            return json.dumps(rewritten, separators=(",", ":"), ensure_ascii=False)
        return raw_line
    params_obj = loaded.get("params")
    update_obj = params_obj.get("update") if isinstance(params_obj, Mapping) else None
    if (
        isinstance(params_obj, Mapping)
        and isinstance(update_obj, Mapping)
        and "configOptions" in update_obj
    ):
        rewritten = _rewrite_update_config_options(loaded, params_obj, update_obj)
        if rewritten is not None:
            return rewritten
    return raw_line


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
            raise AcpInvalidParamsError(f"Config option '{option.id}' requires a boolean value")
        return
    if declared_type == "boolean" or isinstance(value, bool):
        raise AcpInvalidParamsError(
            f"Config option '{option.id}' requires a string select value"
        )
    allowed = _collect_select_values(option)
    if allowed and value not in allowed:
        raise AcpInvalidParamsError(
            f"Unsupported value {value!r} for config option '{option.id}'"
        )


def _validate_config_option_value(
    parsed: SessionSetConfigOptionParams,
    state: SessionRuntimeState | None,
    *,
    allow_boolean: bool,
) -> None:
    """Validate ``parsed.value`` against boolean capability and advertised ``configOptions``."""
    if isinstance(parsed.value, bool) and not allow_boolean:
        raise AcpInvalidParamsError("Boolean config options were not advertised")
    if isinstance(parsed.value, str) and not parsed.value:
        raise AcpInvalidParamsError("Config option value cannot be empty")
    if state is not None and state.config_options:
        matched = next((opt for opt in state.config_options if opt.id == parsed.config_id), None)
        if matched is None:
            raise AcpInvalidParamsError(f"Unknown session configId: '{parsed.config_id}'")
        _validate_against_known_option(matched, parsed.value, parsed.type)


def prepare_set_config_option_params(
    raw_params: object,
    state: SessionRuntimeState | None,
    *,
    allow_boolean: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Validate ``session/set_config_option`` against ``configOptions`` and scrub ``_meta``."""
    if not isinstance(raw_params, Mapping):
        raise AcpInvalidParamsError(
            "Invalid session/set_config_option params: expected JSON object"
        )
    try:
        parsed = SessionSetConfigOptionParams.model_validate(raw_params)
    except PydanticValidationError as exc:
        raise pydantic_error_to_acp_error(exc, label="session/set_config_option") from exc

    _validate_config_option_value(parsed, state, allow_boolean=allow_boolean)
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
        raise AcpInvalidParamsError("Invalid session/set_mode params: expected JSON object")
    try:
        parsed = SessionSetModeParams.model_validate(raw_params)
    except PydanticValidationError as exc:
        raise pydantic_error_to_acp_error(exc, label="session/set_mode") from exc

    if state is not None and state.modes is not None and state.modes.available_modes:
        allowed_ids = {mode.id for mode in state.modes.available_modes}
        if parsed.mode_id not in allowed_ids:
            raise AcpInvalidParamsError(f"Unknown session modeId: '{parsed.mode_id}'")

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
    "filter_boolean_config_options",
    "prepare_set_config_option_params",
    "prepare_set_mode_params",
]
