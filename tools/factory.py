#!/usr/bin/env python3
"""出厂工作站(W63,``docs/出厂流程.md``):在公司台式机上**一条命令烧一台**上装板。

    .venv/bin/python tools/factory.py --board 192.168.1.2=D1M-000123 [--board 主机=序列号 ...] \\
        --keys ~/nexus-keys --out ~/factory-labels [--user robot]

经 SSH 对每块板依次做:
1. 送解绑公钥过去,板子上烧写(``factory burn``:绑定码、热点、配网证书、设备钥匙、出厂申请);
2. 取回出厂申请,**在台式机上**用出厂钥匙签(``factory sign-device``,记出厂台账 ``出厂台账.csv``);
3. 证书送回板子装上(``factory install-cert``:核是这块板的);
4. 贴纸打印文件拿回台式机(``--out/<序列号>.txt``,权限 600;打完删掉);
5. 出厂检验(``factory check``);
6. 板子上的临时目录删掉(失败了也删)。

开发样机(没走过烧写,如 C40221、C40011)加 ``--identity-only``:只补设备身份(第 1 步换成
``factory device-init``,没有第 4、5 步),序列号写它在站点上的编号。

出厂私钥只在台式机上,板子只拿到签好的证书。板子上要已经装好软件(``deploy/install.sh``),
SSH 账号能免密 sudo。
"""

from __future__ import annotations

import argparse
import io
import json
import shlex
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/contract/src", "src"):
    sys.path.insert(0, str(ROOT / _p))

#: 板子上的临时目录(root 独占;最后删掉)。
REMOTE_DIR = "/tmp/nw-factory"
#: 板子上跑我们代码的解释器(``deploy/install.sh`` 装的)。
REMOTE_PY = "/opt/d1max/bin/python"
REMOTE_REQUEST = "/etc/d1max/device/device-request.json"

Run = Callable[..., subprocess.CompletedProcess]


def _run(argv: list[str], *, input: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=input, capture_output=True, timeout=300)


class _Board:
    def __init__(self, host: str, user: str, run: Run, py: str) -> None:
        self.target = f"{user}@{host}" if user else host
        self.host = host
        self._run = run
        self.py = py

    def sh(self, cmd: str, *, what: str, input: bytes | None = None) -> bytes:
        r = self._run(["ssh", self.target, cmd], input=input)
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            raise SystemExit(f"{self.host}:{what}没成:{err or f'退出码 {r.returncode}'}")
        return r.stdout or b""

    def cli(self, args: list[str], *, what: str) -> bytes:
        return self.sh("sudo " + shlex.join([self.py, "-m", "d1max_patrol.cli", *args]), what=what)

    def put(self, path: str, data: bytes, *, what: str) -> None:
        self.sh("sudo sh -c " + shlex.quote(f"umask 077; cat > {path}"), what=what, input=data)


def _check_keys(keys: Path) -> tuple[Path, Path]:
    key, unbind = keys / "factory.key", keys / "unbind-pub.pem"
    for f in (key, unbind, keys / "factory-pub.pem"):
        if not f.is_file():
            raise SystemExit(f"{f} 不在(先 tools/make_keys.py;见 docs/钥匙生成与保管.md)")
    if stat.S_IMODE(key.stat().st_mode) & 0o077:
        raise SystemExit(f"出厂钥匙 {key} 别人也读得到:chmod 600")
    return key, unbind


def _sign(key: Path, req: dict[str, Any], workdir: Path, ledger: Path) -> dict[str, Any]:
    from d1max_patrol.bind.cli import _sign_devices
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    rp = workdir / f"{req['serial']}.request.json"
    rp.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    ns = argparse.Namespace(requests=[str(rp)], key=str(key), out_dir=str(workdir / "certs"),
                            ledger=str(ledger))
    if _sign_devices(ns) != 0:
        raise SystemExit(f"{req['serial']}:签出厂证书没成(见上面)")
    return json.loads((workdir / "certs" / f"{req['serial']}.json").read_text(encoding="utf-8"))


def _untar(raw: bytes, out: Path) -> list[Path]:
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    got = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as t:
        for m in t.getmembers():
            name = Path(m.name).name
            if not m.isfile() or not name or name.startswith("."):
                continue
            f = t.extractfile(m)
            if f is None:
                continue
            dst = out / name
            dst.write_bytes(f.read())
            dst.chmod(0o600)
            got.append(dst)
    return got


def burn_one(host: str, serial: str, *, keys: Path, out: Path, user: str = "robot",
             run: Run = _run, py: str = REMOTE_PY, identity_only: bool = False) -> dict[str, Any]:
    """烧一块板。出错抛 ``SystemExit``(说清楚哪一步);板子上的临时目录总会删掉。
    ``identity_only``:没走过烧写的老机器(开发样机),只补设备身份(``factory device-init``),
    不烧写绑定码、热点,不出贴纸。"""
    key, unbind = _check_keys(keys)
    b = _Board(host, user, run, py)
    d = REMOTE_DIR
    try:
        b.sh(f"sudo rm -rf {d} && sudo install -d -m 0700 {d}", what="建临时目录")
        if identity_only:
            b.cli(["factory", "device-init", "--serial", serial], what="补设备身份")
        else:
            b.put(f"{d}/unbind-pub.pem", unbind.read_bytes(), what="送解绑公钥")
            b.cli(["factory", "burn", "--serial", serial, "--unbind-pub", f"{d}/unbind-pub.pem",
                   "--out", f"{d}/label"], what="烧写")
        try:
            req = json.loads(b.sh(f"sudo cat {REMOTE_REQUEST}", what="取出厂申请"))
        except ValueError as exc:
            raise SystemExit(f"{host}:出厂申请读不懂:{exc}") from exc
        if not isinstance(req, dict) or req.get("serial") != serial:
            got = req.get("serial") if isinstance(req, dict) else None
            raise SystemExit(f"{host}:板子上的出厂申请序列号是 {got},不是 {serial}:没签")
        cert = _sign(key, req, out / ".work", keys / "出厂台账.csv")
        b.put(f"{d}/cert.json", json.dumps(cert, ensure_ascii=False).encode(), what="送出厂证书")
        # 出厂公钥一起送(外审 W63-2 第 2 条):板子上没装发行包里那份(远程升级上来的)也能核
        b.put(f"{d}/factory-pub.pem", (keys / "factory-pub.pem").read_bytes(), what="送出厂公钥")
        b.cli(["factory", "install-cert", f"{d}/cert.json", "--factory-pub",
               f"{d}/factory-pub.pem"], what="装出厂证书")
        if identity_only:                # 装的时候已经核过是这块板的;没有贴纸可核
            return {"host": host, "serial": serial, "labels": [], "key_id": cert["key_id"]}
        labels = _untar(b.sh(f"sudo tar -C {d}/label -cf - .", what="取贴纸文件"), out)
        scan = next((ln.split(" ", 1)[1] for f in labels if f.suffix == ".txt"
                     for ln in f.read_text(encoding="utf-8").splitlines()
                     if ln.startswith("二维码 ")), None)
        if scan is None:
            raise SystemExit(f"{host}:取回的贴纸文件里没有二维码那一行")
        b.cli(["factory", "check", "--scan", scan], what="出厂检验")
        return {"host": host, "serial": serial, "labels": [str(f) for f in labels],
                "key_id": cert["key_id"]}
    finally:
        try:
            b.sh(f"sudo rm -rf {d}", what="删临时目录")
        except SystemExit as exc:
            print(f"注意:{exc}(板子上 {d} 里有贴纸文件,手动删掉)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="出厂工作站:一条命令烧一台上装板(W63)")
    ap.add_argument("--board", action="append", required=True, metavar="主机=序列号",
                    help="要烧的板子(可以给几个,依次烧)")
    ap.add_argument("--keys", default=str(Path.home() / "nexus-keys"),
                    help="钥匙目录(要有 factory.key、unbind-pub.pem;台账也记在这里)")
    ap.add_argument("--out", required=True, help="贴纸打印文件放哪(台式机上;打完删掉)")
    ap.add_argument("--user", default="robot", help="SSH 账号(要能免密 sudo)")
    ap.add_argument("--identity-only", action="store_true",
                    help="没走过烧写的老机器(开发样机):只补设备身份,不烧写、不出贴纸;"
                         "序列号写它在站点上的编号")
    a = ap.parse_args(argv)
    boards = []
    for x in a.board:
        host, sep, serial = x.partition("=")
        if not sep or not host or not serial:
            ap.error(f"--board 要写成 主机=序列号:{x}")
        boards.append((host, serial))
    ok = 0
    for host, serial in boards:
        try:
            got = burn_one(host, serial, keys=Path(a.keys).expanduser(),
                           out=Path(a.out).expanduser(), user=a.user,
                           identity_only=a.identity_only)
        except SystemExit as exc:
            print(f"没成:{exc}", file=sys.stderr)
            continue
        ok += 1
        print(f"好了:{serial}({host})出厂证书已装、检验通过;贴纸文件 {'、'.join(got['labels'])}")
    print(f"共 {len(boards)} 块,成功 {ok} 块。贴纸打完把 {a.out} 里的文件删掉。")
    return 0 if ok == len(boards) else 1


if __name__ == "__main__":
    raise SystemExit(main())
