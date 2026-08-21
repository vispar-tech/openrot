from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from openrot import config as cfg
from openrot.models import Config, Node, Profile


def test_listen_address_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROT_LISTEN", raising=False)
    assert cfg.listen_address() == "127.0.0.1"
    monkeypatch.setenv("OPENROT_LISTEN", "0.0.0.0")
    assert cfg.listen_address() == "0.0.0.0"


def test_load_creates_default_config_and_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    c = cfg.load_config(path)
    assert c.port == 7890
    assert c.urltest_url == "https://www.gstatic.com/generate_204"
    assert c.profiles == []
    assert path.exists()


def test_config_urltest_url_default_and_validation() -> None:
    assert Config().urltest_url == "https://www.gstatic.com/generate_204"
    with pytest.raises(ValidationError):
        Config(urltest_url="gstatic.com/generate_204")
    assert (
        Config(urltest_url="https://example.test/probe").urltest_url
        == "https://example.test/probe"
    )


def test_config_urltest_url_validate_assignment() -> None:
    c = Config()
    with pytest.raises(ValidationError):
        c.urltest_url = "not-a-url"  # type: ignore[assignment]
    assert c.urltest_url == "https://www.gstatic.com/generate_204"


def test_save_load_roundtrip_preserves_model(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    when = datetime(2026, 1, 2, 3, 4, 5)
    original = cfg.Config(
        strategy="urltest",
        profiles=[
            Profile(
                name="pool",
                kind="relay",
                url="https://example.invalid/list.txt",
                nodes=[
                    Node(
                        id="node-abc",
                        raw="vless://a@1.1.1.1:80#X",
                        status="alive",
                        latency_ms=12.5,
                        last_check=when,
                    )
                ],
            )
        ],
    )
    cfg.save_config(original, path)
    loaded = cfg.load_config(path)

    assert loaded.strategy == original.strategy
    assert loaded.profiles[0].name == "pool"
    assert loaded.profiles[0].nodes[0].raw == "vless://a@1.1.1.1:80#X"
    assert loaded.profiles[0].nodes[0].status == "alive"
    assert loaded.profiles[0].nodes[0].latency_ms == 12.5
    assert loaded.profiles[0].nodes[0].last_check == when


def test_env_overrides_port_and_singbox_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_PORT", "9999")
    monkeypatch.setenv("OPENROT_SINGBOX_BIN", str(tmp_path / "sing-box"))
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    loaded = cfg.load_config(path)
    assert loaded.port == 9999
    assert loaded.singbox_bin == str(tmp_path / "sing-box")


def test_env_overrides_bridge_port_and_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_PORT", "9001")
    monkeypatch.setenv("OPENROT_UPSTREAM", "https://up.example/v1")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    loaded = cfg.load_config(path)
    assert loaded.bridge_port == 9001
    assert loaded.bridge_upstream == "https://up.example/v1"


def test_env_bad_bridge_port_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_PORT", "not-a-number")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_env_bad_int_list_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_RETRY_STATUSES", "429,not-a-number")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_env_overrides_retry_and_max_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_RETRY_STATUSES", "429,503")
    monkeypatch.setenv("OPENROT_BRIDGE_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("OPENROT_MAX_WORKERS", "120")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    loaded = cfg.load_config(path)
    assert loaded.bridge_retry_statuses == [429, 503]
    assert loaded.bridge_retry_attempts == 3
    assert loaded.max_workers == 120


def test_env_bad_retry_attempts_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_RETRY_ATTEMPTS", "not-a-number")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_config_bridge_and_pool_defaults() -> None:
    c = Config()
    assert c.bridge_port == 7891
    assert c.bridge_upstream == "https://opencode.ai/zen"
    assert c.bridge_retry_statuses == [429]
    assert c.bridge_retry_attempts == 1
    assert c.max_workers == 50


def test_config_validates_max_workers_bounds() -> None:
    with pytest.raises(ValidationError):
        Config(max_workers=0)
    with pytest.raises(ValidationError):
        Config(max_workers=501)
    assert Config(max_workers=2).max_workers == 2


def test_save_cleans_up_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "openrot.config.yaml.safe_dump",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        cfg.save_config(cfg.Config(), path)
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_config_validates_bridge_port_bounds() -> None:
    with pytest.raises(ValidationError):
        Config(bridge_port=0)
    with pytest.raises(ValidationError):
        Config(bridge_port=65536)
    assert Config(bridge_port=9000).bridge_port == 9000


def test_config_validates_port_bounds() -> None:
    with pytest.raises(ValidationError):
        Config(port=0)
    with pytest.raises(ValidationError):
        Config(port=65536)
    assert Config(port=1).port == 1
    assert Config(port=65535).port == 65535


def test_config_validate_assignment_rejects_bad_values() -> None:
    c = Config()
    with pytest.raises(ValidationError):
        c.port = 0
    with pytest.raises(ValidationError):
        c.health_interval = 0
    with pytest.raises(ValidationError):
        c.fail_threshold = 0
    assert c.port == 7890
    assert c.health_interval == 30
    assert c.fail_threshold == 3


def test_load_config_raises_config_error_on_bad_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("port: [unclosed")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_load_config_raises_config_error_on_bad_value(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("port: 999999\n")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_env_bad_port_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_PORT", "not-a-number")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_update_config_commits_mutation(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)

    def _mutate(c: Config) -> int:
        c.port = 1111
        return 42

    result = cfg.update_config(path, _mutate)
    assert result == 42
    assert cfg.load_config(path).port == 1111


def test_update_config_not_visible_until_commit(tmp_path: Path) -> None:
    """A mutator that raises must leave the persisted config unchanged."""
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)

    def _boom(c: Config) -> None:
        c.port = 3333
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cfg.update_config(path, _boom)
    assert cfg.load_config(path).port == 7890


def test_update_config_creates_missing_config(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    cfg.update_config(path, lambda c: c.__setattr__("port", 2222))
    assert cfg.load_config(path).port == 2222
