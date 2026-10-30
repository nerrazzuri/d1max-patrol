"""配网服务用到的文件(都在 ``/etc/d1max`` 和数据根下;测试换成临时目录)。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 出厂写进 ``env`` 的缺省(站点下发地图以后代理以站点那张为准,这两行只是第一次起来时的缺省)。
FACTORY_ENV = {"D1MAX_SITE_MQTT": "", "D1MAX_SITE_NTP": "", "D1MAX_MAP": "unmapped:0",
               "D1MAX_HOME": "0,0,0"}


@dataclass(frozen=True)
class BindPaths:
    etc: Path = Path("/etc/d1max")
    data_root: Path = Path("/var/lib/d1max")
    #: 发件箱指到别的盘时(``D1MAX_OUTBOX``)清机也要清它。
    extra_data: tuple[Path, ...] = field(default_factory=tuple)

    @property
    def device(self) -> Path:
        return self.etc / "device.json"

    @property
    def bind_dir(self) -> Path:
        return self.etc / "bind"

    @property
    def state(self) -> Path:
        return self.bind_dir / "state.json"

    @property
    def used_tokens(self) -> Path:
        return self.bind_dir / "used-tokens.json"

    @property
    def tls_cert(self) -> Path:
        return self.bind_dir / "bind.crt"

    @property
    def tls_key(self) -> Path:
        return self.bind_dir / "bind.key"

    @property
    def unbind_pub(self) -> Path:
        return self.etc / "unbind-pub.pem"

    @property
    def env(self) -> Path:
        return self.etc / "env"

    @property
    def robot_cert(self) -> Path:
        return self.etc / "tls" / "robot.crt"


def write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    """先写临时文件再换名(断电不留半个文件);权限从写下去那一刻就是 ``mode``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
