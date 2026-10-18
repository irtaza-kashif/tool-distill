from pathlib import Path

import pytest
from helpers import sample_tools

from tooldistill.core.static_check import static_check
from tooldistill.core.tools import ToolSet

TOOLS_DIR = Path(__file__).parent / "fixtures" / "tools"
IMPORT = "from tooldistill import distilled_tool, call\n"


def deco(name: str = "demo", schema: str | None = None, **extra: str) -> str:
    schema = schema or '{"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}'
    args = [f'name="{name}"', 'description="d"', f"input_schema={schema}"]
    args += [f"{key}={value}" for key, value in extra.items()]
    return f"@distilled_tool({', '.join(args)})\n"


DECO = deco()
GOOD = IMPORT + DECO + 'def demo(x):\n    return call("gmail_search", query=x)\n'


def body(code: str) -> str:
    """A module whose tool function body is ``code`` (already indented)."""
    return IMPORT + DECO + "def demo(x):\n" + code


@pytest.fixture
def tools() -> ToolSet:
    return ToolSet(sample_tools())


# ------------------------------------------------------------------------------- good


@pytest.mark.parametrize(
    "file_name,expected_name,deps,has_check",
    [
        ("good_invoice.py", "create_invoice_from_email", ("billing_create_invoice", "billing_find_customer", "gmail_get_message", "gmail_search"), False),
        ("good_with_check.py", "create_invoice_from_email", ("billing_create_invoice", "billing_find_customer", "gmail_get_message", "gmail_search"), True),
        ("good_loop.py", "invoice_all_orders", ("billing_create_invoice", "billing_find_customer", "gmail_get_message", "gmail_search"), False),
    ],
)
def test_good_tools_pass(tools, file_name, expected_name, deps, has_check):
    source = (TOOLS_DIR / file_name).read_text(encoding="utf-8")
    result = static_check(source, tools=tools, expected_name=expected_name)
    assert result.passed, result.message
    assert result.dependencies == deps
    assert result.meta is not None and result.meta.name == expected_name
    assert result.has_check is has_check
    assert [site.line for site in result.call_sites] == sorted(site.line for site in result.call_sites)
    assert result.message.startswith("ok: 4 call() sites, 4 dependencies")


def test_call_sites_record_keys_and_name_is_optional(tools):
    result = static_check(GOOD, tools=tools)
    assert result.passed
    assert [(site.base_tool, site.keys) for site in result.call_sites] == [("gmail_search", ("query",))]
    assert result.meta.input_schema["required"] == ["x"]


@pytest.mark.parametrize(
    "source",
    [
        "import json as j\nfrom datetime import datetime, timedelta\nfrom typing import Any\nimport math\n" + DECO + 'def demo(x):\n    return call("gmail_search", query=j.dumps(x))\n',
        body('    def helper(v):\n        return v.upper()\n    return call("gmail_search", query=helper(x))\n'),
        body('    key = lambda m: m["id"]\n    msgs = call("gmail_search", query=x)\n    return [key(m) for m in msgs if m]\n'),
        body('    for i in range(3):\n        if i:\n            x = call("gmail_search", query=x)\n    return x\n'),
        body('    results = call("gmail_search", query=x, since="1d")\n    return results[0] if results else None\n'),
        IMPORT + deco(schema='{"type": "object", "properties": {"x": {"type": "string"}, "y": {"type": "integer"}}, "required": ["x"]}') + 'def demo(x, *, y=1):\n    return call("gmail_search", query=x)\n',
        IMPORT + DECO + 'def demo(**kwargs):\n    return call("gmail_search", query=kwargs["x"])\n',
    ],
)
def test_allowed_constructs(tools, source):
    result = static_check(source, tools=tools)
    assert result.passed, result.message


# -------------------------------------------------------------------------------- bad

BAD = [
    ("syntax_error", IMPORT + DECO + "def demo(x:\n    return x\n", "syntax error"),
    ("two_tools", IMPORT + DECO + "def a(x):\n    return x\n" + DECO + "def b(x):\n    return x\n", "more than one @distilled_tool"),
    ("no_tool", IMPORT, "no @distilled_tool function found"),
    ("assignment", IMPORT + "X = 1\n" + DECO + "def demo(x):\n    return x\n", "an assignment is not allowed at module level"),
    ("expression", IMPORT + "print(1)\n" + DECO + "def demo(x):\n    return x\n", "an expression statement is not allowed at module level"),
    ("docstring", '"""Module docstring."""\n' + GOOD, "module docstrings are not allowed"),
    ("import_os", "import os\n" + GOOD, "import of 'os' is not allowed"),
    ("import_tooldistill", "import tooldistill\n" + GOOD, "'import tooldistill' is not allowed"),
    ("from_tooldistill_core", "from tooldistill.core import registry\n" + GOOD, "import from 'tooldistill.core' is not allowed"),
    ("from_tooldistill_name", "from tooldistill import ToolResult\n" + GOOD, "'ToolResult' cannot be imported from tooldistill"),
    ("from_other", "from pathlib import Path\n" + GOOD, "import from 'pathlib' is not allowed"),
    ("wildcard", "from datetime import *\n" + GOOD, "wildcard imports are not allowed"),
    ("relative", "from . import x\n" + GOOD, "relative imports are not allowed"),
    ("try_except", body('    try:\n        return call("gmail_search", query=x)\n    except Exception:\n        return None\n'), "try/except is not allowed"),
    ("while", body("    while True:\n        return x\n"), "while is not allowed"),
    ("with", body("    with x:\n        return x\n"), "with is not allowed"),
    ("class", body("    class A:\n        pass\n    return x\n"), "class is not allowed"),
    ("async_def", IMPORT + DECO + "async def demo(x):\n    return x\n", "async def is not allowed"),
    ("global", body("    global y\n    return x\n"), "global is not allowed"),
    ("nonlocal", body("    def inner():\n        nonlocal x\n        return x\n    return inner()\n"), "nonlocal is not allowed"),
    ("yield", body("    yield x\n"), "yield is not allowed"),
    ("dunder_attribute", body("    return x.__class__\n"), "'__class__' is not allowed"),
    ("dunder_import", body('    return __import__("os")\n'), "'__import__' is not allowed"),
    ("dunder_lambda_arg", body("    return (lambda __y: __y)(x)\n"), "'__y' is not allowed"),
    ("dunder_alias", "import json as __j\n" + GOOD, "'__j' is not allowed"),
    ("getattr", body('    return getattr(x, "y")\n'), "'getattr' is not allowed"),
    ("eval", body("    return eval(x)\n"), "'eval' is not allowed"),
    ("open", body('    return open(x)\n'), "'open' is not allowed"),
    ("nonliteral_tool_name", body("    return call(x, query=x)\n"), "literal tool name"),
    ("star_args", body('    return call("gmail_search", *x)\n'), "literal tool name"),
    ("kwargs_splat", body('    return call("gmail_search", **x)\n'), "uses **kwargs; pass explicit keyword arguments"),
    ("unknown_tool", body('    return call("billing_find_customr", name=x)\n'), "call to 'billing_find_customr' — no such base tool"),
    ("propose_tool", body('    return call("propose_distilled_tool", name=x)\n'), "no such base tool"),
    ("missing_required", body('    return call("gmail_search", since=x)\n'), "is missing required property 'query'"),
    ("call_alias", body('    f = call\n    return f("gmail_search", query=x)\n'), "'call' may only be used as call("),
    ("rebind_call", body("    call = x\n    return call\n"), "rebinding 'call'"),
    ("rebind_call_param", IMPORT + DECO + "def demo(x, call=None):\n    return x\n", "rebinding 'call'"),
    ("rebind_alias", "import json as call\n" + GOOD, "rebinding 'call'"),
    ("decorator_misuse", body("    return distilled_tool\n"), "'distilled_tool' may only be used as the decorator"),
    ("call_in_check", GOOD + '\n\ndef check(result):\n    return call("gmail_search", query="x")\n', "check() must not call base tools"),
    ("check_params", GOOD + "\n\ndef check(result, other):\n    return True\n", "check() must take exactly one parameter"),
    ("check_twice", GOOD + "\n\ndef check(result):\n    return True\n\n\ndef check(result):\n    return True\n", "check() is defined more than once"),
    ("extra_function", GOOD + "\n\ndef helper(x):\n    return x\n", "function 'helper' is not allowed at module level"),
    ("nested_import", body("    import json\n    return json.dumps(x)\n"), "imports are only allowed at module level"),
    ("nested_decorator", body("    @staticmethod\n    def inner():\n        return x\n    return x\n"), "decorators are not allowed inside the tool"),
    ("positional_only", IMPORT + DECO + "def demo(x, /):\n    return x\n", "must not use positional-only parameters"),
    ("param_not_required", IMPORT + DECO + "def demo(x, y):\n    return x\n", "parameter 'y' of demo() has no default and is not in input_schema.required"),
    ("property_not_param", IMPORT + DECO + "def demo(z=1):\n    return z\n", "input_schema property 'x' is not a parameter of demo()"),
    ("decorator_positional", IMPORT + '@distilled_tool("demo", description="d", input_schema={"type": "object"})\ndef demo():\n    return 1\n', "keyword arguments only"),
    ("decorator_missing_arg", IMPORT + '@distilled_tool(name="demo", input_schema={"type": "object"})\ndef demo():\n    return 1\n', "missing the 'description' argument"),
    ("decorator_unknown_arg", IMPORT + deco(version="2") + "def demo(x):\n    return x\n", "unknown @distilled_tool argument 'version'"),
    ("decorator_nonliteral", IMPORT + '@distilled_tool(name="demo", description="d", input_schema=SCHEMA)\ndef demo():\n    return 1\n', "argument 'input_schema' must be a literal"),
    ("decorator_kwargs", IMPORT + '@distilled_tool(**cfg)\ndef demo():\n    return 1\n', "does not accept **kwargs"),
    ("wrong_decorator", IMPORT + "@staticmethod\ndef demo():\n    return 1\n", "the only decorator allowed is @distilled_tool"),
    ("two_decorators", IMPORT + "@staticmethod\n" + DECO + "def demo(x):\n    return x\n", "exactly one decorator"),
    ("bad_name_pattern", IMPORT + deco(name="Demo") + "def demo(x):\n    return x\n", "must match ^[a-z][a-z0-9_]{2,63}$"),
    ("name_collides_base", IMPORT + deco(name="gmail_search") + "def demo(x):\n    return x\n", "collides with a base tool"),
    ("name_is_propose", IMPORT + deco(name="propose_distilled_tool") + "def demo(x):\n    return x\n", "collides with the propose tool"),
    ("schema_invalid", IMPORT + deco(schema='{"type": "object", "properties": 5}') + "def demo():\n    return 1\n", "input_schema is not a valid JSON Schema"),
    ("schema_not_object", IMPORT + deco(schema='{"type": "string"}') + "def demo():\n    return 1\n", "input_schema must have type 'object'"),
    ("schema_not_dict", IMPORT + deco(schema="[]") + "def demo():\n    return 1\n", "input_schema must be a dict"),
    ("too_large", GOOD + "# " + "x" * 70_000 + "\n", "larger than 64 KiB"),
]


@pytest.mark.parametrize("source,expected", [(src, exp) for _, src, exp in BAD], ids=[name for name, _, _ in BAD])
def test_bad_tools_fail_with_precise_messages(tools, source, expected):
    result = static_check(source, tools=tools)
    assert not result.passed
    assert expected in result.message, result.message
    assert result.dependencies == () and result.meta is None


def test_unknown_keys_fail_only_when_the_schema_forbids_them(tools):
    source = body('    return call("gmail_search", query=x, sinse="1d")\n')
    assert static_check(source, tools=tools).passed  # the schema allows additional properties: replay decides
    strict = sample_tools()
    strict[0]["input_schema"]["additionalProperties"] = False
    result = static_check(source, tools=ToolSet(strict))
    assert not result.passed
    assert "passes unknown property 'sinse' (schema properties: query, since; additionalProperties is false)" in result.message


def test_failure_messages_name_the_line(tools):
    result = static_check(body('    msgs = call("gmail_search", query=x)\n    return call("billing_find_customr", name=x)\n'), tools=tools)
    assert result.message.startswith("line 5: call to 'billing_find_customr'")


def test_name_must_match_the_proposal(tools):
    result = static_check(GOOD, tools=tools, expected_name="other_name")
    assert not result.passed
    assert "decorator name 'demo' does not match the proposal name 'other_name'" in result.message
    assert static_check(GOOD, tools=tools, expected_name="demo").passed
