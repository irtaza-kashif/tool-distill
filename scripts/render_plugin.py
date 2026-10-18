"""Render the Claude Code plugin directories from one source.

``plugin/`` is the release variant (its commands run the pinned library through ``uvx``), ``plugin-dev/``
runs this checkout (``uv run --project <repo> --extra plugin ...``). The root ``.claude-plugin/marketplace.json``
is rendered too. Run ``uv run python scripts/render_plugin.py`` after changing the library version or the
plugin texts; ``tests/test_plugin_files.py`` fails while the rendered files are stale.
"""

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from tooldistill import __version__  # noqa: E402
from tooldistill.distill.prompts import distill_command_body  # noqa: E402
from tooldistill.integrations.claude_code.names import DEFAULT_OWN_PREFIX  # noqa: E402

PLUGIN_NAME = "tooldistill"
MARKETPLACE_NAME = "tooldistill"
AUTHOR = {"name": "irtaza-kashif"}
DESCRIPTION = (
    "Verified, reusable distilled tools built from your own successful runs: records the MCP tool calls of a "
    "session, turns a solved task into a tool with /tooldistill:distill, verifies it before Claude can use it, "
    "and keeps it current when the tools underneath change."
)
REPO_URL = "https://github.com/irtaza-kashif/tool-distill"
# The release plugin installs the library from PyPI, pinned to the version it was rendered with.
RELEASE_FROM = f"tooldistill[plugin]=={__version__}"
VARIANTS = {"release": ROOT / "plugin", "dev": ROOT / "plugin-dev"}


def _dumps(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


def launcher(variant: str) -> tuple[str, list[str]]:
    """The command and the arguments that run the ``tooldistill`` CLI for a variant."""
    if variant == "release":
        return "uvx", ["--from", RELEASE_FROM, "tooldistill"]
    return "uv", ["run", "--project", "${CLAUDE_PLUGIN_ROOT}/..", "--extra", "plugin", "tooldistill"]


def render(variant: str) -> dict[str, str]:
    """Every file of one plugin directory: relative path -> content."""
    command, base = launcher(variant)

    def hook(event: str, *, timeout: int, background: bool) -> list[dict[str, Any]]:
        entry: dict[str, Any] = {"type": "command", "command": command, "args": [*base, "hook", event], "timeout": timeout}
        if background:
            entry["async"] = True
        return [{"hooks": [entry]}]

    files: dict[str, str] = {}
    files[".claude-plugin/plugin.json"] = _dumps(
        {"name": PLUGIN_NAME, "version": __version__, "description": DESCRIPTION, "author": AUTHOR}
    )
    files[".mcp.json"] = _dumps(
        {"mcpServers": {"tooldistill": {"type": "stdio", "command": command, "args": [*base, "serve"], "alwaysLoad": True}}}
    )
    files["hooks/hooks.json"] = _dumps(
        {
            "hooks": {
                "SessionStart": hook("session-start", timeout=120, background=False),
                "PostToolUse": hook("post-tool-use", timeout=60, background=True),
                "PostToolUseFailure": hook("post-tool-use-failure", timeout=60, background=True),
                "SessionEnd": hook("session-end", timeout=60, background=True),
            }
        }
    )
    files["commands/distill.md"] = (
        "---\n"
        "description: Save the task just completed as a verified, reusable distilled tool\n"
        "argument-hint: [what the task was]\n"
        "disable-model-invocation: true\n"
        "---\n\n" + distill_command_body(prefix=DEFAULT_OWN_PREFIX) + "\n"
    )
    files["README.md"] = readme(variant)
    return files


def readme(variant: str) -> str:
    command, base = launcher(variant)
    runs = f"`{command} {' '.join(base)} serve`"
    which = (
        "the released library, pinned to this plugin's version" if variant == "release" else "the library from this checkout (`plugin-dev/..`)"
    )
    return f"""# tooldistill for Claude Code{" (development variant)" if variant == "dev" else ""}

Distilled tools for Claude Code: the session's MCP tool calls are recorded, `/tooldistill:distill` turns a solved task
into a tool, the library verifies it before Claude can use it, and keeps it current when the tools underneath change.
This directory is rendered by `scripts/render_plugin.py` from the library's texts and version; do not edit it by hand.

The plugin's MCP server and hooks run {which}: {runs}.

## What the project needs

A `tooldistill.toml` in the project root that lists the MCP servers tooldistill may call, in the same structure as
Claude Code's `.mcp.json` (or `mcp_config = ".mcp.json"` pointing at that file), for example:

```toml
store = ".tooldistill"
mcp_config = ".mcp.json"
auto_promote = true
allow_live_verification = true   # only with mock or sandboxed tools: live verification has real side effects
min_calls_to_distill = 4
```

The server and the hooks find it through `CLAUDE_PROJECT_DIR`; set `TOOLDISTILL_CONFIG=<path>` to point elsewhere.
Projects without the file are left alone.

## Install

```
claude plugin marketplace add irtaza-kashif/tool-distill      # or the path of a local checkout
claude plugin install tooldistill@tooldistill
```

Development loop, from the project you want to test in:

```
claude --plugin-dir <checkout>/plugin-dev
/reload-plugins                                                 # after editing the library
claude plugin validate <checkout>/plugin-dev --strict
```

## How it works

| Claude Code mechanism | What tooldistill does with it |
|---|---|
| `SessionStart` hook | creates the session's trace (its id is the session id) and tells Claude the trace id and the distilled tools that exist |
| `PostToolUse` / `PostToolUseFailure` hooks | record every tool call Claude makes (MCP tools as `<server>__<tool>`, built-ins as `builtin__<Name>`) |
| MCP server `tooldistill` | offers `propose_distilled_tool` and every verified distilled tool; runs them through its own connections to the project's MCP servers; announces changes with `tools/list_changed` |
| `/tooldistill:distill [what the task was]` | the user's confirmation that the task went well; asks Claude to propose a distilled tool for it |
| `tooldistill` CLI (`list`, `show`, `verify`, `promote`, `tool-updated`, `health`, `rebuild`) | the same store, from the terminal |

Built-in tools (Bash, Edit, Read, ...) are recorded but can never be part of a distilled tool; only calls to MCP tools can.
Hooks carry no token usage, so baselines in the plugin count tool calls and latency only.

A full walkthrough with fake Gmail and billing tools lives in `examples/invoice_agent/claude_code/` of the repository.
"""


def render_marketplace() -> str:
    return _dumps(
        {
            "name": MARKETPLACE_NAME,
            "owner": AUTHOR,
            "metadata": {"description": "tooldistill: verified, reusable distilled tools for agents", "version": __version__},
            "plugins": [
                {
                    "name": PLUGIN_NAME,
                    "source": "./plugin",
                    "description": DESCRIPTION,
                    "version": __version__,
                }
            ],
        }
    )


def expected_files() -> dict[Path, str]:
    """Absolute path -> content for everything the renderer owns."""
    files: dict[Path, str] = {}
    for variant, target in VARIANTS.items():
        for relative, content in render(variant).items():
            files[target / relative] = content
    files[ROOT / ".claude-plugin" / "marketplace.json"] = render_marketplace()
    return files


def write_all() -> list[Path]:
    written = []
    for path, content in expected_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            written.append(path)
    return written


def stale_files() -> list[Path]:
    return [
        path
        for path, content in expected_files().items()
        if not path.is_file() or path.read_text(encoding="utf-8") != content
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if any rendered file is stale")
    args = parser.parse_args(argv)
    if args.check:
        stale = stale_files()
        for path in stale:
            print(f"stale: {path.relative_to(ROOT)}")
        return 1 if stale else 0
    for path in write_all():
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
