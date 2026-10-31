"""W63 出厂工作站 tools/factory.py:一条命令烧一台(远程烧写 → 取回申请 → 本机签 → 送回装上 →
出厂检验 → 贴纸文件拿回台式机、板子上的临时目录删掉)。ssh 换成假的。"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from d1max_contract import device_id as di
from d1max_contract import ed25519

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("factory_station", ROOT / "tools" / "factory.py")
fs = importlib.util.module_from_spec(spec)
sys.modules["factory_station"] = fs
spec.loader.exec_module(fs)

SCAN = "D1MAXB1.fake-sticker"


class 假板子:
    """记下发给板子的每条命令;回放烧写出的申请、贴纸;收下送过去的证书。"""

    def __init__(self, serial: str):
        self.dev = ed25519.new_seed()
        self.req = di.make_request(serial=serial, model="d1max",
                                   device_pub=ed25519.public_key(self.dev), hw_id="b" * 64)
        self.cmds: list[list[str]] = []
        self.sent: dict[str, bytes] = {}
        self.fail_on = ""

    def __call__(self, argv, *, input=None):
        self.cmds.append(list(argv))
        line = " ".join(argv)
        if self.fail_on and self.fail_on in line:
            return subprocess.CompletedProcess(argv, 1, b"", "烧写过了".encode())
        out = b""
        if argv[0] == "ssh" and "cat > " in line:
            self.sent[line.rsplit("cat > ", 1)[1].split()[0]] = input
        elif "device-request.json" in line and "cat " in line:
            out = json.dumps(self.req).encode()
        elif "tar " in line:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                data = f"序列号 {self.req['serial']}\n二维码 {SCAN}\n".encode()
                ti = tarfile.TarInfo(f"{self.req['serial']}.txt")
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
            out = buf.getvalue()
        return subprocess.CompletedProcess(argv, 0, out, b"")


@pytest.fixture
def 台(tmp_path):
    keys = tmp_path / "nexus-keys"
    keys.mkdir()
    seed = ed25519.new_seed()
    (keys / "factory.key").write_bytes(ed25519.private_pem(seed))
    (keys / "factory.key").chmod(0o600)
    (keys / "unbind-pub.pem").write_bytes(ed25519.public_pem(ed25519.public_key(seed)))
    return keys, ed25519.public_key(seed)


def test_一条命令烧一台_签在台式机_私钥不出门(台, tmp_path):
    keys, pub = 台
    board = 假板子("D1M-000200")
    out = tmp_path / "labels"
    got = fs.burn_one("10.0.0.7", "D1M-000200", keys=keys, out=out, user="robot", run=board)
    assert got["serial"] == "D1M-000200"
    sent = b"".join(board.sent.values())
    assert b"PRIVATE" not in sent, "出厂私钥不许送到板子上"
    certs = [json.loads(v) for v in board.sent.values() if b'"key_id"' in v]
    assert len(certs) == 1 and di.verify_cert(certs[0], [pub]).serial == "D1M-000200"
    lines = [" ".join(c) for c in board.cmds]
    order = [next(i for i, ln in enumerate(lines) if k in ln)
             for k in ("factory burn", "device-request.json", "install-cert", "tar ",
                       "factory check")]
    assert order == sorted(order), lines
    assert all(c[:2] == ["ssh", "robot@10.0.0.7"] for c in board.cmds)
    assert "rm -rf" in lines[-1], "贴纸文件里有绑定码、热点密码:板子上的临时目录最后删掉"
    assert SCAN in next(ln for ln in lines if "factory check" in ln)
    assert (out / "D1M-000200.txt").read_text().startswith("序列号 D1M-000200")
    assert (out / "D1M-000200.txt").stat().st_mode & 0o077 == 0
    rows = list(csv.DictReader((keys / "出厂台账.csv").open(encoding="utf-8")))
    assert rows[0]["serial"] == "D1M-000200"


def test_板子上烧写失败_停下_不签不装(台, tmp_path):
    keys, _ = 台
    board = 假板子("D1M-000201")
    board.fail_on = "factory burn"
    with pytest.raises(SystemExit, match="烧写"):
        fs.burn_one("10.0.0.8", "D1M-000201", keys=keys, out=tmp_path / "l", user="robot",
                    run=board)
    assert not any("install-cert" in " ".join(c) for c in board.cmds)
    assert not (keys / "出厂台账.csv").exists()
    assert "rm -rf" in " ".join(board.cmds[-1]), "失败了也清掉板子上的临时目录"


def test_板子回来的申请序列号不对_不签(台, tmp_path):
    keys, _ = 台
    board = 假板子("D1M-999999")
    with pytest.raises(SystemExit, match="序列号"):
        fs.burn_one("10.0.0.9", "D1M-000202", keys=keys, out=tmp_path / "l", user="robot",
                    run=board)
    assert not (keys / "出厂台账.csv").exists()


def test_出厂钥匙别人读得到_或者不在_不干(台, tmp_path):
    keys, _ = 台
    (keys / "factory.key").chmod(0o644)
    with pytest.raises(SystemExit, match="chmod 600"):
        fs.burn_one("h", "D1M-000203", keys=keys, out=tmp_path / "l", user="robot",
                    run=假板子("D1M-000203"))
    with pytest.raises(SystemExit, match="不在"):
        fs.burn_one("h", "D1M-000203", keys=tmp_path / "none", out=tmp_path / "l", user="robot",
                    run=假板子("D1M-000203"))


def test_开发样机_只补设备身份_不烧写不出贴纸(台, tmp_path):
    keys, pub = 台
    board = 假板子("C40221")
    got = fs.burn_one("10.0.0.5", "C40221", keys=keys, out=tmp_path / "l", user="robot",
                      run=board, identity_only=True)
    lines = [" ".join(c) for c in board.cmds]
    assert any("factory device-init --serial C40221" in ln for ln in lines)
    assert not any("factory burn" in ln or "tar " in ln or "factory check" in ln for ln in lines)
    assert any("install-cert" in ln for ln in lines) and "rm -rf" in lines[-1]
    certs = [json.loads(v) for v in board.sent.values() if b'"key_id"' in v]
    assert di.verify_cert(certs[0], [pub]).serial == "C40221" and got["labels"] == []
