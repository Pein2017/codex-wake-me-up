#!/usr/bin/env python3
"""Source-tree bootstrap for the Codex stdio MCP process.

The module is intentionally usable before an operator installs the package.
The plugin manifest runs this script with the plugin root as its working
directory; inserting ``src`` here keeps that source-tree route explicit.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_wake_me_up.mcp_server import main  # noqa: E402


if __name__ == "__main__":
    main()
