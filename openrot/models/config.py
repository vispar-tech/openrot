from pydantic import BaseModel, ConfigDict, Field

from .enums import ActiveLevel

DEFAULT_BRIDGE_UPSTREAM = "https://opencode.ai/zen"


class Config(BaseModel):
    """Top-level runtime configuration persisted as YAML."""

    model_config = ConfigDict(validate_assignment=True)

    health_interval: int = Field(default=30, ge=1)
    active_level: ActiveLevel = ActiveLevel.NONE
    bridge_port: int = Field(default=7891, ge=1, le=65535)
    bridge_upstream: str = DEFAULT_BRIDGE_UPSTREAM
    bridge_retry_statuses: list[int] = Field(default_factory=lambda: [429])
    bridge_retry_attempts: int = Field(default=1, ge=0, le=10)
    bridge_max_concurrent: int = Field(default=3, ge=1, le=20)
    bridge_min_interval: float = Field(default=0.5, ge=0.0)
    bridge_inject_session: bool = True
