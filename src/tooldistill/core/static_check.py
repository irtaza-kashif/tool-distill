"""The static check: the rules a distilled tool module must satisfy before it is ever imported.

Everything here works on the source text with ``ast``. Nothing imports the module, because importing
runs its top-level code.
"""

import ast
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from tooldistill.core.runtime import DistilledToolMeta
from tooldistill.core.tools import DISTILLED_NAME_RE, PROPOSE_TOOL_NAME, ToolSet

__all__ = [
    "ALLOWED_IMPORT_MODULES",
    "BANNED_NAMES",
    "MAX_SOURCE_BYTES",
    "CallSite",
    "StaticResult",
    "static_check",
]

MAX_SOURCE_BYTES = 64 * 1024
LIBRARY_MODULE = "tooldistill"
LIBRARY_NAMES = frozenset({"distilled_tool", "call"})
ALLOWED_IMPORT_MODULES = frozenset({"typing", "json", "re", "datetime", "math"})
DECORATOR_ARGS = ("name", "description", "input_schema")
BANNED_NODES: tuple[tuple[type[ast.AST], str], ...] = (
    (ast.Try, "try/except"),
    (ast.TryStar, "try/except*"),
    (ast.While, "while"),
    (ast.With, "with"),
    (ast.AsyncWith, "async with"),
    (ast.AsyncFor, "async for"),
    (ast.AsyncFunctionDef, "async def"),
    (ast.Await, "await"),
    (ast.ClassDef, "class"),
    (ast.Global, "global"),
    (ast.Nonlocal, "nonlocal"),
    (ast.Yield, "yield"),
    (ast.YieldFrom, "yield from"),
)
BANNED_NAMES = frozenset(
    {
        "open", "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
        "getattr", "setattr", "delattr", "breakpoint", "input", "exit", "quit", "help", "memoryview",
        "os", "sys", "subprocess", "socket", "requests", "urllib", "pathlib", "shutil", "importlib",
    }
)


class _Fail(Exception):
    def __init__(self, line: int | None, message: str) -> None:
        self.line = line
        self.message = message
        super().__init__(f"line {line}: {message}" if line is not None else message)


@dataclass(frozen=True, slots=True)
class CallSite:
    line: int
    base_tool: str
    keys: tuple[str, ...]


@dataclass(slots=True)
class StaticResult:
    passed: bool
    message: str
    meta: DistilledToolMeta | None = None
    dependencies: tuple[str, ...] = ()
    call_sites: tuple[CallSite, ...] = ()
    has_check: bool = False


def static_check(source: str, *, tools: ToolSet, expected_name: str | None = None) -> StaticResult:
    """Check a distilled tool module's source. Fails on the first broken rule, naming the line."""
    try:
        return _check(source, tools, expected_name)
    except _Fail as fail:
        return StaticResult(passed=False, message=str(fail))


def _check(source: str, tools: ToolSet, expected_name: str | None) -> StaticResult:
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise _Fail(None, "module is larger than 64 KiB")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise _Fail(exc.lineno, f"syntax error: {exc.msg}") from None

    main: ast.FunctionDef | None = None
    check_fn: ast.FunctionDef | None = None
    for node in tree.body:
        match node:
            case ast.Import():
                _check_import(node)
            case ast.ImportFrom():
                _check_import_from(node)
            case ast.FunctionDef() if node.decorator_list:
                if main is not None:
                    raise _Fail(node.lineno, "more than one @distilled_tool function; a module defines exactly one")
                _check_decorator_list(node)
                main = node
            case ast.FunctionDef():
                if node.name != "check":
                    raise _Fail(
                        node.lineno,
                        f"function '{node.name}' is not allowed at module level: only the @distilled_tool "
                        "function and check() may be defined",
                    )
                if check_fn is not None:
                    raise _Fail(node.lineno, "check() is defined more than once")
                _check_check_signature(node)
                check_fn = node
            case ast.AsyncFunctionDef():
                raise _Fail(node.lineno, "async def is not allowed in a distilled tool")
            case ast.Expr(value=ast.Constant(value=str())):
                raise _Fail(
                    node.lineno,
                    "module docstrings are not allowed: the module must contain only imports, "
                    "the @distilled_tool function and check()",
                )
            case _:
                raise _Fail(
                    node.lineno,
                    f"{_describe_node(node)} is not allowed at module level: only imports, "
                    "the @distilled_tool function and check() are allowed",
                )
    if main is None:
        raise _Fail(None, "no @distilled_tool function found")

    meta = _read_decorator(main, tools, expected_name)
    _walk_rules(tree, main)
    sites = _call_sites(tree, tools)
    if check_fn is not None and any(_is_call_to_call(n) for n in ast.walk(check_fn)):
        raise _Fail(check_fn.lineno, "check() must not call base tools")
    _check_signature(main, meta.input_schema)
    dependencies = tuple(sorted({site.base_tool for site in sites}))
    return StaticResult(
        passed=True,
        message=f"ok: {len(sites)} call() sites, {len(dependencies)} dependencies",
        meta=meta,
        dependencies=dependencies,
        call_sites=tuple(sites),
        has_check=check_fn is not None,
    )


# ------------------------------------------------------------------------------ imports


def _check_import(node: ast.Import) -> None:
    for alias in node.names:
        root = alias.name.split(".")[0]
        if root == LIBRARY_MODULE:
            raise _Fail(
                node.lineno,
                f"'import {alias.name}' is not allowed; use 'from tooldistill import call, distilled_tool'",
            )
        if root not in ALLOWED_IMPORT_MODULES:
            raise _Fail(node.lineno, f"import of '{alias.name}' is not allowed (allowed: {_allowed_modules()})")


def _check_import_from(node: ast.ImportFrom) -> None:
    if node.level != 0 or node.module is None:
        raise _Fail(node.lineno, "relative imports are not allowed")
    root = node.module.split(".")[0]
    if root == LIBRARY_MODULE:
        if node.module != LIBRARY_MODULE:
            raise _Fail(node.lineno, f"import from '{node.module}' is not allowed; only 'from tooldistill import ...'")
        for alias in node.names:
            if alias.name not in LIBRARY_NAMES:
                raise _Fail(
                    node.lineno,
                    f"'{alias.name}' cannot be imported from tooldistill (only call and distilled_tool)",
                )
        return
    if root not in ALLOWED_IMPORT_MODULES:
        raise _Fail(node.lineno, f"import from '{node.module}' is not allowed (allowed: {_allowed_modules()})")
    for alias in node.names:
        if alias.name == "*":
            raise _Fail(node.lineno, "wildcard imports are not allowed")


def _allowed_modules() -> str:
    return ", ".join(sorted(ALLOWED_IMPORT_MODULES))


# ---------------------------------------------------------------------------- decorator


def _check_decorator_list(node: ast.FunctionDef) -> None:
    if len(node.decorator_list) != 1:
        raise _Fail(node.lineno, "the tool function takes exactly one decorator: @distilled_tool(...)")
    deco = node.decorator_list[0]
    if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Name) and deco.func.id == "distilled_tool"):
        raise _Fail(node.lineno, "the only decorator allowed is @distilled_tool(name=..., description=..., input_schema=...)")


def _read_decorator(main: ast.FunctionDef, tools: ToolSet, expected_name: str | None) -> DistilledToolMeta:
    deco = main.decorator_list[0]
    assert isinstance(deco, ast.Call)
    if deco.args:
        raise _Fail(deco.lineno, "@distilled_tool takes keyword arguments only (name=, description=, input_schema=)")
    values: dict[str, object] = {}
    for keyword in deco.keywords:
        if keyword.arg is None:
            raise _Fail(deco.lineno, "@distilled_tool does not accept **kwargs")
        if keyword.arg not in DECORATOR_ARGS:
            raise _Fail(deco.lineno, f"unknown @distilled_tool argument '{keyword.arg}'")
        try:
            values[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, SyntaxError, TypeError):
            raise _Fail(keyword.value.lineno, f"@distilled_tool argument '{keyword.arg}' must be a literal") from None
    for required in DECORATOR_ARGS:
        if required not in values:
            raise _Fail(deco.lineno, f"@distilled_tool is missing the '{required}' argument")
    name = values["name"]
    if not isinstance(name, str) or not DISTILLED_NAME_RE.match(name):
        raise _Fail(deco.lineno, f"name {name!r} must match ^[a-z][a-z0-9_]{{2,63}}$")
    if expected_name is not None and name != expected_name:
        raise _Fail(deco.lineno, f"decorator name '{name}' does not match the proposal name '{expected_name}'")
    if name in tools:
        raise _Fail(deco.lineno, f"name '{name}' collides with a base tool")
    if name == PROPOSE_TOOL_NAME:
        raise _Fail(deco.lineno, f"name '{name}' collides with the propose tool")
    description = values["description"]
    if not isinstance(description, str):
        raise _Fail(deco.lineno, "description must be a string")
    schema = values["input_schema"]
    if not isinstance(schema, dict):
        raise _Fail(deco.lineno, "input_schema must be a dict")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise _Fail(deco.lineno, f"input_schema is not a valid JSON Schema: {exc.message}") from None
    if schema.get("type") != "object":
        raise _Fail(deco.lineno, "input_schema must have type 'object'")
    return DistilledToolMeta(name=name, description=description, input_schema=schema)


# ------------------------------------------------------------------------------- rules


def _walk_rules(tree: ast.Module, main: ast.FunctionDef) -> None:
    top_level = {id(node) for node in tree.body}
    decorator_funcs = {id(deco.func) for deco in main.decorator_list if isinstance(deco, ast.Call)}
    call_funcs = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "call"
    }
    for node in ast.walk(tree):
        for node_type, label in BANNED_NODES:
            if isinstance(node, node_type):
                raise _Fail(node.lineno, f"{label} is not allowed in a distilled tool")
        match node:
            case ast.Import() | ast.ImportFrom():
                if id(node) not in top_level:
                    raise _Fail(node.lineno, "imports are only allowed at module level")
            case ast.FunctionDef():
                _check_identifier(node.name, node.lineno)
                if id(node) not in top_level and node.decorator_list:
                    raise _Fail(node.lineno, "decorators are not allowed inside the tool")
            case ast.Name():
                _check_identifier(node.id, node.lineno)
                if node.id in BANNED_NAMES:
                    raise _Fail(node.lineno, f"'{node.id}' is not allowed in a distilled tool")
                if node.id in LIBRARY_NAMES:
                    if isinstance(node.ctx, ast.Store | ast.Del):
                        raise _Fail(node.lineno, f"rebinding '{node.id}' is not allowed")
                    if node.id == "call" and id(node) not in call_funcs:
                        raise _Fail(node.lineno, "'call' may only be used as call(\"<tool name>\", ...)")
                    if node.id == "distilled_tool" and id(node) not in decorator_funcs:
                        raise _Fail(node.lineno, "'distilled_tool' may only be used as the decorator")
            case ast.Attribute():
                _check_identifier(node.attr, node.lineno)
            case ast.arg():
                _check_identifier(node.arg, node.lineno)
                if node.arg in LIBRARY_NAMES:
                    raise _Fail(node.lineno, f"rebinding '{node.arg}' is not allowed")
            case ast.keyword() if node.arg is not None:
                _check_identifier(node.arg, node.lineno)
            case ast.alias():
                _check_identifier(node.name, node.lineno)
                if node.asname is not None:
                    _check_identifier(node.asname, node.lineno)
                    if node.asname in LIBRARY_NAMES and node.name not in LIBRARY_NAMES:
                        raise _Fail(node.lineno, f"rebinding '{node.asname}' is not allowed")


def _check_identifier(name: str, line: int) -> None:
    if name.startswith("__"):
        raise _Fail(line, f"'{name}' is not allowed: names starting with '__' are banned")


def _is_call_to_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "call"


def _call_sites(tree: ast.Module, tools: ToolSet) -> list[CallSite]:
    sites: list[CallSite] = []
    for node in ast.walk(tree):
        if not _is_call_to_call(node):
            continue
        assert isinstance(node, ast.Call)
        line = node.lineno
        if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
            raise _Fail(line, "call() needs a literal tool name string as its only positional argument")
        tool_name = node.args[0].value
        keys: list[str] = []
        for keyword in node.keywords:
            if keyword.arg is None:
                raise _Fail(line, f"call to '{tool_name}' uses **kwargs; pass explicit keyword arguments")
            keys.append(keyword.arg)
        base = tools.get(tool_name)
        if base is None:
            raise _Fail(line, f"call to '{tool_name}' — no such base tool")
        properties = base.input_schema.get("properties")
        # JSON Schema allows properties it does not list unless additionalProperties is false. Only then
        # is an unknown key a static error; otherwise replay decides (a stale tool definition must not
        # block a call the real API accepts, e.g. a field the API grew before its definition did).
        if isinstance(properties, dict) and base.input_schema.get("additionalProperties") is False:
            for key in keys:
                if key not in properties:
                    listed = ", ".join(properties) or "none"
                    raise _Fail(
                        line,
                        f"call to '{tool_name}' passes unknown property '{key}' "
                        f"(schema properties: {listed}; additionalProperties is false)",
                    )
        required = base.input_schema.get("required")
        if isinstance(required, list):
            for prop in required:
                if prop not in keys:
                    raise _Fail(line, f"call to '{tool_name}' is missing required property '{prop}'")
        sites.append(CallSite(line=line, base_tool=tool_name, keys=tuple(keys)))
    sites.sort(key=lambda site: site.line)
    return sites


def _check_check_signature(node: ast.FunctionDef) -> None:
    args = node.args
    if len(args.args) != 1 or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg:
        raise _Fail(node.lineno, "check() must take exactly one parameter (the tool's result)")


def _check_signature(main: ast.FunctionDef, schema: dict[str, object]) -> None:
    args = main.args
    if args.posonlyargs:
        raise _Fail(main.lineno, f"{main.name}() must not use positional-only parameters")
    params = [a.arg for a in args.args] + [a.arg for a in args.kwonlyargs]
    without_default = [a.arg for a in args.args[: len(args.args) - len(args.defaults)]]
    without_default += [a.arg for a, default in zip(args.kwonlyargs, args.kw_defaults, strict=True) if default is None]
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required")
    required = required if isinstance(required, list) else []
    if args.kwarg is None:
        for prop in properties:
            if prop not in params:
                raise _Fail(main.lineno, f"input_schema property '{prop}' is not a parameter of {main.name}()")
    for param in without_default:
        if param not in required:
            raise _Fail(main.lineno, f"parameter '{param}' of {main.name}() has no default and is not in input_schema.required")


def _describe_node(node: ast.AST) -> str:
    match node:
        case ast.Assign() | ast.AnnAssign() | ast.AugAssign():
            return "an assignment"
        case ast.Expr():
            return "an expression statement"
        case ast.If():
            return "an if statement"
        case ast.For():
            return "a for loop"
        case _:
            return f"a {type(node).__name__} statement"
