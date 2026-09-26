"""``d1max-site``:站点的命令行(W00c1)。

站点目录(``--home``,默认 ``/var/lib/d1max-site``)的布局::

    site.json          site_id、broker 端口、主机名;可选 ``video``:``srt_host``(狗推流连的站点地址,
                   默认第一个主机名,要跟狗连 MQTT 用的是同一个)、``ports``(SRT 端口段,默认
                   8890–8989/UDP,防火墙要放行)、``ffmpeg``
    site.db            SQLite:注册表、账号、会话、命令、事件
    ca/                站点 CA(ca.key 0600)、签发过的证书、CRL;ca/server/ 是站点服务证书
    broker/            mosquitto.conf 与 acl(由 broker_conf 生成),d1max-mosquitto.service 用它

子命令::

    init      --site-id S --hostname H [--hostname …] [--broker-port 8883]
    enroll    ROBOT_ID [--days 365]        → 打印证书包目录(拷到狗的 /etc/d1max/)
    revoke    ROBOT_ID                     → 吊销;要重启 d1max-mosquitto 才对 broker 生效
    add-admin NAME                         → 口令从 D1MAX_SITE_PASSWORD 或交互输入
    add-account NAME --role admin|guard|owner → 同上,带角色(W00c3)
    set-role NAME ROLE / disable NAME / enable NAME → 账号管理(W00c3;都吊销那个账号的会话)
    import-bundle DIR                      → 导入任务包,成为当前包(W00c2a)
    standby ROBOT NAME --map M:VER --pose x,y,yaw [--default] → 登记待命点(W00c2b)
    source-add NAME                        → 登记事件源,打印共享密钥(只这一次;W00c2c)
    intercept NAME --map M:VER --pose x,y,yaw → 登记拦截点(W00c2c)
    zone ZONE INTERCEPT                    → 防区映射到拦截点(W00c2c)
    fingerprint                            → 站点服务证书的 SHA-256(手机添加站点时核对;W00c4)
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
#: 后台杂事(自动判读、备份)多久一拍(秒)。
CHORE_PERIOD_S = 30.0


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


def cmd_fingerprint(home: Path) -> str:
    """站点服务证书(DER)的 SHA-256,小写十六进制。手机钉的就是它(W00c4 设计决定二 A)。"""
    _load(home)
    fp = SiteCA(home / "ca").fingerprint(home / "ca" / "server" / "server.crt")
    return fp.split(":", 1)[1]


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


def cmd_map_import(home: Path, src: Path, map_id: str, version: str, note: str) -> dict:
    """W00c5d 第二部分:把一个目录里的地图文件登记进站点的地图目录(厂商图、离线建好的图)。"""
    from d1max_site.maps import MapCatalog, MapError
    _load(home)
    db = SiteDB(home / "site.db")
    try:
        ref = MapCatalog(home, db, now_ms=wall_ms).import_dir(
            src, map_id=map_id, version=version, source=f"cli:{getpass.getuser()}", note=note)
        return ref.to_wire()
    except MapError as exc:
        raise SiteError(str(exc)) from exc
    finally:
        db.close()


def cmd_release_add(home: Path, src: Path, note: str) -> dict:
    """W00c5d 第三部分:把一个发布包目录(``release pack`` 的产物)登记进站点的发布目录。"""
    from d1max_site.releases import ReleaseCatalog, ReleaseCatalogError
    _load(home)
    db = SiteDB(home / "site.db")
    try:
        return ReleaseCatalog(home, db, now_ms=wall_ms).add(src, note=note)
    except ReleaseCatalogError as exc:
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


def cmd_add_admin(home: Path, name: str, password: str, role: str = "admin") -> None:
    _load(home)
    db = SiteDB(home / "site.db")
    try:
        Accounts(db, now_ms=wall_ms).add(name, password, role=role)
    finally:
        db.close()


def cmd_account(home: Path, what: str, name: str, role: str = "") -> None:
    _load(home)
    db = SiteDB(home / "site.db")
    try:
        acc = Accounts(db, now_ms=wall_ms)
        if what == "set-role":
            acc.set_role(name, role)
        else:
            acc.set_disabled(name, what == "disable")
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

        # 监护台(W00c6i)一个,接口和待命点管理器共用:站点这一道关要看得见谁在监护。
        from d1max_site.supervision import SupervisionDesk
        self.supervision = SupervisionDesk(self.dispatcher, self.loop, now_ms=wall_ms)
        self.standby = StandbyManager(self.db, self.dispatcher, now_ms=wall_ms,
                                      refusal=self.supervision.refusal)
        from d1max_site.incidents import IncidentDesk
        self.incidents = IncidentDesk(self.db, self.dispatcher, now_ms=wall_ms)
        # W00c5a:告警台挂上派遣器(状态、事件、遥测),经 SSE 推给值守屏。
        from d1max_site.alert_sources import SiteAlertSources
        from d1max_site.alert_store import AlertDesk
        self.alerts = AlertDesk(self.db, now_ms=wall_ms, publish=self.dispatcher.feed.publish)
        self.alert_sources = SiteAlertSources(self.alerts, now_ms=wall_ms)
        self.alert_sources.attach(self.dispatcher)
        # W00c5b:视频经站点。狗按需把相机推到这里(SRT),这里转 MJPEG 给观众。
        from d1max_site.video import VideoHub, dispatcher_sender
        vcfg = cfg.get("video", {})
        self.video = VideoHub(send=dispatcher_sender(self.dispatcher, self.loop),
                              known=lambda rid: rid in self.dispatcher.clients,
                              srt_host=vcfg.get("srt_host") or cfg["hostnames"][0],
                              ports=tuple(vcfg.get("ports", (8890, 8989))),
                              ffmpeg=vcfg.get("ffmpeg", "ffmpeg"))
        self.dispatcher.on_event(self.video.on_event)
        # W00c5c:遥控经站点(决策 7)。「没画面不许动」读的就是上面那个视频的画面健康。
        from d1max_site.teleop import TeleopDesk
        self.teleop = TeleopDesk(self.dispatcher, self.loop, audit=None, now_ms=wall_ms,
                                 video_ok=self._video_ok)
        # W00c5d(决策 8):狗的运行记录经狗专用口(mTLS)传上来,落证据库;判读、复核、导出、备份
        # 都在这儿。
        from d1max_site.backup import SiteBackup
        from d1max_site.evidence import EvidenceStore
        from d1max_site.intake import DEFAULT_PORT, IntakeServer, server_context
        from d1max_site.runs import RunDesk
        self.evidence = EvidenceStore(home / "evidence", self.db, now_ms=wall_ms)
        from d1max_site.maps import MapCatalog
        from d1max_site.releases import ReleaseCatalog
        self.maps = MapCatalog(home, self.db, now_ms=wall_ms)
        self.releases = ReleaseCatalog(home, self.db, now_ms=wall_ms)
        # 判读、备份、接收口在别的线程里:告警要跳回事件循环去报(告警簿只许在循环里改)。
        from d1max_site.alert_store import LoopAlerts
        loop_alerts = LoopAlerts(self.alerts, self.loop)
        self.runs = RunDesk(self.evidence, home=home, now_ms=wall_ms, alerts=loop_alerts)
        backup_dir = cfg.get("backup_dir")
        self.backup = SiteBackup(self.db, self.evidence.root,
                                 Path(backup_dir) if backup_dir else None, now_ms=wall_ms,
                                 alerts=loop_alerts,
                                 more={"maps": home / "maps", "bags": home / "bags",
                                       "releases": home / "releases"})
        icfg = cfg.get("intake", {})
        self.intake = IntakeServer(
            host=icfg.get("host", "0.0.0.0"), port=int(icfg.get("port", DEFAULT_PORT)),
            ctx=server_context(cert=server_crt, key=server_key, ca=ca / "ca.crt",
                               crl=ca / "crl.pem"),
            db=self.db, store=self.evidence, now_ms=wall_ms, maps=self.maps)
        self.intake.releases = self.releases
        self.intake.on_refused = lambda robot, run, rel, why: loop_alerts.raise_alert(
            kind="upload_refused", robot=robot, title=f"站点不收 {run}/{rel}",
            detail=f"{why}(那一趟留在狗上,不会自己删)")
        self.api = SiteApi(host=api_host, port=api_port, loop=self.loop,
                           dispatcher=self.dispatcher, accounts=self.accounts, tls=tls,
                           scheduler=self.scheduler, standby=self.standby,
                           incidents=self.incidents, alerts=self.alerts, video=self.video,
                           teleop=self.teleop, runs=self.runs, backup=self.backup,
                           maps=self.maps, releases=self.releases,
                           supervision=self.supervision, now_ms=wall_ms)
        self.teleop.audit = self.api.audit
        self._stop = threading.Event()
        self._chores = threading.Thread(target=self._chore_loop, daemon=True,
                                        name="site-chores")

    def start(self) -> None:
        self.loop.call(self.dispatcher.start, timeout_s=30)
        self.loop.submit(self._sync_loop)
        self.loop.submit(self._schedule_loop)
        self.loop.submit(self._alert_loop)
        self.api.start()
        self.intake.start()
        self._chores.start()

    def _chore_loop(self) -> None:
        """后台杂事(不在事件循环里:判读要等模型、备份要拷盘):每 30 s 自动判读一拍,
        每拍看一眼备份到点没有。一拍炸了记下来、下一拍照走。"""
        while not self._stop.wait(CHORE_PERIOD_S):
            for what, fn in (("自动判读", self.runs.step), ("备份", self.backup.step)):
                try:
                    fn()
                except Exception:
                    log.exception("%s这一拍没办成", what)

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
            try:
                self.alert_sources.on_site_error("schedule", self.scheduler.last_error)
            except Exception:
                # 报告警本身失败(比如库锁住了)不许把排程协程带走 —— 那正是 schedule_died 要防的
                # 静默死亡(W00c5a 内部评审)。
                log.exception("排程没办成的告警记不下来")

    async def _alert_loop(self) -> None:
        """告警:每几秒看一次掉线、让 P1 未确认的升档。一拍炸了记下来、下一拍照走。"""
        from d1max_site.alert_sources import STEP_S
        while not self._stop.is_set():
            await asyncio.sleep(STEP_S)
            try:
                self.alert_sources.step()
            except Exception:
                log.exception("告警这一拍没办成")

    async def _sync_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(SYNC_PERIOD_S)
            try:
                added = await self.dispatcher.sync_robots()
                if added:
                    log.info("新登记的狗挂上了: %s", ", ".join(added))
            except Exception:
                log.exception("同步注册表失败,下一轮再试")

    def _video_ok(self, robot_id: str) -> bool:
        """这台狗至少一路画面在线(最近 2 s 内来过帧)。查不到就当没有。"""
        from d1max_site.video import VideoError
        try:
            return any(c["online"] for c in self.video.health(robot_id).values())
        except VideoError:
            return False

    def stop(self) -> None:
        self._stop.set()
        for what, fn in (("遥控", self.teleop.close_all), ("API", self.api.stop),
                         ("接收口", self.intake.stop),
                         ("视频", self.video.close),
                         ("待命点", lambda: self.loop.call(self.standby.close, 10)),
                         ("事件台", lambda: self.loop.call(self.incidents.close, 10)),
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
    sub.add_parser("fingerprint", help="打印站点服务证书的 SHA-256(手机添加站点时核对)")
    e = sub.add_parser("enroll", help="给一台狗签证书并登记")
    e.add_argument("robot_id")
    e.add_argument("--days", type=int, default=365)
    r = sub.add_parser("revoke", help="吊销一台狗(之后重启 d1max-mosquitto)")
    r.add_argument("robot_id")
    b = sub.add_parser("import-bundle", help="导入任务包,成为当前包")
    b.add_argument("bundle_dir", type=Path)
    ra = sub.add_parser("release-add", help="登记一个发布包(W00c5d):之后可以给狗装、切")
    ra.add_argument("dir", type=Path)
    ra.add_argument("--note", default="")
    mi = sub.add_parser("map-import", help="把一个目录里的地图文件登记成站点的一张图(W00c5d)")
    mi.add_argument("dir", type=Path)
    mi.add_argument("--map-id", required=True)
    mi.add_argument("--version", required=True)
    mi.add_argument("--note", default="")
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
    aa = sub.add_parser("add-account", help="加账号(带角色)")
    aa.add_argument("name")
    aa.add_argument("--role", required=True, choices=("admin", "guard", "owner"))
    sr = sub.add_parser("set-role", help="改账号的角色")
    sr.add_argument("name")
    sr.add_argument("role", choices=("admin", "guard", "owner"))
    for verb in ("disable", "enable"):
        sub.add_parser(verb, help=f"{verb} 账号").add_argument("name")
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
            print(f"站点证书指纹(手机添加站点时核对):{cmd_fingerprint(home)}")
        elif args.cmd == "fingerprint":
            print(cmd_fingerprint(home))
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
        elif args.cmd == "release-add":
            got = cmd_release_add(home, args.dir, args.note)
            print(f"登记了 {got['name']}({got['version']}):{got['size']} 字节,"
                  f"sha256 {got['sha256']}")
        elif args.cmd == "map-import":
            got = cmd_map_import(home, args.dir, args.map_id, args.version, args.note)
            print(f"登记了 {got['map_id']}:{got['version']}:"
                  + ", ".join(f"{f['name']}({f['size']} 字节)" for f in got["files"]))
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
        elif args.cmd in ("add-admin", "add-account"):
            pw = os.environ.get("D1MAX_SITE_PASSWORD") or getpass.getpass("口令: ")
            role = getattr(args, "role", "admin")
            cmd_add_admin(home, args.name, pw, role)
            print(f"账号 {args.name}({role})加好了")
        elif args.cmd in ("set-role", "disable", "enable"):
            cmd_account(home, args.cmd, args.name, getattr(args, "role", ""))
            print(f"{args.cmd} {args.name}:好了(这个账号的会话已全部吊销)")
        else:
            return cmd_serve(home, args.api_host, args.api_port, args.broker)
    except (SiteError, CAError, RegistryError, AuthError) as exc:
        print(f"d1max-site: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
