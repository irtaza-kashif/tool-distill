from pathlib import Path

import pytest

from tooldistill import ToolDistill
from tooldistill.core.config import SAMPLE_CONFIG, Settings, import_string, load_config
from tooldistill.core.errors import ConfigError, ToolDefinitionError

TOOLS_MODULE = '''
TOOLS = [{"name": "ping", "description": "", "input_schema": {"type": "object", "properties": {}}}]


def execute_tool(name, args):
    return {"pong": name}


def probe(name):
    return True
'''


def write_project(tmp_path: Path, toml_text: str) -> Path:
    package = tmp_path / "myapp"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "tools.py").write_text(TOOLS_MODULE, encoding="utf-8")
    path = tmp_path / "tooldistill.toml"
    path.write_text(toml_text, encoding="utf-8")
    return path


def test_sample_config_round_trips(tmp_path):
    config = load_config(write_project(tmp_path, SAMPLE_CONFIG))
    assert config.store == (tmp_path / ".tooldistill").resolve()
    assert config.settings == Settings()
    assert config.executor_ref == "myapp.tools:execute_tool" and config.health_probe_ref is None
    assert config.load_executor()("ping", {}) == {"pong": "ping"}
    assert config.load_tools()[0]["name"] == "ping"
    assert config.load_health_probe() is None


def test_settings_overrides_and_relative_store(tmp_path):
    text = (
        'store = "custom/store"\nexecutor = "myapp.tools:execute_tool"\ntools = "myapp.tools:TOOLS"\n'
        'health_probe = "myapp.tools:probe"\nauto_promote = true\nmin_calls_to_distill = 4\n'
        "min_call_reduction = 0.5\nmax_exposed_tools = 3\n"
    )
    config = load_config(write_project(tmp_path, text))
    assert config.store == (tmp_path / "custom" / "store").resolve()
    assert config.settings.auto_promote and config.settings.min_calls_to_distill == 4
    assert config.settings.min_call_reduction == 0.5 and config.settings.max_exposed_tools == 3
    assert config.load_health_probe()("x") is True


@pytest.mark.parametrize(
    "line,match",
    [
        ("bogus = 1", "unknown setting 'bogus'"),
        ('auto_promote = "yes"', "must be bool"),
        ("min_calls_to_distill = 2.5", "must be int"),
        ("min_calls_to_distill = true", "must be int"),
        ("min_call_reduction = 1.5", "min_call_reduction must be in"),
        ("executor = 3", "'executor' must be a 'module:attr' string"),
        ("store = 3", "'store' must be a path string"),
    ],
)
def test_invalid_config_values(tmp_path, line, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write_project(tmp_path, line + "\n"))


def test_missing_or_broken_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("store = \n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(broken)


def test_import_string_errors(tmp_path):
    with pytest.raises(ConfigError, match="module:attr"):
        import_string("no_colon")
    with pytest.raises(ConfigError, match="module:attr"):
        import_string("module:")
    with pytest.raises(ConfigError, match="cannot import"):
        import_string("does_not_exist_xyz:thing")
    write_project(tmp_path, "")
    with pytest.raises(ConfigError, match="has no attribute"):
        import_string("myapp.tools:nothing", base_dir=tmp_path)
    assert len(import_string("myapp.tools:TOOLS", base_dir=tmp_path)) == 1


def test_missing_refs_raise(tmp_path):
    config = load_config(write_project(tmp_path, 'store = ".tooldistill"\n'))
    with pytest.raises(ConfigError, match="'executor' is not set"):
        config.load_executor()
    with pytest.raises(ConfigError, match="'tools' is not set"):
        config.load_tools()


def test_from_config_builds_tooldistill(tmp_path):
    path = write_project(tmp_path, SAMPLE_CONFIG.replace("auto_promote = false", "auto_promote = true"))
    td = ToolDistill.from_config(path, max_exposed_tools=5, fallback_distiller=False)
    try:
        assert td.settings.auto_promote and td.settings.max_exposed_tools == 5
        assert not td.settings.fallback_distiller
        assert td.tools.names == ("ping",)
        assert (tmp_path / ".tooldistill" / "registry.db").exists()
    finally:
        td.close()


def test_tooldistill_validates_its_arguments(tmp_path):
    with pytest.raises(ConfigError, match="unknown setting"):
        ToolDistill(tmp_path / "s", lambda n, a: None, [], bogus=1)
    with pytest.raises(ConfigError, match="min_call_reduction"):
        ToolDistill(tmp_path / "s", lambda n, a: None, [], min_call_reduction=2.0)
    with pytest.raises(ToolDefinitionError, match="callable"):
        ToolDistill(tmp_path / "s", "not callable", [])  # type: ignore[arg-type]
