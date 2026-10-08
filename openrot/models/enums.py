from enum import StrEnum


class ActiveLevel(StrEnum):
    """What the cascade is currently serving on the local port."""

    NONE = "none"
    WARP = "warp"
