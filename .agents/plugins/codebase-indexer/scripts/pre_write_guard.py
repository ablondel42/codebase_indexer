"""PreToolUse Hexagonal boundary and code-hygiene AST guard for edit_file/write_file."""

import ast
import io
import json
import sys
import tokenize
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]

EXPECTED_DOMAINS: frozenset[str] = frozenset(
    {
        "core",
        "storage",
        "lsp",
        "parsing",
        "graph",
        "context",
        "indexing",
        "engine",
        "mcp_server",
        "cli",
    }
)

DOMAIN_SERVICE_PACKAGES: frozenset[str] = frozenset(
    {"storage", "parsing", "lsp", "graph", "context", "indexing", "engine"}
)

STDLIB_NON_SQLITE: frozenset[str] = frozenset(sys.stdlib_module_names) - {"sqlite3"}

ALLOWED_IMPORTS_BY_DOMAIN: dict[str, frozenset[str]] = {
    "core": frozenset({"core", "pydantic"}),
    "storage": frozenset({"core", "storage", "pydantic", "sqlalchemy", "sqlite3"}),
    "lsp": frozenset({"core", "lsp", "pydantic"}),
    "parsing": frozenset({"core", "parsing", "pydantic"}),
    "graph": frozenset({"core", "graph", "pydantic"}),
    "context": frozenset({"core", "graph", "context", "pydantic"}),
    "indexing": frozenset({"core", "parsing", "lsp", "indexing", "pydantic", "watchdog"}),
    "engine": frozenset(
        {
            "core",
            "storage",
            "lsp",
            "parsing",
            "graph",
            "context",
            "indexing",
            "engine",
            "pydantic",
        }
    ),
    "mcp_server": frozenset(
        {
            "core",
            "engine",
            "graph.models",
            "context.models",
            "indexing.models",
            "mcp_server",
            "pydantic",
            "mcp",
        }
    ),
    "cli": frozenset(
        {
            "core",
            "engine",
            "graph.models",
            "context.models",
            "indexing.models",
            "cli",
            "pydantic",
        }
    ),
}

ALLOWED_PROTOCOL_DICT_RETURNS: frozenset[str] = frozenset(
    {
        "dict[str, FileRecord]",
        "dict[str, SymbolNode]",
        "dict[str, SymbolSummary]",
    }
)

DOCUMENTED_BROAD_EXCEPT_FILES: frozenset[str] = frozenset(
    {
        "cli/services/app.py",
        "indexing/services/batch.py",
        "indexing/services/incremental.py",
        "indexing/services/scheduler.py",
        "lsp/services/transport.py",
        "mcp_server/services/registry.py",
        "storage/services/uow.py",
    }
)

LOG_METHODS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical"}
)

CONTEXT_MANAGER_RETURN_TYPES: dict[str, str] = {
    "contextmanager": "Generator",
    "asynccontextmanager": "AsyncGenerator",
}


def _emit_decision(decision: str, reason: str | None = None) -> None:
    """Write hook JSON decision to ``stdout``."""
    payload: dict[str, str] = {"decision": decision}
    if reason is not None:
        payload["reason"] = reason
    sys.stdout.write(json.dumps(payload) + "\n")


def _parse_hook_payload() -> dict[str, Any] | None:
    """Read and decode hook JSON payload from ``stdin``."""
    try:
        loaded = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"pre_write_guard: invalid JSON stdin: {exc}\n")
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _resolve_repo_relative(payload: dict[str, Any], raw_path: object) -> tuple[Path, str] | None:
    """Resolve ``raw_path`` inside ``REPO_ROOT`` if it is a tracked Python path."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path_obj = Path(raw_path)
    if not path_obj.is_absolute():
        base_dir = payload.get("cwd")
        base_path = Path(base_dir) if isinstance(base_dir, str) and base_dir else REPO_ROOT
        path_obj = base_path / path_obj
    try:
        resolved = path_obj.resolve()
        repo_rel = resolved.relative_to(REPO_ROOT).as_posix()
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"pre_write_guard: skipping non-repo path {raw_path!r}: {exc}\n")
        return None
    if resolved.suffix != ".py":
        return None
    in_src = repo_rel.startswith("src/")
    in_tests = repo_rel.startswith("tests/") and not repo_rel.startswith("tests/fixtures/")
    return (resolved, repo_rel) if (in_src or in_tests) else None


def _reconstruct_write_source(target: Path, args: dict[str, Any]) -> str | None:
    """Compute post-write file text for ``write_file``."""
    content = args.get("Content")
    if not isinstance(content, str):
        return None
    if bool(args.get("Append")) and target.is_file():
        return target.read_text(encoding="utf-8") + content
    return content


def _reconstruct_edit_source(target: Path, args: dict[str, Any]) -> str | None:
    """Compute post-edit file text for ``edit_file``."""
    if not target.is_file():
        return None
    target_str = args.get("Target")
    replacement = args.get("Replacement")
    if not isinstance(target_str, str) or not isinstance(replacement, str) or not target_str:
        return None
    existing = target.read_text(encoding="utf-8")
    if target_str not in existing:
        return None
    return existing.replace(target_str, replacement)


def _compute_proposed_source(tool_name: str, target: Path, args: dict[str, Any]) -> str | None:
    """Compute the post-tool file content in memory for ``edit_file`` or ``write_file``."""
    try:
        if tool_name == "write_file":
            return _reconstruct_write_source(target, args)
        if tool_name == "edit_file":
            return _reconstruct_edit_source(target, args)
    except OSError as exc:
        sys.stderr.write(f"pre_write_guard: unable to read {target}: {exc}\n")
        return None
    return None


def _check_noqa_f401_comments(repo_rel: str, raw_text: str) -> list[str]:
    """Flag real Python comments suppressing F401 without false-positives in string literals."""
    errors: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(raw_text).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT and "noqa" in tok.string and "F401" in tok.string:
                errors.append(f"{repo_rel}:{tok.start[0]}: forbidden '# noqa: F401' comment")
    except (tokenize.TokenError, IndentationError) as exc:
        sys.stderr.write(f"pre_write_guard: tokenizer skipped {repo_rel}: {exc}\n")
    return errors


def _terminal_name(node: ast.expr) -> str | None:
    """Return trailing identifier of a ``Name``, ``Attribute``, or ``Subscript`` AST node."""
    if isinstance(node, ast.Subscript):
        return _terminal_name(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _check_common_hygiene(repo_rel: str, tree: ast.Module, raw_text: str) -> list[str]:
    """Validate rules shared by ``src/`` and non-fixture ``tests/`` files."""
    errors = _check_noqa_f401_comments(repo_rel, raw_text)
    top_body_ids = {id(stmt) for stmt in tree.body}

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in top_body_ids:
            errors.append(f"{repo_rel}:{node.lineno}: non-top-level import is forbidden")
        elif (
            isinstance(node, ast.ExceptHandler)
            and node.type is not None
            and "ImportError" in ast.unparse(node.type)
        ):
            errors.append(f"{repo_rel}:{node.lineno}: try/except ImportError is forbidden")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            errors.extend(_check_func_context_manager(repo_rel, node))
    return errors


def _check_func_context_manager(
    repo_rel: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """Ensure ``@contextmanager`` / ``@asynccontextmanager`` return generator types."""
    errors: list[str] = []
    for decorator in node.decorator_list:
        expected = CONTEXT_MANAGER_RETURN_TYPES.get(_terminal_name(decorator) or "")
        if expected is None:
            continue
        actual = _terminal_name(node.returns) if node.returns is not None else None
        if actual != expected:
            errors.append(
                f"{repo_rel}:{node.lineno}: {node.name} must return {expected}[...], got {actual}"
            )
    return errors


def _check_src_layout(src_rel: str) -> tuple[str, str, list[str]]:
    """Verify ``src/`` path obeys 10-domain ``{services,models,utils}`` and naming rules."""
    parts = src_rel.split("/")
    domain = parts[0]
    layer = parts[1] if len(parts) > 2 else ""
    errors: list[str] = []

    if domain not in EXPECTED_DOMAINS:
        errors.append(f"src/{src_rel}: unexpected domain package '{domain}'")
        return domain, layer, errors

    if src_rel.endswith("_service.py"):
        errors.append(f"src/{src_rel}: '_service.py' suffix is forbidden")

    if len(parts) == 2:
        allowed_roots = {"__init__.py", "main.py"} if domain == "cli" else {"__init__.py"}
        if parts[1] not in allowed_roots:
            errors.append(f"src/{src_rel}: unexpected root module '{parts[1]}' in {domain}/")
        return domain, "", errors

    expected_layers = {"ports", "models", "utils"} if domain == "core" else {
        "services",
        "models",
        "utils",
    }
    if layer not in expected_layers:
        errors.append(
            f"src/{src_rel}: layer '{layer}' invalid for '{domain}' ({sorted(expected_layers)})"
        )
    return domain, layer, errors


def _is_module_allowed(domain: str, src_rel: str, module_name: str) -> bool:
    """Return ``True`` when ``module_name`` is permitted inside ``domain`` / ``src_rel``."""
    top = module_name.split(".")[0]
    if top in STDLIB_NON_SQLITE:
        return True
    allowed = set(ALLOWED_IMPORTS_BY_DOMAIN[domain])
    if src_rel == "cli/services/server.py":
        allowed.add("mcp_server")
    if top in allowed:
        return True
    parts = module_name.split(".")
    return len(parts) >= 2 and f"{parts[0]}.{parts[1]}" in allowed


def _check_src_import_from(domain: str, src_rel: str, node: ast.ImportFrom) -> list[str]:
    """Validate an ``ast.ImportFrom`` inside ``src/`` against the Allowed-Import Matrix."""
    if node.level > 0:
        return [f"src/{src_rel}:{node.lineno}: relative import is forbidden"]
    if not node.module:
        return []
    errors: list[str] = []
    if node.module == "contextlib" and any(alias.name == "suppress" for alias in node.names):
        errors.append(f"src/{src_rel}:{node.lineno}: contextlib.suppress is banned")
    if node.module in ("graph", "context", "indexing") and domain in ("mcp_server", "cli"):
        bad = [alias.name for alias in node.names if alias.name != "models"]
        if bad:
            errors.append(
                f"src/{src_rel}:{node.lineno}: forbidden subpackage import "
                f"from {node.module}: {bad}"
            )
        return errors
    if not _is_module_allowed(domain, src_rel, node.module):
        errors.append(f"src/{src_rel}:{node.lineno}: disallowed from-import '{node.module}'")
    return errors


def _check_src_import_node(domain: str, src_rel: str, node: ast.Import) -> list[str]:
    """Validate an ``ast.Import`` inside ``src/`` against the Allowed-Import Matrix."""
    return [
        f"src/{src_rel}:{node.lineno}: disallowed import '{alias.name}'"
        for alias in node.names
        if not _is_module_allowed(domain, src_rel, alias.name)
    ]


def _check_src_imports(domain: str, src_rel: str, tree: ast.Module) -> list[str]:
    """Validate all imports and ``contextlib.suppress`` attributes inside ``src/``."""
    if domain not in EXPECTED_DOMAINS:
        return []
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            errors.extend(_check_src_import_from(domain, src_rel, node))
        elif isinstance(node, ast.Import):
            errors.extend(_check_src_import_node(domain, src_rel, node))
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "contextlib"
            and node.attr == "suppress"
        ):
            errors.append(f"src/{src_rel}:{node.lineno}: contextlib.suppress is banned")
    return errors


def _contains_forbidden_dataclass_or_typeddict(tree: ast.Module) -> bool:
    """Return ``True`` if ``tree`` uses ``@dataclass`` or ``TypedDict``."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module in ("dataclasses", "typing")
            and any(alias.name in ("dataclass", "TypedDict") for alias in node.names)
        ):
            return True
        if isinstance(node, ast.ClassDef):
            bases = {ast.unparse(b) for b in node.bases}
            decs = {ast.unparse(d).split("(")[0] for d in node.decorator_list}
            if "TypedDict" in bases or "dataclass" in decs or "dataclasses.dataclass" in decs:
                return True
    return False


def _check_models_layer(src_rel: str, tree: ast.Module) -> list[str]:
    """Ensure ``models/`` classes inherit ``FrozenModel``, ``MutableModel``, or ``StrEnum``."""
    if src_rel in ("core/models/base.py", "core/models/exceptions.py"):
        return []
    allowed_bases = {"FrozenModel", "MutableModel", "StrEnum"}
    errors: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {ast.unparse(b) for b in node.bases}
        if not (bases & allowed_bases):
            errors.append(
                f"src/{src_rel}:{node.lineno}: model '{node.name}' must inherit "
                "FrozenModel, MutableModel, or StrEnum"
            )
    return errors


def _check_utils_layer(src_rel: str, tree: ast.Module) -> list[str]:
    """Ensure ``utils/`` modules contain no unexpected classes."""
    allowed = {"JsonText"} if src_rel == "storage/utils/schema.py" else set()
    return [
        f"src/{src_rel}:{node.lineno}: unexpected class '{node.name}' in utils/"
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name not in allowed
    ]


def _check_layer_rules(domain: str, layer: str, src_rel: str, tree: ast.Module) -> list[str]:
    """Enforce layer rules across ``models/``, ``utils/``, and ``services/``."""
    errors: list[str] = []
    if _contains_forbidden_dataclass_or_typeddict(tree):
        errors.append(f"src/{src_rel}: uses forbidden dataclass or TypedDict")
    if layer == "models":
        errors.extend(_check_models_layer(src_rel, tree))
    elif layer == "utils":
        errors.extend(_check_utils_layer(src_rel, tree))
    elif layer == "services":
        errors.extend(_check_service_layer(domain, src_rel, tree))
    return errors


def _check_service_class(src_rel: str, cls_node: ast.ClassDef) -> list[str]:
    """Check public methods on ``cls_node`` for unauthorized ``dict`` return types."""
    errors: list[str] = []
    for member in cls_node.body:
        if (
            not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            or member.name.startswith("_")
            or member.returns is None
        ):
            continue
        ret_str = ast.unparse(member.returns)
        is_dict = ret_str == "dict" or ret_str.startswith("dict[")
        if is_dict and ret_str not in ALLOWED_PROTOCOL_DICT_RETURNS:
            errors.append(
                f"src/{src_rel}:{member.lineno}: {cls_node.name}.{member.name} "
                f"returns unauthorized '{ret_str}'"
            )
    return errors


def _check_service_layer(domain: str, src_rel: str, tree: ast.Module) -> list[str]:
    """Enforce class-only ``services/`` and zero unauthorized ``-> dict`` public returns."""
    errors: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            errors.append(
                f"src/{src_rel}:{stmt.lineno}: standalone function '{stmt.name}' in services/"
            )
        elif isinstance(stmt, ast.ClassDef) and domain in DOMAIN_SERVICE_PACKAGES:
            errors.extend(_check_service_class(src_rel, stmt))
    return errors


def _handler_raises_or_logs(handler: ast.ExceptHandler) -> bool:
    """Return ``True`` when ``handler`` raises or logs via ``*logger.<level>()``."""
    for stmt in handler.body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Raise):
                return True
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id.endswith("logger")
                and sub.func.attr in LOG_METHODS
            ):
                return True
    return False


def _is_broad_exception(exc_type: ast.expr | None) -> bool:
    """Return ``True`` if ``exc_type`` catches ``Exception`` or ``BaseException``."""
    if exc_type is None:
        return True
    tokens = {tok.strip("() ") for tok in ast.unparse(exc_type).split(",")}
    return bool(tokens & {"Exception", "BaseException"})


def _check_src_exceptions(src_rel: str, tree: ast.Module) -> list[str]:
    """Enforce zero silent handlers and quarantined broad ``except`` clauses in ``src/``."""
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            errors.append(f"src/{src_rel}:{node.lineno}: bare 'except:' is forbidden")
        if not _handler_raises_or_logs(node):
            errors.append(
                f"src/{src_rel}:{node.lineno}: silent except handler neither raises nor logs"
            )
        if _is_broad_exception(node.type) and src_rel not in DOCUMENTED_BROAD_EXCEPT_FILES:
            errors.append(
                f"src/{src_rel}:{node.lineno}: broad except Exception forbidden outside "
                "documented boundary files"
            )
    return errors


def _collect_violations(repo_rel: str, proposed: str) -> list[str]:
    """Run all AST and layout checks against ``proposed`` source text."""
    try:
        tree = ast.parse(proposed, filename=repo_rel)
    except SyntaxError as exc:
        return [f"{repo_rel}:{exc.lineno or 1}: SyntaxError: {exc.msg}"]

    violations = _check_common_hygiene(repo_rel, tree, proposed)
    if repo_rel.startswith("src/"):
        src_rel = repo_rel.removeprefix("src/")
        domain, layer, layout_errors = _check_src_layout(src_rel)
        violations.extend(layout_errors)
        violations.extend(_check_src_imports(domain, src_rel, tree))
        violations.extend(_check_layer_rules(domain, layer, src_rel, tree))
        violations.extend(_check_src_exceptions(src_rel, tree))
    return violations


def _evaluate_tool_call(payload: dict[str, Any] | None) -> str | None:
    """Return a denial reason string if ``payload`` violates rules, else ``None``."""
    tool_call = payload.get("toolCall") if payload is not None else None
    if not isinstance(tool_call, dict):
        return None
    tool_name = tool_call.get("name")
    args = tool_call.get("args")
    if not isinstance(tool_name, str) or not isinstance(args, dict):
        return None

    resolved = _resolve_repo_relative(payload or {}, args.get("Path"))
    if resolved is None:
        return None
    target, repo_rel = resolved

    proposed = _compute_proposed_source(tool_name, target, args)
    violations = _collect_violations(repo_rel, proposed) if proposed is not None else []
    if not violations:
        return None
    return "Hexagonal / hygiene gate blocked edit:\n- " + "\n- ".join(violations[:8])


def main() -> int:
    """Inspect incoming PreToolUse tool call JSON and emit allow/deny decision."""
    reason = _evaluate_tool_call(_parse_hook_payload())
    if reason is not None:
        _emit_decision("deny", reason)
    else:
        _emit_decision("allow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
