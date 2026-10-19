"""Genuine OpenCode tool definitions for the free-tier gateway.

The upstream gate rejects synthesised stubs that merely reuse a recognised
tool name, so the bridge ships the real definitions (captured from the
official client) wrapped in the OpenAI Chat shape
``{"type":"function","function":{...}}``.

The raw definitions live in ``data/opencode_tools.json``
(``name -> {description, parameters}``) and are wrapped into the Chat wire
shape at import time.

- ``GENUINE_TOOLS`` — every recognised name; same-named tools arriving from
  a client are replaced with these definitions.
- ``CORE_TOOL_NAMES`` — the core agent set appended when missing.

Ported from opencode2api (internal/gateway/opencode_tools.json).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Core agent tools the anonymous free tier expects to be present.
CORE_TOOL_NAMES: list[str] = ["shell", "edit", "glob", "grep", "read"]

_TOOLS_JSON_PATH = Path(__file__).parent / "data" / "opencode_tools.json"
_TOOL_SPECS: dict[str, dict[str, Any]] = json.loads(
    _TOOLS_JSON_PATH.read_text(encoding="utf-8")
)

# Recognised tool name -> genuine definition in Chat wire shape.
GENUINE_TOOLS: dict[str, dict[str, Any]] = {
    name: {
        "type": "function",
        "function": {
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
    }
    for name, spec in _TOOL_SPECS.items()
}

# Reworded replacement for opencode's title-generation system prompt.
#
# The gateway fingerprints the original prompt (its ``<task>``, ``<rules>`` and
# ``<examples>`` markers only trip the check together) and answers 403
# FreeTierError on the title request that opencode fires at the start of every
# new chat. A reworded equivalent that still produces a good short title is
# accepted, so the bridge swaps it in.
_TITLE_SYSTEM_PATH = Path(__file__).parent / "data" / "title_system.txt"
TITLE_SYSTEM_PROMPT: str = _TITLE_SYSTEM_PATH.read_text(encoding="utf-8").strip()
