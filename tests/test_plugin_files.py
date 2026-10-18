"""The rendered Claude Code plugin directories and the marketplace file."""

import importlib.util
import json
from pathlib import Path

import pytest

from tooldistill import __version__
from tooldistill.distill.prompts import FILE_FORMAT_RULES, NAMING_GUIDANCE
from tooldistill.integrations.claude_code.names import DEFAULT_OWN_PREFIX

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render_plugin.py"
EVENTS = {"SessionStart": "session-start", "PostToolUse": "post-tool-use", "PostToolUseFailure": "post-tool-use-failure", "SessionEnd": "session-end"}


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_plugin", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_rendered_files_are_up_to_date():
    renderer = load_renderer()
    assert renderer.stale_files() == []
    assert renderer.main(["--check"]) == 0


@pytest.mark.parametrize("variant", ["plugin", "plugin-dev"])
def test_plugin_directory(variant):
    folder = ROOT / variant
    manifest = read_json(folder / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "tooldistill" and manifest["version"] == __version__
    assert manifest["author"]["name"] and manifest["description"]

    mcp = read_json(folder / ".mcp.json")
    server = mcp["mcpServers"]["tooldistill"]
    assert server["type"] == "stdio" and server["alwaysLoad"] is True
    assert server["args"][-1] == "serve" and "tooldistill" in server["args"]
    if variant == "plugin":
        assert server["command"] == "uvx" and server["args"][1] == f"tooldistill[plugin]=={__version__}"
    else:
        assert server["command"] == "uv" and server["args"][:4] == ["run", "--project", "${CLAUDE_PLUGIN_ROOT}/..", "--extra"]

    hooks = read_json(folder / "hooks" / "hooks.json")["hooks"]
    assert set(hooks) == set(EVENTS)
    for event, slug in EVENTS.items():
        [group] = hooks[event]
        [handler] = group["hooks"]
        assert handler["type"] == "command" and handler["command"] == server["command"]
        assert handler["args"][:-2] == server["args"][:-1] and handler["args"][-2:] == ["hook", slug]
        assert isinstance(handler["timeout"], int) and handler["timeout"] > 0
        assert ("async" in handler) == (event != "SessionStart")  # recording never blocks Claude; SessionStart must finish first

    command = (folder / "commands" / "distill.md").read_text(encoding="utf-8")
    assert command.startswith("---\ndescription: ")
    assert "disable-model-invocation: true" in command and "argument-hint:" in command
    assert f"{DEFAULT_OWN_PREFIX}propose_distilled_tool" in command and "$ARGUMENTS" in command
    assert NAMING_GUIDANCE in command and FILE_FORMAT_RULES in command

    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert "tooldistill.toml" in readme and "/tooldistill:distill" in readme


def test_marketplace_points_at_the_release_plugin():
    marketplace = read_json(ROOT / ".claude-plugin" / "marketplace.json")
    assert marketplace["name"] == "tooldistill" and marketplace["owner"]["name"]
    [entry] = marketplace["plugins"]
    assert entry == {"name": "tooldistill", "source": "./plugin", "description": entry["description"], "version": __version__}
    assert (ROOT / "plugin" / ".claude-plugin" / "plugin.json").is_file()


def test_plugin_directories_stay_out_of_the_wheel():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'build-backend = "uv_build"' in text  # src layout: only src/tooldistill ships
    assert not (ROOT / "src" / "tooldistill" / "plugin").exists()


def test_renderer_writes_to_a_fresh_location(tmp_path, monkeypatch):
    renderer = load_renderer()
    monkeypatch.setattr(renderer, "ROOT", tmp_path)
    monkeypatch.setattr(renderer, "VARIANTS", {"release": tmp_path / "plugin", "dev": tmp_path / "plugin-dev"})
    written = renderer.write_all()
    assert len(written) == 11  # 5 files per variant + the marketplace
    assert renderer.stale_files() == [] and renderer.write_all() == []  # idempotent
    (tmp_path / "plugin" / ".mcp.json").write_text("{}", encoding="utf-8")
    assert renderer.stale_files() == [tmp_path / "plugin" / ".mcp.json"]
    assert renderer.main(["--check"]) == 1
