import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tooldistill.core.registry import Registry

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = ROOT / "examples" / "invoice_agent"
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / ".tooldistill"


@pytest.fixture
def registry(store: Path) -> Iterator[Registry]:
    reg = Registry(store)
    try:
        yield reg
    finally:
        reg.close()


def _load_example_module(alias: str, path: Path):
    """Import an example's module under a distinct name (several examples have a ``tools.py``)."""
    import importlib.util

    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


_load_example_module("incident_tools", ROOT / "examples" / "incident_agent" / "tools.py")
