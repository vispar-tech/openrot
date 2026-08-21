import hashlib
from datetime import datetime

from pydantic import BaseModel, model_validator

from .enums import ProfileKind
from .node import Node


def profile_id(name: str) -> str:
    """Return a stable short id derived from the profile name."""
    return "prof-" + hashlib.sha256(name.encode()).hexdigest()[:8]


class Profile(BaseModel):
    """A source profile pulling a list of nodes from a URL."""

    id: str = ""
    name: str
    kind: ProfileKind = ProfileKind.RELAY
    url: str = ""
    priority: int = 0
    enabled: bool = True
    interval: int | None = (
        None  # seconds, per-profile override; None → cfg.update_interval
    )
    nodes: list[Node] = []
    last_update: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def default_id(cls, data: object) -> object:
        """Fill in a deterministic id from the name when not provided."""
        if isinstance(data, dict) and not data.get("id") and data.get("name"):
            data = {**data, "id": profile_id(data["name"])}
        return data
