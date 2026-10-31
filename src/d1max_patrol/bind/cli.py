"""W42:``d1max-patrol bind-serve`` / ``factory burn|check|rekey`` / ``unbind-token keygen|sign``。
"""

from __future__ import annotations

import argparse
import json
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
    f.add_argument("--no-service", action="store_true", help=argparse.SUPPRESS)  # 演示:不碰 systemd
    fs = f.add_subparsers(dest="factory_command", required=True)
    b = fs.add_parser("burn", help="生成绑定码、热点、配网证书,写进机器人和路由器,出贴纸文件")
    paths_args(b)
    b.add_argument("--serial", required=True)
    b.add_argument("--unbind-pub", required=True, help="公司解绑公钥(PEM)")
    b.add_argument("--model", default="D1 Max")
    b.add_argument("--out", required=True, help="贴纸打印文件放哪(打完删掉)")
    b.add_argument("--force", action="store_true", help="烧写过了也重来(小心:旧贴纸作废)")
    # W63:芯片编号从哪儿读(测试、演示用;真板子上读设备树,不用给)
    b.add_argument("--hw-source", action="append", default=None, help=argparse.SUPPRESS)
    c = fs.add_parser("check", help="出厂检验:扫贴纸,跟机器人里存的核对;设备身份(W63)")
    paths_args(c)
    c.add_argument("--scan", required=True, help="扫贴纸二维码得到的那一串")
    c.add_argument("--hw-source", action="append", default=None, help=argparse.SUPPRESS)
    sd = fs.add_parser("sign-device", help="W63:用出厂钥匙签出厂申请(在公司台式机上跑)")
    sd.add_argument("requests", nargs="+", help="出厂申请 device-request.json(可以一次多份)")
    sd.add_argument("--key", required=True, help="出厂钥匙(~/nexus-keys/factory.key)")
    sd.add_argument("--out-dir", required=True, help="签好的出厂证书放哪(每台一份 <序列号>.json)")
    sd.add_argument("--ledger", default="", help="出厂台账(CSV,追加;公司内部保存,不进仓库)")
    ic = fs.add_parser("install-cert", help="W63:把签好的出厂证书装到这块板上(核是这块板的)")
    paths_args(ic)
    ic.add_argument("cert", help="出厂证书 <序列号>.json")
    ic.add_argument("--hw-source", action="append", default=None, help=argparse.SUPPRESS)
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
    # W57 签发规矩:每签一张记一行台账(公司内部保存,不进仓库)
    s.add_argument("--ledger", default="", help="台账文件(CSV,追加一行);给了就要写下面三项")
    s.add_argument("--requester", default="", help="申请人(客户哪位管理员)")
    s.add_argument("--checker", default="", help="核对人(公司谁核过销售记录、序列号)")
    s.add_argument("--purpose", default="", help="用途(比如:站点主机损坏无备份)")


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


def serve_main(argv: list[str] | None = None) -> int:
    """配网服务的入口(``deploy/d1max-bind-run``:root 独占目录里那份代码、系统 python ``-I`` 跑;
    参数同 ``d1max-patrol bind-serve``)。"""
    ap = argparse.ArgumentParser(prog="d1max-bind")
    add_args(ap.add_subparsers(dest="command", required=True))
    return run(ap.parse_args(["bind-serve", *(sys.argv[1:] if argv is None else argv)]))


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


SERVICE = "d1max-bind.service"


def _sign_devices(args: argparse.Namespace) -> int:
    """W63:公司台式机上,用出厂钥匙签一份或几份出厂申请;每签一台记一行出厂台账。"""
    import csv
    from datetime import datetime, timezone

    from d1max_contract import device_id as di
    from d1max_contract import ed25519
    try:
        seed = ed25519.seed_from_pem(Path(args.key).read_bytes())
    except (OSError, ValueError) as exc:
        print(f"没成:读不了出厂钥匙 {args.key}:{exc}", file=sys.stderr)
        return 1
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    bad = 0
    for r in args.requests:
        try:
            req = json.loads(Path(r).read_text(encoding="utf-8"))
            cert = di.sign_device(seed, req, now_ms=_now())
        except (OSError, ValueError) as exc:
            print(f"没签:{r}:{exc}", file=sys.stderr)
            bad += 1
            continue
        dst = out / f"{cert['serial']}.json"
        dst.write_text(json.dumps(cert, ensure_ascii=False, indent=1), encoding="utf-8")
        if args.ledger:
            led = Path(args.ledger)
            new = not led.exists()
            with led.open("a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["utc", "serial", "model", "hw_id", "device_pub", "key_id"])
                w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            cert["serial"], cert["model"], cert["hw_id"], cert["device_pub"],
                            cert["key_id"]])
        print(f"签好了:{cert['serial']} → {dst}")
    return 1 if bad else 0


def _factory(args: argparse.Namespace) -> int:
    from d1max_contract.binding import pretty_code
    from d1max_patrol.bind import devid
    from d1max_patrol.bind import factory as fa
    from d1max_patrol.bind import wipe as wp
    if args.factory_command == "sign-device":
        return _sign_devices(args)
    paths = _paths(args)
    hw = [Path(x) for x in args.hw_source] if getattr(args, "hw_source", None) else None
    hw_kw = {"hw_sources": hw} if hw is not None else {}
    try:
        if args.factory_command == "install-cert":
            devid.install_cert(paths, devid.load_cert_file(Path(args.cert)),
                               factory_pubs=devid.factory_pubs(paths), **hw_kw)
            print(f"装好了:出厂证书 → {devid.cert_path(paths)}")
            return 0
        if args.factory_command == "check":
            bad = fa.check(paths, args.scan)
            pubs = devid.factory_pubs(paths)
            bad += devid.problems(paths, factory_pubs=pubs, **hw_kw) if pubs else \
                ["这台没有出厂公钥(/etc/d1max/factory-pub.pem):先装发行包"]
            for line in bad:
                print(f"  FAIL  {line}")
            if not bad:
                print("  PASS  贴纸跟机器人里存的对得上;出厂证书是这块板的、核得过")
            return 1 if bad else 0
        router = _router(args.router)
        svc = not args.no_service
        if args.factory_command == "burn":
            st = fa.burn(paths, args.serial, router=router, unbind_pub=Path(args.unbind_pub),
                         model=args.model, force=args.force, **hw_kw)
            if svc and wp._run(["systemctl", "enable", "--now", SERVICE]) != 0:
                print(f"注意:{SERVICE} 没起来(systemctl enable --now {SERVICE} 看原因)")
        else:
            # 外审 I10 / 2-E:配网服务在跑的话它记着旧绑定码、旧证书:先停(停不下就不换),
            # 换完再起(起不来算没成:新贴纸要它起来才能用)
            was = svc and wp._run(["systemctl", "is-active", "--quiet", SERVICE]) == 0
            if was and (wp._run(["systemctl", "stop", SERVICE]) != 0
                        or wp._run(["systemctl", "is-active", "--quiet", SERVICE]) == 0):
                print(f"没成:{SERVICE} 停不下来,没换贴纸(旧贴纸照旧能用)", file=sys.stderr)
                return 1
            st = fa.rekey(paths, router=router)
            if was and wp._run(["systemctl", "start", SERVICE]) != 0:
                files = fa.label_files(st, Path(args.out))
                print(f"贴纸已经换了(打印文件:{'、'.join(map(str, files))}),但 {SERVICE} 没起来:"
                      f"systemctl start {SERVICE} 起来以后新贴纸才能用", file=sys.stderr)
                return 1
    except (fa.FactoryError, devid.DevIdError) as exc:
        print(f"没成:{exc}", file=sys.stderr)
        return 1
    files = fa.label_files(st, Path(args.out))
    print(f"好了:序列号 {st.serial},绑定码 {pretty_code(st.code)},热点 {st.hotspot_ssid}")
    print("贴纸打印文件(**打完删掉**):" + "、".join(str(p) for p in files))
    print("二维码图在生产线电脑上出:python tools/sticker_label.py <上面那个 .txt>")
    if args.factory_command == "burn":
        print(f"出厂申请(W63,拿到台式机上用出厂钥匙签):{devid.request_path(paths)}")
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
    if args.ledger and not (args.requester and args.checker and args.purpose):
        print("记台账要写 --requester、--checker、--purpose", file=sys.stderr)
        return 1
    try:
        seed = ed25519.seed_from_pem(Path(args.private).read_bytes())
        tok = sign_unbind(seed, args.serial, now_ms=_now(), hours=args.hours)
    except (OSError, ValueError, BindingError) as exc:
        print(f"没签成:{exc}", file=sys.stderr)
        return 1
    if args.ledger:
        _ledger_row(Path(args.ledger), args, tok, ed25519.public_key(seed))
    print(tok)
    return 0


def _ledger_row(path: Path, args: argparse.Namespace, tok: str, pub: bytes) -> None:
    """台账一行:什么时候、哪台、谁申请、谁核对、为什么、什么时候过期、这张的随机号、哪把钥匙。"""
    import csv
    from datetime import datetime, timezone

    from d1max_contract.binding import key_id, verify_unbind
    t = verify_unbind(tok, pub, serial=args.serial, now_ms=_now())
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["utc", "serial", "requester", "checker", "purpose", "expires_utc",
                        "nonce", "key_id"])
        utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
        exp = datetime.fromtimestamp(t.expires_ms / 1000, timezone.utc).isoformat(
            timespec="seconds")
        w.writerow([utc, args.serial, args.requester, args.checker, args.purpose, exp, t.nonce,
                    key_id(pub)])
