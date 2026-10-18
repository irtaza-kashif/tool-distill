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
