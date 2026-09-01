"""Toggle opencode provider in opencode.jsonc."""

import json
import shutil
from pathlib import Path

import json5

CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.jsonc"
OPENCODE_PROVIDER: dict = {"options": {"baseURL": "http://127.0.0.1:7891/v1"}}
SCHEMA = "https://opencode.ai/config.json"


def _read(path: Path) -> dict:
    if not path.exists():
        return {"$schema": SCHEMA}
    return json5.loads(path.read_text())


def _write(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent="\t", ensure_ascii=False) + "\n")


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(".jsonc.bak"))


def is_enabled(data: dict) -> bool:
    return "opencode" in data.get("provider", {})


def toggle(action: str = "auto", path: Path = CONFIG_PATH) -> bool:
    """Toggle the opencode provider. Returns new state (True=on, False=off)."""
    _backup(path)
    data = _read(path)
    current = is_enabled(data)
    new_state = not current if action == "auto" else action == "on"

    data.setdefault("provider", {})
    if new_state:
        data["provider"]["opencode"] = OPENCODE_PROVIDER
    else:
        data["provider"].pop("opencode", None)
        if not data["provider"]:
            data.pop("provider", None)

    _write(data, path)
    return new_state
