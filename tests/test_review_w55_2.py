"""外审 W55-2(`reviews/W55-外审-2.md`)里 W42 配网、装机脚本那几条老问题:
C6 开通码 / env 注入、C7 清机跟着顶层链接删、C8 装机脚本以 root 执行机器人账号能改的解释器。"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _code(**d) -> str:
    base = {"api": "https://s:8443", "fp": "ab", "mqtt": "mqtts://172.29.70.1:8883", "id": "x-A",
            "t": "tok"}
    base.update(d)
    return "D1MAX1." + base64.urlsafe_b64encode(json.dumps(base).encode()).decode().rstrip("=")


@pytest.mark.parametrize("mqtt", ["mqtts://h:8883\nLD_PRELOAD=/tmp/x.so", "mqtts://h:8883 x",
                                  "http://h:1", "mqtts://h"])
def test_C6_开通码里的消息服务地址不对_拒(mqtt):
    from d1max_patrol.provision import ProvisionError, decode
    with pytest.raises(ProvisionError, match="消息服务地址"):
        decode(_code(mqtt=mqtt))
    assert decode(_code())["mqtt"] == "mqtts://172.29.70.1:8883"


def test_C6_env_的值带换行_拒(tmp_path):
    from d1max_patrol.provision import set_env
    env = tmp_path / "env"
    env.write_text("A=1\n")
    with pytest.raises(ValueError):
        set_env(env, "D1MAX_SITE_MQTT", "x\nLD_PRELOAD=/tmp/evil.so")
    with pytest.raises(ValueError):
        set_env(env, "LD PRELOAD", "x")
    assert env.read_text() == "A=1\n"
    set_env(env, "D1MAX_SITE_MQTT", "mqtts://h:8883")
    assert "D1MAX_SITE_MQTT=mqtts://h:8883" in env.read_text()


def test_C7_清机_要清的目录被换成链接_不删过去(tmp_path):
    from d1max_patrol.bind.wipe import _clear_dir
    keep = tmp_path / "system"
    keep.mkdir()
    (keep / "important").write_text("x")
    parent = tmp_path / "ssd"
    parent.mkdir()
    (parent / "outbox").symlink_to(keep)
    with pytest.raises(OSError):
        _clear_dir(parent / "outbox")
    assert (keep / "important").exists()


def test_C7_清机_上级目录是链接也不跟(tmp_path):
    from d1max_patrol.bind.wipe import _clear_dir
    real = tmp_path / "real"
    (real / "outbox").mkdir(parents=True)
    (real / "outbox" / "f").write_text("x")
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(OSError):
        _clear_dir(tmp_path / "link" / "outbox")
    assert (real / "outbox" / "f").exists()


def test_C7_清机_正常目录里的东西都删掉_链接只删链接(tmp_path):
    from d1max_patrol.bind.wipe import _clear_dir
    d = tmp_path / "data"
    (d / "runs" / "a").mkdir(parents=True)
    (d / "runs" / "a" / "x").write_text("1")
    (d / "q.jsonl").write_text("1")
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep").write_text("k")
    (d / "runs" / "evil").symlink_to(other)
    _clear_dir(d)
    assert d.is_dir() and not list(d.iterdir())
    assert (other / "keep").exists()


def test_C8_装机脚本_root不执行机器人账号的解释器和pip():
    text = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    for i, ln in enumerate(code):
        s = ln.strip()
        if re.match(r'^"?\$ROOT/(bin/python|bin-venv/bin/)', s) or "-m venv" in s:
            prev = code[i - 1].strip() if i else ""
            assert s.startswith("sudo -u") or prev.endswith("\\") and "sudo -u" in prev, ln
    assert 'sudo -u "$RUN_USER" tee "$ROOT/bin/python"' in text
