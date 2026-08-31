"""YAML 配置加载。严格校验:拼错的键必须报错,不能静默退回默认值。"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig, NavConfig

#: nav 小节的字段类型表。不用 dataclasses.fields() 推断,因为
#: `from __future__ import annotations` 会让 Field.type 变成字符串。
_NAV_FIELD_TYPES: dict[str, type] = {
    "url": str,
    "request_timeout_s": float,
    "connect_timeout_s": float,
    "status_poll_interval_s": float,
    "reconnect_min_s": float,
    "reconnect_max_s": float,
}

_TOP_LEVEL_KEYS = frozenset({"nav", "runs_dir"})

#: 环境变量覆盖表: 变量名 -> (小节名, 字段名)
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "D1MAX_NAV_URL": ("nav", "url"),
}


class ConfigError(Exception):
    """配置文件缺失、含未知键或字段类型不符。"""


def _coerce(section: str, name: str, value: Any, target: type) -> Any:
    if target is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{section}.{name} 应为数字,实际为 {value!r}")
        return float(value)
    if target is str:
        if not isinstance(value, str):
            raise ConfigError(f"{section}.{name} 应为字符串,实际为 {value!r}")
        return value
    raise ConfigError(f"不支持的字段类型 {target!r}（{section}.{name}）")


def _build_nav(raw: Any) -> NavConfig:
    if raw is None:
        return NavConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"nav 小节应为映射,实际为 {raw!r}")
    unknown = sorted(set(raw) - set(_NAV_FIELD_TYPES))
    if unknown:
        raise ConfigError(f"nav 下未知配置项: {', '.join(unknown)}")
    kwargs = {
        name: _coerce("nav", name, value, _NAV_FIELD_TYPES[name])
        for name, value in raw.items()
    }
    return NavConfig(**kwargs)


def load_config(path: str | Path | None) -> AppConfig:
    """读取配置文件;`path` 为 None 时返回全默认配置。"""
    raw: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"配置文件不存在: {p}")
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"配置文件顶层应为映射: {p}")
        raw = loaded

    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(f"未知配置项: {', '.join(unknown)}")

    nav = _build_nav(raw.get("nav"))
    runs_dir = raw.get("runs_dir", "runs")
    if not isinstance(runs_dir, str):
        raise ConfigError(f"runs_dir 应为字符串,实际为 {runs_dir!r}")

    return _apply_env(AppConfig(nav=nav, runs_dir=runs_dir))


def _apply_env(cfg: AppConfig) -> AppConfig:
    nav = cfg.nav
    for env_name, (section, field_name) in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is None:
            continue
        if section == "nav":
            nav = dataclasses.replace(nav, **{field_name: value})
    return dataclasses.replace(cfg, nav=nav)
