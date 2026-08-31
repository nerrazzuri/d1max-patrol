"""配置加载的行为约定:默认可用、显式覆盖、拼错即报错。"""

import pytest

from d1max_patrol.config.loader import ConfigError, load_config
from d1max_patrol.config.models import AppConfig, NavConfig


def test_不给路径时返回全默认配置():
    cfg = load_config(None)
    assert isinstance(cfg, AppConfig)
    assert cfg.nav.url == "ws://192.168.144.100:10010"
    assert cfg.nav.request_timeout_s == 5.0
    assert cfg.nav.status_poll_interval_s == 0.5
    assert cfg.runs_dir == "runs"


def test_yaml_覆盖部分字段其余保持默认(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "nav:\n"
        "  url: ws://127.0.0.1:19999\n"
        "  request_timeout_s: 2.5\n"
        "runs_dir: /data/runs\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.nav.url == "ws://127.0.0.1:19999"
    assert cfg.nav.request_timeout_s == 2.5
    assert cfg.nav.connect_timeout_s == 5.0
    assert cfg.runs_dir == "/data/runs"


def test_顶层未知键报错(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("bridge:\n  port: 8790\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="未知配置项: bridge"):
        load_config(p)


def test_nav_下未知键报错(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("nav:\n  urls: ws://x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="nav 下未知配置项: urls"):
        load_config(p)


def test_数字字段给字符串报错(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("nav:\n  request_timeout_s: fast\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="request_timeout_s"):
        load_config(p)


def test_空文件等价于全默认(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("", encoding="utf-8")
    assert load_config(p) == load_config(None)


def test_文件不存在报错(tmp_path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(tmp_path / "nope.yaml")


def test_环境变量覆盖_url(monkeypatch):
    monkeypatch.setenv("D1MAX_NAV_URL", "ws://10.0.0.1:10010")
    cfg = load_config(None)
    assert cfg.nav.url == "ws://10.0.0.1:10010"


def test_配置对象不可变():
    cfg = load_config(None)
    with pytest.raises(AttributeError):
        cfg.nav.url = "ws://other"  # type: ignore[misc]


def test_navconfig_可独立构造():
    nav = NavConfig(url="ws://a:1")
    assert nav.reconnect_min_s == 1.0
    assert nav.reconnect_max_s == 15.0
