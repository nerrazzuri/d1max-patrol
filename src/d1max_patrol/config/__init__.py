"""配置加载。"""

from .loader import ConfigError, load_config
from .models import AppConfig, NavConfig

__all__ = ["AppConfig", "ConfigError", "NavConfig", "load_config"]
