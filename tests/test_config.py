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


def test_expand_env_refs(capsys):
    from tooldistill.core.config import expand_env_refs

    env = {"TOKEN": "secret", "EMPTY": ""}
    assert expand_env_refs("Bearer ${TOKEN}", environ=env) == "Bearer secret"
    assert expand_env_refs("${MISSING:-fallback}", environ=env) == "fallback"
    assert expand_env_refs("${EMPTY:-x}", environ=env) == ""  # set but empty wins over the default
    assert expand_env_refs("${MISSING}", environ=env) == "${MISSING}"
    assert "MISSING is not set" in capsys.readouterr().err
    assert expand_env_refs(["${TOKEN}", 1, {"k": "${TOKEN}"}], environ=env) == ["secret", 1, {"k": "secret"}]
    assert expand_env_refs({"${TOKEN}": "${TOKEN}"}, environ=env) == {"${TOKEN}": "secret"}  # keys are left alone


def test_server_spec_from_mapping(tmp_path):
    from tooldistill.core.config import ServerSpec

    stdio = ServerSpec.from_mapping("invoice", {"command": "uv", "args": ["run", "x"], "env": {"A": "${A:-1}"}, "cwd": "sub"}, base_dir=tmp_path, environ={})
    assert (stdio.transport, stdio.command, stdio.args, stdio.env) == ("stdio", "uv", ("run", "x"), {"A": "1"})
    assert stdio.cwd == str(tmp_path / "sub") and stdio.url is None and stdio.headers == {}
    default = ServerSpec.from_mapping("invoice", {"command": "uv", "args": ["run", "x"]}, base_dir=tmp_path, environ={})
    assert default.cwd == str(tmp_path)  # no cwd given: the server runs from the config file's folder
    http = ServerSpec.from_mapping("billing", {"url": "http://x/mcp", "headers": {"Authorization": "Bearer ${T}"}}, environ={"T": "tok"})
    assert (http.transport, http.url, http.headers) == ("http", "http://x/mcp", {"Authorization": "Bearer tok"})
    assert ServerSpec.from_mapping("s", {"type": "sse", "url": "http://x/sse"}).transport == "sse"
    for name, mapping, match in [
        ("bad name", {"command": "x"}, "must match"),
        ("s", {"command": "x", "url": "http://x"}, "exactly one of"),
        ("s", {}, "exactly one of"),
        ("s", {"command": "x", "type": "http"}, "does not match"),
        ("s", {"url": "http://x", "type": "ws"}, "must be stdio, http or sse"),
        ("s", {"command": "x", "args": "notalist"}, "'args' must be a list"),
        ("s", {"command": "x", "env": {"A": 1}}, "'env' must be a table of string"),
        ("s", {"command": "x", "bogus": 1}, "unknown key"),
        ("s", "not a table", "must be a table"),
    ]:
        with pytest.raises(ConfigError, match=match):
            ServerSpec.from_mapping(name, mapping)  # type: ignore[arg-type]


def test_mcp_config_file_and_inline_servers(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"invoice": {"type": "stdio", "command": "python", "args": ["server.py"]}, "crm": {"type": "http", "url": "http://crm/mcp"}}}',
        encoding="utf-8",
    )
    toml = tmp_path / "tooldistill.toml"
    toml.write_text(
        'mcp_config = ".mcp.json"\n[mcpServers.crm]\nurl = "http://override/mcp"\n[mcpServers.extra]\ncommand = "uvx"\nargs = ["extra-server"]\n',
        encoding="utf-8",
    )
    config = load_config(toml)
    assert config.uses_mcp and config.mcp_config == (tmp_path / ".mcp.json").resolve()
    servers = config.load_mcp_servers(environ={})
    assert list(servers) == ["invoice", "crm", "extra"]
    assert servers["crm"].url == "http://override/mcp"  # inline wins over the file
    assert servers["extra"].command == "uvx" and servers["invoice"].args == ("server.py",)
    bare = tmp_path / "bare.json"
    bare.write_text('{"only": {"command": "x"}}', encoding="utf-8")
    toml.write_text('mcp_config = "bare.json"\n', encoding="utf-8")
    assert list(load_config(toml).load_mcp_servers(environ={})) == ["only"]
    toml.write_text('mcp_config = "missing.json"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="mcp_config file not found"):
        load_config(toml).load_mcp_servers()
    bare.write_text("[]", encoding="utf-8")
    toml.write_text('mcp_config = "bare.json"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="expected an object"):
        load_config(toml).load_mcp_servers()
    assert not load_config(write_project(tmp_path, SAMPLE_CONFIG)).uses_mcp


def test_mcp_servers_and_executor_refs_are_exclusive(tmp_path):
    toml = write_project(tmp_path, 'executor = "myapp.tools:execute_tool"\nmcp_config = ".mcp.json"\n')
    with pytest.raises(ConfigError, match="not both"):
        load_config(toml)
    toml.write_text('mcp_config = 3\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="'mcp_config' must be a path string"):
        load_config(toml)
    toml.write_text('mcpServers = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="'mcpServers' must be a table"):
        load_config(toml)


def test_tooldistill_validates_its_arguments(tmp_path):
    with pytest.raises(ConfigError, match="unknown setting"):
        ToolDistill(tmp_path / "s", lambda n, a: None, [], bogus=1)
    with pytest.raises(ConfigError, match="min_call_reduction"):
        ToolDistill(tmp_path / "s", lambda n, a: None, [], min_call_reduction=2.0)
    with pytest.raises(ToolDefinitionError, match="callable"):
        ToolDistill(tmp_path / "s", "not callable", [])  # type: ignore[arg-type]
