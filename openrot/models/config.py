from pydantic import BaseModel, ConfigDict, Field

from .enums import ActiveLevel, Strategy
from .node import Node
from .profile import Profile

DEFAULT_URLTEST_URL = "https://www.gstatic.com/generate_204"
DEFAULT_BRIDGE_UPSTREAM = "https://opencode.ai/zen"

TOP_LIMIT = 20  # max nodes kept per profile pool after verification


class Config(BaseModel):
    """Top-level runtime configuration persisted as YAML."""

    model_config = ConfigDict(validate_assignment=True)

    port: int = Field(default=7890, ge=1, le=65535)
    strategy: Strategy = Strategy.FALLBACK
    urltest_url: str = Field(default=DEFAULT_URLTEST_URL, pattern=r"^https?://")
    health_interval: int = Field(default=30, ge=1)
    health_timeout: int = Field(default=5, ge=1)
    fail_threshold: int = Field(default=3, ge=1)
    top_limit: int = Field(default=TOP_LIMIT, ge=1)
    max_workers: int = Field(default=50, ge=1, le=500)
    update_interval: int = Field(default=3600, ge=0)
    warp_enabled: bool = True
    profiles: list[Profile] = []
    current_node_id: str | None = None
    active_level: ActiveLevel = ActiveLevel.NONE
    singbox_bin: str = "sing-box"
    bridge_port: int = Field(default=7891, ge=1, le=65535)
    bridge_upstream: str = DEFAULT_BRIDGE_UPSTREAM
    bridge_retry_statuses: list[int] = Field(default_factory=lambda: [429])
    bridge_retry_attempts: int = Field(default=1, ge=0, le=10)
    deduplicate_by_ip: bool = True

    def all_nodes(self) -> list[Node]:
        """Flatten nodes from all profiles into a single list."""
        return [node for profile in self.profiles for node in profile.nodes]
