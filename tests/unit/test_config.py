from pathlib import Path

import pytest
from pydantic import ValidationError

from openrot import config as cfg
from openrot.models import Config


def test_listen_address_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROT_LISTEN", raising=False)
    assert cfg.listen_address() == "127.0.0.1"
    monkeypatch.setenv("OPENROT_LISTEN", "0.0.0.0")
    assert cfg.listen_address() == "0.0.0.0"


def test_load_creates_default_config_and_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    c = cfg.load_config(path)
    assert c.bridge_port == 7891
    assert c.health_interval == 30
    assert c.active_level.value == "none"
    assert path.exists()


def test_save_load_roundtrip_preserves_model(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    original = cfg.Config(
        health_interval=15,
        bridge_port=9001,
        bridge_upstream="https://example.test/v1",
    )
    cfg.save_config(original, path)
    loaded = cfg.load_config(path)

    assert loaded.health_interval == 15
    assert loaded.bridge_port == 9001
    assert loaded.bridge_upstream == "https://example.test/v1"


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


def test_env_overrides_retry_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_RETRY_STATUSES", "429,503")
    monkeypatch.setenv("OPENROT_BRIDGE_RETRY_ATTEMPTS", "3")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    loaded = cfg.load_config(path)
    assert loaded.bridge_retry_statuses == [429, 503]
    assert loaded.bridge_retry_attempts == 3


def test_env_bad_retry_attempts_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_RETRY_ATTEMPTS", "not-a-number")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_env_overrides_bridge_min_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_MIN_INTERVAL", "1.5")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    loaded = cfg.load_config(path)
    assert loaded.bridge_min_interval == 1.5


def test_env_bad_bridge_min_interval_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_MIN_INTERVAL", "not-a-number")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_env_bridge_inject_session_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_INJECT_SESSION", "false")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    assert cfg.load_config(path).bridge_inject_session is False


def test_env_bad_bridge_inject_session_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROT_BRIDGE_INJECT_SESSION", "not-a-bool")
    path = tmp_path / "config.yaml"
    cfg.save_config(cfg.Config(), path)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_config_bridge_defaults() -> None:
    c = Config()
    assert c.bridge_port == 7891
    assert c.bridge_upstream == "https://opencode.ai/zen"
    assert c.bridge_retry_statuses == [429]
    assert c.bridge_retry_attempts == 1
    assert c.bridge_max_concurrent == 3
    assert c.bridge_min_interval == 0.5
    assert c.bridge_inject_session is True


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


def test_config_validate_assignment_rejects_bad_values() -> None:
    c = Config()
    with pytest.raises(ValidationError):
        c.bridge_port = 0
    with pytest.raises(ValidationError):
        c.health_interval = 0
    assert c.bridge_port == 7891
    assert c.health_interval == 30


def test_load_config_raises_config_error_on_bad_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("health_interval: [unclosed")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_load_config_raises_config_error_on_bad_value(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("bridge_port: 999999\n")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)
