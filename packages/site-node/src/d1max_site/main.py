"""``d1max-site``:站点的命令行(W00c1)。

站点目录(``--home``,默认 ``/var/lib/d1max-site``)的布局::

    site.json          site_id、broker 端口、主机名
    site.db            SQLite:注册表、账号、会话、命令、事件
    ca/                站点 CA(ca.key 0600)、签发过的证书、CRL;ca/server/ 是站点服务证书
    broker/            mosquitto.conf 与 acl(由 broker_conf 生成),d1max-mosquitto.service 用它

子命令::

    init      --site-id S --hostname H [--hostname …] [--broker-port 8883]
    enroll    ROBOT_ID [--days 365]        → 打印证书包目录(拷到狗的 /etc/d1max/)
    revoke    ROBOT_ID                     → 吊销;要重启 d1max-mosquitto 才对 broker 生效
    add-admin NAME                         → 口令从 D1MAX_SITE_PASSWORD 或交互输入
    import-bundle DIR                      → 导入任务包,成为当前包(W00c2a)
    standby ROBOT NAME --map M:VER --pose x,y,yaw [--default] → 登记待命点(W00c2b)
    source-add NAME                        → 登记事件源,打印共享密钥(只这一次;W00c2c)
    intercept NAME --map M:VER --pose x,y,yaw → 登记拦截点(W00c2c)
    zone ZONE INTERCEPT                    → 防区映射到拦截点(W00c2c)
    serve     [--api-host 127.0.0.1] [--api-port 8443] [--broker mqtts://127.0.0.1:8883]
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from d1max_site import SITE_VERSION
from d1max_site.accounts import Accounts, AuthError
from d1max_site.broker_conf import BrokerPaths, render_acl, render_conf
from d1max_site.ca import CAError, SiteCA, site_principal
from d1max_site.db import SiteDB
from d1max_site.registry import Registry, RegistryError

log = logging.getLogger(__name__)

DEFAULT_HOME = Path("/var/lib/d1max-site")
SYNC_PERIOD_S = 10.0


def wall_ms() -> int:
    return int(time.time() * 1000)


class SiteError(RuntimeError):
    pass


def _load(home: Path) -> dict:
    try:
        return json.loads((home / "site.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SiteError(f"{home} 还没 init(读不到 site.json): {exc}") from exc


# ------------------------------------------------------------ 子命令


def _check_owner(home: Path) -> None:
    """命令行必须以站点目录的属主跑(安装脚本里是 d1max-site)。root 跑的话,重写出来的
    CRL、数据库都成了 root 的 0600,以 d1max-site 跑的 broker 与站点进程重启后读不了。"""
    if home.exists() and os.geteuid() != home.stat().st_uid:
        raise SiteError(f"要以 {home} 的属主跑(uid {home.stat().st_uid}),比如 "
                        f"sudo -u d1max-site d1max-site …;现在是 uid {os.geteuid()}")


def cmd_init(home: Path, site_id: str, hostnames: list[str], broker_port: int) -> None:
    if (home / "site.json").exists():
        raise SiteError(f"{home} 已经 init 过了")
    if (home / "ca").exists():
        raise SiteError(f"{home} 里有上一次 init 没做完留下的 ca/(没有 site.json)。确认这个站点"
                        f"还没给任何狗签过证书之后,删掉 {home}/ca 再 init;签过的话别删,找人处理")
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o750)
    ca = SiteCA(home / "ca")
    ca.init(site_id)
    crt, key = ca.issue_server(hostnames)
    SiteDB(home / "site.db").close()
    broker = home / "broker"
    broker.mkdir(exist_ok=True)
    (broker / "acl").write_text(render_acl(site_id), encoding="utf-8")
    (broker / "mosquitto.conf").write_text(render_conf(port=broker_port, paths=BrokerPaths(
        cafile=ca.ca_cert, certfile=crt, keyfile=key, crlfile=ca.crl,
        aclfile=broker / "acl", persistence_dir=broker / "data")), encoding="utf-8")
    (broker / "data").mkdir(exist_ok=True)
    (home / "site.json").write_text(json.dumps({
        "site_id": site_id, "broker_port": broker_port, "hostnames": hostnames,
        "version": SITE_VERSION}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_enroll(home: Path, robot_id: str, days: int) -> Path:
    cfg = _load(home)
    ca = SiteCA(home / "ca")
    db = SiteDB(home / "site.db")
    try:
        reg = Registry(db, site_id=cfg["site_id"])
        cur = reg.get(robot_id)
        if cur is not None and not cur.revoked:
            raise SiteError(f"{robot_id} 已登记且未吊销;先 revoke")
        bundle = ca.issue_robot(robot_id, days=days, now_ms=wall_ms())
        r = bundle.registration
        reg.enroll(robot_id, fingerprint=bundle.fingerprint, issued_at=r.issued_at,
                   expires_at=r.expires_at, now_ms=wall_ms())
        return bundle.dir
    finally:
        db.close()


def cmd_revoke(home: Path, robot_id: str) -> str:
    """注册表先吊销(派遣器立刻不再派单),CA 后吊销(进 CRL,broker 重启后拒连)。

    - 某一侧**本来就没有**这台狗(enroll 半路失败留下的半状态):跳过那一侧,照做另一侧。
    - 某一侧**有**、但吊销执行失败(openssl 出错、CRL 写不进、库被锁……):抛 ``SiteError``,
      消息里说清楚哪一侧做完了、哪一侧失败了。**不许**部分成功却报成功:CRL 没更新的话,
      重启 broker 之后那张证书照样能连。
    - 两侧都没有:抛 ``SiteError``。

    成功时返回一句给人看的总结。"""
    cfg = _load(home)
    ca = SiteCA(home / "ca")
    db = SiteDB(home / "site.db")
    done: list[str] = []
    failed: list[str] = []
    try:
        reg = Registry(db, site_id=cfg["site_id"])
        reg_present = reg.get(robot_id) is not None
        ca_present = ca.has_valid(robot_id)
        if not reg_present and not ca_present:
            raise SiteError(f"注册表与 CA 里都没有 {robot_id}(或它的证书早已吊销)")
        if reg_present:
            try:
                reg.revoke(robot_id)
                done.append("注册表已吊销")
            except Exception as exc:  # noqa: BLE001 - 哪一侧失败都要报出来
                failed.append(f"注册表吊销失败: {exc}")
        else:
            done.append("注册表里本来就没有(跳过)")
        if ca_present:
            try:
                ca.revoke(robot_id)
                done.append("CRL 已更新")
            except Exception as exc:  # noqa: BLE001
                failed.append(f"CRL 更新失败: {exc}")
        else:
            done.append("CA 里没有有效证书(跳过)")
    finally:
        db.close()
    summary = ";".join(done)
    if failed:
        raise SiteError(f"{robot_id} 只吊销了一部分:{summary or '无'};" + ";".join(failed)
                        + "。修好之后再跑一次 revoke(已完成的那一侧会被跳过或重做,不会出错)")
    return summary


def cmd_import_bundle(home: Path, bundle_dir: Path, imported_by: str) -> dict:
    from d1max_site.catalog import CatalogError, import_bundle
    _load(home)
    db = SiteDB(home / "site.db")
    try:
        return import_bundle(db, bundle_dir, imported_by=imported_by, now_ms=wall_ms())
    except CatalogError as exc:
        raise SiteError(str(exc)) from exc
    finally:
        db.close()


def cmd_standby(home: Path, robot_id: str, name: str, map_id: str, pose: str,
                default: bool) -> None:
    from d1max_site.dispatcher import Dispatcher
    from d1max_site.standby import StandbyError, StandbyManager
    cfg = _load(home)
    try:
        x, y, yaw = (float(v) for v in pose.split(","))
    except ValueError as exc:
        raise SiteError(f"--pose 要写成 x,y,yaw: {pose!r}") from exc
    mid, sep, ver = map_id.rpartition(":")
    if not sep or not mid or not ver:
        raise SiteError(f"--map 要写成 <map_id>:<version>: {map_id!r}")
    db = SiteDB(home / "site.db")
    try:
        disp = Dispatcher(transport=None, db=db,  # type: ignore[arg-type] - 只用注册表
                          registry=Registry(db, site_id=cfg["site_id"]), now_ms=wall_ms)
        StandbyManager(db, disp, now_ms=wall_ms).set(robot_id, name, map_id=mid, map_version=ver,
                                                      x=x, y=y, yaw=yaw, default=default or None)
    except StandbyError as exc:
        raise SiteError(str(exc)) from exc
    finally:
        db.close()


def _desk(home: Path, db: SiteDB):
    from d1max_site.dispatcher import Dispatcher
    from d1max_site.incidents import IncidentDesk
    cfg = _load(home)
    disp = Dispatcher(transport=None, db=db,  # type: ignore[arg-type] - 只用注册表与库
                      registry=Registry(db, site_id=cfg["site_id"]), now_ms=wall_ms)
    return IncidentDesk(db, disp, now_ms=wall_ms)


def cmd_incident_admin(home: Path, what: str, **kw) -> str:
    from d1max_site.incidents import IncidentError
    _load(home)
    db = SiteDB(home / "site.db")
    try:
        desk = _desk(home, db)
        if what == "source":
            return desk.add_source(kw["name"])
        if what == "intercept":
            mid, sep, ver = kw["map"].rpartition(":")
            if not sep or not mid or not ver:
                raise SiteError(f"--map 要写成 <map_id>:<version>: {kw['map']!r}")
            try:
                x, y, yaw = (float(v) for v in kw["pose"].split(","))
            except ValueError as exc:
                raise SiteError(f"--pose 要写成 x,y,yaw: {kw['pose']!r}") from exc
            desk.set_intercept(kw["name"], map_id=mid, map_version=ver, x=x, y=y, yaw=yaw)
            return ""
        desk.map_zone(kw["zone"], kw["intercept"])
        return ""
    except IncidentError as exc:
        raise SiteError(str(exc)) from exc
    finally:
        db.close()


def cmd_add_admin(home: Path, name: str, password: str) -> None:
    _load(home)
    db = SiteDB(home / "site.db")
    try:
        Accounts(db, now_ms=wall_ms).add(name, password)
    finally:
        db.close()


# ------------------------------------------------------------ serve


class Server:
    """站点进程:事件循环线程里跑 MQTT 与派遣器,HTTP 线程跑 API。"""

    def __init__(self, home: Path, *, api_host: str, api_port: int, broker_url: str) -> None:
        from d1max_contract.paho_transport import PahoTransport
        from d1max_site.api import SiteApi, check_exposure
        from d1max_site.dispatcher import Dispatcher
        from d1max_site.loop import LoopThread

        cfg = _load(home)
        server_crt, server_key = home / "ca" / "server" / "server.crt", \
            home / "ca" / "server" / "server.key"
        tls = None if api_host in ("127.0.0.1", "localhost", "::1") else (server_crt, server_key)
        check_exposure(api_host, tls)                     # 起线程之前拒
        self.db = SiteDB(home / "site.db")
        self.registry = Registry(self.db, site_id=cfg["site_id"])
        self.accounts = Accounts(self.db, now_ms=wall_ms)
        self.loop = LoopThread()
        self.loop.start()
        ca = home / "ca"
        transport = PahoTransport(broker_url, client_id=site_principal(cfg["site_id"]),
                                  tls_ca=str(ca / "ca.crt"), tls_cert=str(server_crt),
                                  tls_key=str(server_key))
        self.dispatcher = Dispatcher(transport, self.db, self.registry, now_ms=wall_ms)
        from d1max_site.scheduler import SiteScheduler
        self.scheduler = SiteScheduler(self.db, self.dispatcher, now_ms=wall_ms)
        from d1max_site.standby import StandbyManager
        self.standby = StandbyManager(self.db, self.dispatcher, now_ms=wall_ms)
        from d1max_site.incidents import IncidentDesk
        self.incidents = IncidentDesk(self.db, self.dispatcher, now_ms=wall_ms)
        self.api = SiteApi(host=api_host, port=api_port, loop=self.loop,
                           dispatcher=self.dispatcher, accounts=self.accounts, tls=tls,
                           scheduler=self.scheduler, standby=self.standby,
                           incidents=self.incidents, now_ms=wall_ms)
        self._stop = threading.Event()

    def start(self) -> None:
        self.loop.call(self.dispatcher.start, timeout_s=30)
        self.loop.submit(self._sync_loop)
        self.loop.submit(self._schedule_loop)
        self.api.start()

    async def _schedule_loop(self) -> None:
        """排程执行器:每 30 s 一拍。一拍炸了记下来、下一拍照走(老 W06 执行器同一个理由:
        它死掉的后果是静默的 —— 从此到点没人起跑)。"""
        from d1max_site.scheduler import PERIOD_S
        while not self._stop.is_set():
            await asyncio.sleep(PERIOD_S)
            try:
                await self.scheduler.tick()
                self.scheduler.last_error = ""
            except Exception as exc:
                self.scheduler.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("排程这一拍没办成")

    async def _sync_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(SYNC_PERIOD_S)
            try:
                added = await self.dispatcher.sync_robots()
                if added:
                    log.info("新登记的狗挂上了: %s", ", ".join(added))
            except Exception:
                log.exception("同步注册表失败,下一轮再试")

    def stop(self) -> None:
        self._stop.set()
        for what, fn in (("API", self.api.stop),
                         ("待命点", lambda: self.loop.call(self.standby.close, 10)),
                         ("派遣器", lambda: self.loop.call(self.dispatcher.close, 10)),
                         ("事件循环", self.loop.stop), ("数据库", self.db.close)):
            try:
                fn()
            except Exception:
                log.exception("收尾:%s 失败,后面的照做", what)


def cmd_serve(home: Path, api_host: str, api_port: int, broker_url: str | None) -> int:
    cfg = _load(home)
    url = broker_url or f"mqtts://127.0.0.1:{cfg['broker_port']}"
    srv = Server(home, api_host=api_host, api_port=api_port, broker_url=url)
    try:
        srv.start()
    except Exception as exc:  # noqa: BLE001 - 连不上 broker 之类:退 1,交给 systemd 重试
        print(f"d1max-site 起不来: {exc!r}", file=sys.stderr, flush=True)
        srv.stop()
        return 1
    signal.signal(signal.SIGTERM, lambda *_: srv._stop.set())
    print(f"d1max-site 起来了:site={cfg['site_id']} api={srv.api.url} broker={url}",
          flush=True)
    try:
        while not srv._stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    print("收尾中……", flush=True)
    srv.stop()
    return 0


# ------------------------------------------------------------ 入口


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="d1max-site", description=f"D1 Max 站点 {SITE_VERSION}")
    p.add_argument("--home", type=Path, default=DEFAULT_HOME, help="站点目录")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="建站点目录:CA、站点证书、数据库、broker 配置")
    i.add_argument("--site-id", required=True)
    i.add_argument("--hostname", action="append", required=True,
                   help="站点证书里的主机名或 IP(狗与手机用它连站点),可给多个")
    i.add_argument("--broker-port", type=int, default=8883)
    e = sub.add_parser("enroll", help="给一台狗签证书并登记")
    e.add_argument("robot_id")
    e.add_argument("--days", type=int, default=365)
    r = sub.add_parser("revoke", help="吊销一台狗(之后重启 d1max-mosquitto)")
    r.add_argument("robot_id")
    b = sub.add_parser("import-bundle", help="导入任务包,成为当前包")
    b.add_argument("bundle_dir", type=Path)
    sb = sub.add_parser("standby", help="登记(或改)一台狗的待命点")
    sb.add_argument("robot_id")
    sb.add_argument("name")
    sb.add_argument("--map", required=True, help="<map_id>:<version>")
    sb.add_argument("--pose", required=True, help="x,y,yaw")
    sb.add_argument("--default", action="store_true")
    so = sub.add_parser("source-add", help="登记事件源,打印共享密钥")
    so.add_argument("name")
    ic = sub.add_parser("intercept", help="登记(或改)拦截点")
    ic.add_argument("name")
    ic.add_argument("--map", required=True, help="<map_id>:<version>")
    ic.add_argument("--pose", required=True, help="x,y,yaw")
    zo = sub.add_parser("zone", help="防区映射到拦截点")
    zo.add_argument("zone")
    zo.add_argument("intercept")
    a = sub.add_parser("add-admin", help="加管理员账号")
    a.add_argument("name")
    s = sub.add_parser("serve", help="跑站点:派遣器 + 站点 API")
    s.add_argument("--api-host", default="127.0.0.1")
    s.add_argument("--api-port", type=int, default=8443)
    s.add_argument("--broker", default=None, help="默认 mqtts://127.0.0.1:<init 时的端口>")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    home: Path = args.home
    try:
        _check_owner(home)
        if args.cmd == "init":
            cmd_init(home, args.site_id, args.hostname, args.broker_port)
            print(f"站点目录建好了: {home}")
        elif args.cmd == "enroll":
            d = cmd_enroll(home, args.robot_id, args.days)
            print(f"证书包: {d}\n拷到狗上: ca.crt robot.crt robot.key → /etc/d1max/tls/,"
                  f"registration.json → /etc/d1max/")
        elif args.cmd == "revoke":
            summary = cmd_revoke(home, args.robot_id)
            print(f"已吊销 {args.robot_id}({summary})。重启 broker 才对 broker 生效: "
                  f"systemctl restart d1max-mosquitto")
        elif args.cmd == "import-bundle":
            got = cmd_import_bundle(home, args.bundle_dir, f"cli:{getpass.getuser()}")
            print(f"导入了 {got['bundle_id']} v{got['version']}:任务 {', '.join(got['missions'])};"
                  f"排程 {', '.join(got['schedule_entries']) or '无'}({got['timezone']})")
        elif args.cmd == "standby":
            cmd_standby(home, args.robot_id, args.name, args.map, args.pose, args.default)
            print(f"{args.robot_id} 的待命点 {args.name} 登记好了"
                  + ("(默认)" if args.default else ""))
        elif args.cmd == "source-add":
            secret = cmd_incident_admin(home, "source", name=args.name)
            print(f"事件源 {args.name} 登记好了。共享密钥"
                  f"(只显示这一次,配到摄像头/NVR 的转发器上):\n{secret}")
        elif args.cmd == "intercept":
            cmd_incident_admin(home, "intercept", name=args.name, map=args.map, pose=args.pose)
            print(f"拦截点 {args.name} 登记好了")
        elif args.cmd == "zone":
            cmd_incident_admin(home, "zone", zone=args.zone, intercept=args.intercept)
            print(f"防区 {args.zone} → 拦截点 {args.intercept}")
        elif args.cmd == "add-admin":
            pw = os.environ.get("D1MAX_SITE_PASSWORD") or getpass.getpass("口令: ")
            cmd_add_admin(home, args.name, pw)
            print(f"账号 {args.name} 加好了")
        else:
            return cmd_serve(home, args.api_host, args.api_port, args.broker)
    except (SiteError, CAError, RegistryError, AuthError) as exc:
        print(f"d1max-site: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
