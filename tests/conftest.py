from collections.abc import Iterator
from pathlib import Path

import pytest

from tooldistill.core.registry import Registry


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
