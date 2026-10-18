"""Reset the demos between takes: delete their stores and put every switch file back to all-off.

    uv run python scripts/demo_reset.py

Touches only the paths listed below, nothing else. Safe to run twice.
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STORES = [
    ROOT / ".tooldistill",  # the sandbox agent's store (examples/sandbox_agent.py runs from the repo root)
    ROOT / "examples" / "invoice_agent" / ".tooldistill",
    ROOT / "examples" / "invoice_agent" / "claude_code" / ".tooldistill",
    ROOT / "examples" / "incident_agent" / ".tooldistill",
    ROOT / "examples" / "incident_agent" / "claude_code" / ".tooldistill",
]

SWITCH_FILES = {
    ROOT / "examples" / "invoice_agent" / "claude_code" / "switches.json": {
        "import_outage": False,
        "schema_currency": False,
        "tools_updated": False,
    },
    ROOT / "examples" / "incident_agent" / "claude_code" / "switches.json": {
        "rollback_outage": False,
        "schema_reason": False,
        "tools_updated": False,
    },
}


def main() -> int:
    for store in STORES:
        if store.is_dir():
            shutil.rmtree(store)
            print(f"removed  {store.relative_to(ROOT)}")
        else:
            print(f"absent   {store.relative_to(ROOT)}")
    for path, switches in SWITCH_FILES.items():
        path.write_text(json.dumps(switches) + "\n", encoding="utf-8")
        print(f"reset    {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
