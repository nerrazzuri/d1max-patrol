"""W42:``d1max-patrol bind-serve`` / ``factory burn|check|rekey`` / ``unbind-token keygen|sign``。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from d1max_patrol.bind.paths import BindPaths
from d1max_patrol.bind.router import FakeRouter, Router


def add_args(sub: argparse._SubParsersAction) -> None:
    def paths_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--etc", default="/etc/d1max", help=argparse.SUPPRESS)
        p.add_argument("--data-root", default=os.environ.get("D1MAX_DATA_ROOT", "/var/lib/d1max"),
                       help=argparse.SUPPRESS)
        p.add_argument("--router", default="fake",
                       help="路由器:fake(假的,演示 / 测试)、fake:<状态文件>;G806w 等硬件答复")

    p = sub.add_parser("bind-serve", help="配网服务(W42,以 root 跑,热点那一侧 + 代理套接字)")
    paths_args(p)
    p.add_argument("--host", required=True, help="只绑这个地址(路由器 LAN1 那一侧)")
    p.add_argument("--port", type=int, default=8450)
    p.add_argument("--sock", default="/run/d1max/bind.sock")
    p.add_argument("--group", default=os.environ.get("D1MAX_USER", "robot"),
                   help="代理那个账号的组(能连套接字)")
    p.add_argument("--outbox", default=os.environ.get("D1MAX_OUTBOX", ""),
                   help="发件箱在别的盘上时给出来(清机一起清)")
    p.add_argument("--user", default=os.environ.get("D1MAX_USER", "robot"),
                   help="代理用哪个账号跑(证书包的属主)")
    # 演示站点:不碰系统服务、日志
    p.add_argument("--dry-system", action="store_true", help=argparse.SUPPRESS)

    f = sub.add_parser("factory", help="出厂:烧写、检验、重做贴纸(W42,生产线上以 root 跑)")
    fs = f.add_subparsers(dest="factory_command", required=True)
    b = fs.add_parser("burn", help="生成绑定码、热点、配网证书,写进机器人和路由器,出贴纸文件")
    paths_args(b)
    b.add_argument("--serial", required=True)
    b.add_argument("--unbind-pub", required=True, help="公司解绑公钥(PEM)")
    b.add_argument("--model", default="D1 Max")
    b.add_argument("--out", required=True, help="贴纸打印文件放哪(打完删掉)")
    b.add_argument("--force", action="store_true", help="烧写过了也重来(小心:旧贴纸作废)")
    c = fs.add_parser("check", help="出厂检验:扫贴纸,跟机器人里存的核对")
    paths_args(c)
    c.add_argument("--scan", required=True, help="扫贴纸二维码得到的那一串")
    r = fs.add_parser("rekey", help="贴纸坏了重做(只能在没绑定的时候)")
    paths_args(r)
    r.add_argument("--out", required=True)

    u = sub.add_parser("unbind-token", help="公司解绑令(W42 §7,在公司的离线签名机上跑)")
    us = u.add_subparsers(dest="unbind_command", required=True)
    k = us.add_parser("keygen", help="生成解绑钥匙(跟发行钥匙分开)")
    k.add_argument("--private", required=True)
    k.add_argument("--public", required=True)
    s = us.add_parser("sign", help="给一台机器人签一张解绑令")
    s.add_argument("--private", required=True)
    s.add_argument("--serial", required=True)
    s.add_argument("--hours", type=float, default=24.0)


def _paths(args: argparse.Namespace) -> BindPaths:
    extra = (Path(args.outbox),) if getattr(args, "outbox", "") else ()
    return BindPaths(etc=Path(args.etc), data_root=Path(args.data_root), extra_data=extra)


def _router(spec: str) -> Router:
    if spec == "fake":
        return FakeRouter(reachable=None)
    if spec.startswith("fake:"):
        return FakeRouter(reachable=None, path=Path(spec[5:]))
    raise SystemExit(f"不认识的路由器 {spec!r}(G806w 那份等硬件答复,W42 §8)")


def _now() -> int:
    return int(time.time() * 1000)


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.command == "bind-serve":
        return _serve(args)
    if args.command == "factory":
        return _factory(args)
    return _unbind_token(args)


def _serve(args: argparse.Namespace) -> int:
    from d1max_patrol import __version__
    from d1max_patrol import provision as pv
    from d1max_patrol.bind import wipe as wp
    from d1max_patrol.bind.core import BindCore
    from d1max_patrol.bind.server import serve
    from d1max_patrol.bind.sitelink import SiteLink
    paths, router = _paths(args), _router(args.router)
    user = args.user or None
    if args.dry_system:                       # 演示站点(tools/site_demo.py):代理由演示脚本起、收

        def sysrun(cmd: object) -> int:
            return 0
    else:
        sysrun = wp._run
    core = BindCore(paths, router=router, site=SiteLink(),
                    provision=lambda code: pv.provision(code, etc=paths.etc, user=user,
                                                        force=True),
                    wipe=lambda: wp.wipe(paths, router=router, run=sysrun),
                    start_services=lambda: wp.start_services(sysrun), now_ms=_now,
                    version=__version__)
    serve(core, host=args.host, port=args.port, sock=Path(args.sock), group=args.group)
    return 0


def _factory(args: argparse.Namespace) -> int:
    from d1max_contract.binding import pretty_code
    from d1max_patrol.bind import factory as fa
    paths = _paths(args)
    try:
        if args.factory_command == "check":
            bad = fa.check(paths, args.scan)
            for line in bad:
                print(f"  FAIL  {line}")
            if not bad:
                print("  PASS  贴纸跟机器人里存的对得上")
            return 1 if bad else 0
        router = _router(args.router)
        if args.factory_command == "burn":
            st = fa.burn(paths, args.serial, router=router, unbind_pub=Path(args.unbind_pub),
                         model=args.model, force=args.force)
        else:
            st = fa.rekey(paths, router=router)
    except fa.FactoryError as exc:
        print(f"没成:{exc}", file=sys.stderr)
        return 1
    files = fa.label_files(st, Path(args.out))
    print(f"好了:序列号 {st.serial},绑定码 {pretty_code(st.code)},热点 {st.hotspot_ssid}")
    print("贴纸打印文件(**打完删掉**):" + "、".join(str(p) for p in files))
    print("二维码图在生产线电脑上出:python tools/sticker_label.py <上面那个 .txt>")
    return 0


def _unbind_token(args: argparse.Namespace) -> int:
    from d1max_contract import ed25519
    from d1max_contract.binding import BindingError, sign_unbind
    if args.unbind_command == "keygen":
        priv, pub = Path(args.private), Path(args.public)
        if priv.exists():
            print(f"{priv} 已经有了:不覆盖", file=sys.stderr)
            return 1
        seed = ed25519.new_seed()
        old = os.umask(0o077)
        try:
            priv.write_bytes(ed25519.private_pem(seed))
        finally:
            os.umask(old)
        pub.write_bytes(ed25519.public_pem(ed25519.public_key(seed)))
        print(f"私钥 {priv}(只留在离线签名机上),公钥 {pub}(烧写时装进每台机器人)")
        return 0
    try:
        seed = ed25519.seed_from_pem(Path(args.private).read_bytes())
        tok = sign_unbind(seed, args.serial, now_ms=_now(), hours=args.hours)
    except (OSError, ValueError, BindingError) as exc:
        print(f"没签成:{exc}", file=sys.stderr)
        return 1
    print(tok)
    return 0
