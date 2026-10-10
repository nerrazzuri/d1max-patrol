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
    add-account NAME --role admin|guard|owner [--display-name 真名] → 同上,带角色(W00c3)、
                     显示名(W20 操作人实名:一人一个账号)
    set-role NAME ROLE / disable NAME / enable NAME → 账号管理(W00c3;都吊销那个账号的会话)
    import-bundle DIR                      → 导入任务包,成为当前包(W00c2a)
    standby ROBOT NAME --map M:VER --pose x,y,yaw [--default] → 登记待命点(W00c2b)
    source-add NAME                        → 登记事件源,打印共享密钥(只这一次;W00c2c)
    camera-add NAME --onvif URL --zone Z   → 登记固定摄像头(ONVIF 事件 → 入侵派遣;W19)
    camera-rm NAME / camera-list
    source-rotate NAME / source-rm NAME / source-list → 换密钥、删、列出事件源(W16)
    intercept NAME --map M:VER --pose x,y,yaw → 登记拦截点(W00c2c)
    zone ZONE INTERCEPT                    → 防区映射到拦截点(W00c2c)
    fingerprint                            → 站点服务证书的 SHA-256(手机添加站点时核对;W00c4)
    serve     [--api-host 127.0.0.1] [--api-port 8443] [--broker mqtts://127.0.0.1:8883]
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import inspect
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

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


def _open_db(home: Path, cfg: dict) -> SiteDB:
    """站点库,接上口令密钥(W30,决策 43:摄像头口令、事件源密钥加密落库)。"""
    from d1max_site.sealbox import site_box
    db = SiteDB(home / "site.db")
    db.sealbox = site_box(cfg, "secrets")
    return db


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


def _code_text(code: str) -> str:
    return ("开通码(一次性,用过、过期就作废;别贴到公开的地方):\n" + code +
            "\n狗上:sudo /opt/d1max/bin/python -m d1max_patrol.cli provision --code '<上面那一行>'")


def cmd_enroll_code(home: Path, robot_id: str, host: str | None, hours: float) -> str:
    """给已登记的狗出一个开通码(商业化 A4):一次性、``hours`` 小时有效,旧码作废。"""
    from d1max_site.provision import ProvisionError, issue_code
    cfg = _load(home)
    ca = SiteCA(home / "ca")
    db = SiteDB(home / "site.db")
    try:
        cur = Registry(db, site_id=cfg["site_id"]).get(robot_id)
        if cur is None or cur.revoked:
            raise SiteError(f"{robot_id} 没登记或已吊销:先 enroll")
        try:
            return issue_code(db, cfg, home, robot_id,
                              fingerprint=ca.fingerprint(home / "ca" / "server" / "server.crt"),
                              now_ms=wall_ms(), host=host, ttl_ms=int(hours * 3600_000))
        except ProvisionError as exc:
            raise SiteError(str(exc)) from exc
    finally:
        db.close()


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
        # W33:新登记的狗先只许手动派,管理员确认后(robot-auto)才接自动派遣
        reg.enroll(robot_id, fingerprint=bundle.fingerprint, issued_at=r.issued_at,
                   expires_at=r.expires_at, now_ms=wall_ms(), manual_only=True)
        # W30b:证书包里带站点的证据公钥(狗用它封照片、录像),从证据私钥算出来
        from d1max_contract.evseal import public_key
        from d1max_site.evidence import load_evidence_key
        key = load_evidence_key(cfg)
        if key is not None:
            (bundle.dir / "evidence-pub.key").write_text(public_key(key).hex() + "\n",
                                                         encoding="ascii")
        return bundle.dir
    finally:
        db.close()


#: 事件源密钥怎么用(W33:2026-10-08 C40221 现场照字面拿字符串算 HMAC,一直 401)。打在密钥**前面**:
#: 密钥照旧是最后一行(脚本、测试按最后一行取)。
_HEX_HINT = ("注意:下面是 64 位十六进制,算签名前先解码成 32 字节再当 HMAC-SHA256 的密钥"
             "(Python: bytes.fromhex(密钥));签名格式见 docs/事件源接入.md")


def cmd_evidence_open(home: Path, key_path: str | None = None) -> tuple[int, int]:
    """W30b:封好、还没解开的照片、录像补解(换了私钥、当时没配私钥)。"""
    from d1max_site.evidence import EvidenceStore, load_evidence_key
    from d1max_site.recordings import RecordingStore
    cfg = _load(home)
    if key_path:
        cfg = {**cfg, "evidence_key": key_path}
    key = load_evidence_key(cfg)
    if key is None:
        raise SiteError("没有证据私钥:解不了")
    db = SiteDB(home / "site.db")
    try:
        store = EvidenceStore(home / "evidence", db, now_ms=wall_ms)
        rec = RecordingStore(db, home / "recordings", now_ms=wall_ms)
        store.evidence_key = rec.evidence_key = key
        a, b = store.open_leftovers()
        c, d = rec.open_leftovers()
        return a + c, b + d
    finally:
        db.close()


def cmd_robot_service(home: Path, robot_id: str, manual: bool, note: str = "") -> str:
    """W33:只许手动派 / 接自动派遣。站点在跑也能改(下一拍起按库里的算)。"""
    cfg = _load(home)
    db = SiteDB(home / "site.db")
    try:
        reg = Registry(db, site_id=cfg["site_id"])
        if reg.get(robot_id) is None:
            raise SiteError(f"没有登记过 {robot_id}")
        reg.set_manual_only(robot_id, manual, by=f"cli:{getpass.getuser()}", now_ms=wall_ms(),
                            note=note)
        from d1max_site.audit import AuditLog
        AuditLog(db, now_ms=wall_ms).record(
            actor=f"cli:{getpass.getuser()}",
            action="robot manual_only" if manual else "robot auto", target=robot_id,
            detail={"note": note} if note else {})
        return (f"{robot_id}:只许手动派(排程、入侵、回充、自动回待命点都不派)" if manual
                else f"{robot_id}:接自动派遣(排程、入侵、回充、自动回待命点)")
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
    cfg = _load(home)
    db = SiteDB(home / "site.db")
    try:
        # A 阶段外审 I3:不验签只能显式打开(开发用),删掉钥匙不等于不验
        pub = None if cfg.get("release_unsigned") is True else \
            Path(cfg.get("release_pubkey") or "/etc/d1max-site/release-pub.pem")
        if pub is None:
            print("警告:site.json 里 release_unsigned=true,登记升级包不验签(只该在开发环境)",
                  file=sys.stderr)
        return ReleaseCatalog(home, db, now_ms=wall_ms, pubkey=pub).add(src, note=note)
    except ReleaseCatalogError as exc:
        raise SiteError(str(exc)) from exc
    finally:
        db.close()


def cmd_privacy_purge(home: Path, since: str, until: str, robot: str | None,
                      reason: str) -> dict:
    """按时间段删证据和录像(W30,PDPA:当事人要求删除)。标了留着的不删;记审计。"""
    from datetime import datetime

    from d1max_site.audit import AuditLog
    from d1max_site.evidence import EvidenceStore
    from d1max_site.privacy import PrivacyDesk
    from d1max_site.recordings import RecordingStore
    cfg = _load(home)
    if not reason.strip():
        raise SiteError("要写为什么删(--reason):谁要求的、哪一条请求")
    try:
        since_ms = int(datetime.fromisoformat(since).timestamp() * 1000)
        until_ms = int(datetime.fromisoformat(until).timestamp() * 1000)
    except ValueError as exc:
        raise SiteError(f"时刻要写成 2026-10-08T21:00+08:00 这样:{exc}") from exc
    db = SiteDB(home / "site.db")
    try:
        from d1max_site.runs import RunDesk
        backup = cfg.get("backup_dir")
        store = EvidenceStore(home / "evidence", db, now_ms=wall_ms)
        desk = PrivacyDesk(db, store, now_ms=wall_ms,
                           recordings=RecordingStore(db, home / "recordings", now_ms=wall_ms),
                           backup_dest=Path(backup) if backup else None,
                           exports=RunDesk(store, home=home, now_ms=wall_ms))
        try:
            got = desk.purge(since_ms=since_ms, until_ms=until_ms, robot_id=robot)
        except ValueError as exc:
            raise SiteError(str(exc)) from exc
        AuditLog(db, now_ms=wall_ms).record(
            actor=f"cli:{os.environ.get('USER', '?')}", action="privacy purge",
            target=robot or "*", status=200,
            detail={"since": since, "until": until, "reason": reason[:200],
                    "runs": got["runs"], "recordings": got["recordings"],
                    "held": len(got["held_runs"]) + len(got["held_recordings"])})
        return got
    finally:
        db.close()


def _passphrase(confirm: bool) -> str:
    """托管包的口令:``D1MAX_KEYS_PASSPHRASE`` 或交互输入(导出时输两遍)。"""
    p = os.environ.get("D1MAX_KEYS_PASSPHRASE")
    if p:
        return p
    p = getpass.getpass("托管包口令:")
    if confirm and getpass.getpass("再输一遍:") != p:
        raise SiteError("两遍口令不一样")
    return p


def cmd_keys(home: Path, args: argparse.Namespace) -> int:
    """站点密钥(A3):自检、导出托管包、导回。"""
    from d1max_site import keyvault
    cfg = _load(home) if (home / "site.json").exists() else {}
    try:
        if args.cmd == "keys-status":
            rows = keyvault.status(cfg)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=1))
            else:
                for r in rows:
                    print(f"{r['name']:9} {r['fingerprint'] or '-':16} {r['mode'] or '-':6} "
                          f"{r['problem'] or 'ok'}  {r['path']}")
            return 1 if any(r["problem"] for r in rows) else 0
        if args.cmd == "keys-export":
            names = keyvault.export(cfg, Path(args.out), _passphrase(True))
            print(f"托管包写好了:{args.out}(带了 {'、'.join(names)})。口令另外记,别跟它放一起。")
            return 0
        names = keyvault.import_(cfg, Path(args.src), _passphrase(False), force=args.force)
        print("放回了:" + ("、".join(names) if names else "(都已经在了、一样的)"))
        return 0
    except keyvault.KeyError_ as exc:
        raise SiteError(str(exc)) from exc


def cmd_support_bundle(home: Path, out: str | None, hours: int) -> str:
    """命令行导出诊断包(A1):没有正在跑的站点进程的实时状态(事件循环、MQTT、狗),其余都有。"""
    from d1max_site.health import HealthDesk, support_bundle
    now = wall_ms()
    path = Path(out) if out else Path.cwd() / time.strftime(
        "d1max-support-%Y%m%dT%H%M%SZ.tar.gz", time.gmtime(now / 1000))
    db = SiteDB(home / "site.db")
    try:
        h = HealthDesk(db, home=home, now_ms=wall_ms)
        return str(support_bundle(path, home=home, db=db, health=h, hours=hours, now_ms=now))
    finally:
        db.close()


def backup_dirs(home: Path, *, encrypted: bool) -> dict[str, Path]:
    """要镜像进备份的目录。A2:站点身份(CA 私钥、站点服务证书、broker 配置)也进 —— **只进加密的
    备份**(明文备份盘被拿走,CA 私钥就丢了)。"""
    out = {"maps": home / "maps", "bags": home / "bags", "releases": home / "releases"}
    if encrypted:
        out |= {"ca": home / "ca", "broker": home / "broker"}
    return out


def cmd_backup_drill(home: Path, key: str | None) -> dict:
    """灾备演练(A2):用站点配的备份目录、备份密钥,恢复到临时目录、校验、对数,结果记库。"""
    from d1max_site.restore import drill
    from d1max_site.sealbox import DEFAULT_KEYS
    cfg = _load(home)
    dest = cfg.get("backup_dir")
    if not dest:
        raise SiteError("site.json 没配 backup_dir:没有备份,演练不了")
    k = Path(key or cfg.get("backup_key") or DEFAULT_KEYS["backup"])
    got = drill(home / "site.db", Path(dest), k if k.is_file() else None, now_ms=wall_ms)
    db = SiteDB(home / "site.db")
    try:
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                      ("backup_drill", json.dumps(got, ensure_ascii=False)))
    finally:
        db.close()
    return got


def cmd_backup_open(src: Path, dst: Path, key: Path) -> int:
    """把加密的备份目录解开到另一个目录(W30;恢复用)。回解开了几个文件。"""
    from d1max_site.backup import SEALED
    from d1max_site.sealbox import SealBox, SealError, load_or_create_key
    if not key.is_file():
        raise SiteError(f"没有备份密钥 {key}(装机时离线另存的那一份)")
    if dst.exists() and any(dst.iterdir()):
        raise SiteError(f"{dst} 不是空的")
    box = SealBox(load_or_create_key(key))
    n = 0
    for f in sorted(src.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            if f.name.endswith(SEALED):
                box.open_file(f, out.with_name(out.name[:-len(SEALED)]))
                n += 1
            else:
                import shutil
                shutil.copy2(f, out)
        except SealError as exc:
            raise SiteError(f"{rel} 解不开:{exc}") from exc
    return n


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
    desk = IncidentDesk(db, disp, now_ms=wall_ms)
    # W23 外审:命令行设拦截点也查走不走得到(同接口)
    from d1max_site.intercept_reach import InterceptReach
    from d1max_site.maps import MapCatalog
    from d1max_site.nav_zones import NavZones
    desk.reach = InterceptReach(db, MapCatalog(home, db, now_ms=wall_ms),
                                NavZones(db, now_ms=wall_ms))
    return desk


def cmd_camera(home: Path, what: str, **kw) -> object:
    """固定摄像头(W19)。口令存在站点库里(只许站点用户读),``list`` 不显示。"""
    from d1max_site.cctv import Camera, add_camera, load_cameras, remove_camera
    db = _open_db(home, _load(home))
    try:
        if what == "add":
            try:
                add_camera(db, Camera(name=kw["name"], onvif_url=kw["onvif"], username=kw["user"],
                                      password=kw["password"], zone=kw["zone"],
                                      rtsp_url=kw["rtsp"], motion=kw["motion"]),
                           now_ms=wall_ms())
            except ValueError as exc:
                raise SiteError(str(exc)) from exc
            return ""
        if what == "rm":
            return remove_camera(db, kw["name"])
        return "\n".join(f"{c.name}  防区 {c.zone}  {c.onvif_url}"
                         + ("  有画面" if c.rtsp_url else "") + ("  动了也算" if c.motion else "")
                         for c in load_cameras(db))
    finally:
        db.close()


def cmd_incident_admin(home: Path, what: str, **kw) -> str:
    from d1max_site.incidents import IncidentError
    db = _open_db(home, _load(home))
    try:
        desk = _desk(home, db)
        if what == "source":
            return desk.add_source(kw["name"])
        if what == "source-rotate":                      # W16
            return desk.rotate_secret(kw["name"])
        if what == "source-rm":
            desk.remove_source(kw["name"])
            return ""
        if what == "source-list":
            return "\n".join(s["name"] for s in desk.sources())
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


def cmd_add_admin(home: Path, name: str, password: str, role: str = "admin",
                  display_name: str = "") -> None:
    _load(home)
    db = SiteDB(home / "site.db")
    try:
        Accounts(db, now_ms=wall_ms).add(name, password, role=role, display_name=display_name)
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

    def __init__(self, home: Path, *, api_host: str, api_port: int, broker_url: str,
                 rtcm_source: str | None = None) -> None:
        from d1max_contract.paho_transport import PahoTransport
        from d1max_site.api import SiteApi, check_exposure
        from d1max_site.dispatcher import Dispatcher
        from d1max_site.loop import LoopThread

        cfg = _load(home)
        server_crt, server_key = home / "ca" / "server" / "server.crt", \
            home / "ca" / "server" / "server.key"
        tls = None if api_host in ("127.0.0.1", "localhost", "::1") else (server_crt, server_key)
        check_exposure(api_host, tls)                     # 起线程之前拒
        self.db = _open_db(home, cfg)
        from d1max_site.sealbox import migrate_plaintext
        n = migrate_plaintext(self.db)                     # W30:老库里的明文口令就地加密
        if n:
            log.info("老库里的 %d 条明文口令加密了", n)
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
        self.alert_sources = SiteAlertSources(self.alerts, now_ms=wall_ms,
                                              skew_of=self.dispatcher.clock_skew_s)   # W09d:同一份
        self.alert_sources.attach(self.dispatcher)
        # W00c6c:排程这一轮没跑,告诉值守的人。
        self.scheduler.on_outcome = self.alert_sources.on_schedule_outcome
        # W16:入侵有了去向(派出去了、没狗去)、事件源被限流,告诉值守的人。
        self.incidents.on_outcome = self.alert_sources.on_incident
        self.incidents.on_throttled = self.alert_sources.on_incident_throttled
        # W20:布防模式。撤防的防区来了入侵只记账、不派狗、不报告警。
        from d1max_site.modes import ArmingDesk
        self.arming = ArmingDesk(self.db, now_ms=wall_ms, publish=self.dispatcher.feed.publish)
        self.incidents.arming = self.arming
        # W22:分级驱离。到了拦截点由它接管(开声光、自动升级、回程)。
        from d1max_site.deterrence import DeterrenceDesk
        self.deterrence = DeterrenceDesk(self.db, self.dispatcher, now_ms=wall_ms,
                                         standby=self.standby)
        # W13:自动回充(决策 25、45)。回充这几趟结束时待命点管理器不派回程;入侵派遣问它能不能派。
        from d1max_site.charging import ChargeDesk
        ccfg = cfg.get("charging", {})
        self.charge = ChargeDesk(self.db, self.dispatcher, now_ms=wall_ms,
                                 low_pct=float(ccfg.get("low_pct", 30)),
                                 resume_pct=float(ccfg.get("resume_pct", 90)))
        self.charge.alerts = self.alerts
        # W33(决策 48):布防中狗没在驱离时自己看见人报 P1
        from d1max_site.sightings import PersonWatch
        self.sightings = PersonWatch(self.db, self.dispatcher, now_ms=wall_ms, arming=self.arming,
                                     deterrence=self.deterrence)
        self.sightings.alerts = self.alerts
        from d1max_site.force import ForceWatch
        #: 狗翻倒、被抱起来(W26,决策 52):按狗能力里的状态对账报 P1,布防时被抱起来响警笛。
        self.force = ForceWatch(self.db, self.dispatcher, now_ms=wall_ms, arming=self.arming)
        self.force.alerts = self.alerts
        self.deterrence.held_by_others = self.force.held   # 系统审查 S04:驱离不关受力警报的灯
        self.charge.busy = self.deterrence.busy
        self.scheduler.site_busy = self.deterrence.busy    # 系统审查 S05:排程不抢在驱离的狗
        self.incidents.charging = self.charge.refuse
        self.standby.hold = lambda rid, tid: (self.deterrence.holds(rid, tid)
                                              or self.charge.holds(rid, tid))
        self.deterrence.alerts = self.alerts           # W24:驱离中看到人报告警
        self.incidents.busy = self.deterrence.busy
        # W29:全天候。天气(联网查 + 手动切):雷暴停排程巡检、照派入侵;下雨、雷暴全狗限速。
        from d1max_site.weather import WeatherDesk, parse_latlon
        latlon = parse_latlon(cfg.get("weather", {}).get("latlon")
                              or os.environ.get("D1MAX_SITE_LATLON"))
        self.weather = WeatherDesk(self.db, self.dispatcher, now_ms=wall_ms, latlon=latlon,
                                   publish=self.dispatcher.feed.publish)
        self.scheduler.weather = self.weather
        self.weather.standby = self.standby            # 雷暴撤了巡检派回待命点(W29 外审 1)
        self.weather.alerts = self.alerts              # 回不得报「没回待命点」(W29 复查二)
        self.alert_sources.weather = self.weather
        log.info("天气:%s", f"联网查(坐标 {latlon[0]:.2f},{latlon[1]:.2f})" if latlon
                 else "没配坐标,只能手动切")
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
        self.dispatcher.maps = self.maps           # 派单前查点在不在「有图」的地方(W09c)
        from d1max_site.nav_zones import NavZones
        self.zones = NavZones(self.db, now_ms=wall_ms)
        # W23:拦截点走不走得到(跟狗同一份规划);设的时候查,杂事线程每拍对账
        from d1max_site.intercept_reach import InterceptReach
        self.incidents.reach = InterceptReach(self.db, self.maps, self.zones)
        self.dispatcher.zones = self.zones         # W10:派单前核区域修订、补发
        self.releases = ReleaseCatalog(home, self.db, now_ms=wall_ms)
        # 判读、备份、接收口在别的线程里:告警要跳回事件循环去报(告警簿只许在循环里改)。
        from d1max_site.alert_store import LoopAlerts
        loop_alerts = LoopAlerts(self.alerts, self.loop)
        #: 杂事线程、推送线程里报告警用这个(经事件循环;AlertDesk 只许在事件循环里用)。
        self._loop_alerts = loop_alerts
        self.runs = RunDesk(self.evidence, home=home, now_ms=wall_ms, alerts=loop_alerts)
        backup_dir = cfg.get("backup_dir")
        from d1max_site.sealbox import DEFAULT_KEYS, site_box
        bbox = site_box(cfg, "backup") if backup_dir else None
        more = backup_dirs(home, encrypted=bbox is not None)
        if bbox is None and backup_dir:
            log.error("备份没加密:CA 私钥不进备份,站点主机坏了狗要全部重登记"
                      "(装机脚本会生成备份密钥)")
        self.backup = SiteBackup(self.db, self.evidence.root,
                                 Path(backup_dir) if backup_dir else None, now_ms=wall_ms,
                                 alerts=loop_alerts, more=more, box=bbox,
                                 files={"site.json": home / "site.json"})
        self._backup_key = Path(cfg.get("backup_key") or DEFAULT_KEYS["backup"]) \
            if bbox is not None else None
        icfg = cfg.get("intake", {})
        self.intake = IntakeServer(
            host=icfg.get("host", "0.0.0.0"), port=int(icfg.get("port", DEFAULT_PORT)),
            ctx=server_context(cert=server_crt, key=server_key, ca=ca / "ca.crt",
                               crl=ca / "crl.pem"),
            db=self.db, store=self.evidence, now_ms=wall_ms, maps=self.maps)
        self.intake.releases = self.releases
        # W18:连续录像。不进备份(30 天两路录像要 TB 级;留存与盘的规矩见 recordings)。
        from d1max_site.alert_sources import SITE as _SITE
        from d1max_site.recordings import RecordingStore
        rcfg = cfg.get("retention", {})
        self.recordings = RecordingStore(self.db, home / "recordings", now_ms=wall_ms,
                                         keep_days=int(rcfg.get("recordings_days", 30)))
        # W30b(决策 51):狗封好的照片、录像收齐了用证据私钥解开
        from d1max_site.evidence import EvidencePlainWatch, load_evidence_key
        ekey = load_evidence_key(cfg)
        self.evidence.evidence_key = self.recordings.evidence_key = ekey
        self.evidence_watch = EvidencePlainWatch(self.db, self.dispatcher, now_ms=wall_ms)
        # W30(决策 43,PDPA):证据留存期、按时间段删
        from d1max_site.privacy import KEEP_DAYS, PrivacyDesk
        self.privacy = PrivacyDesk(self.db, self.evidence, now_ms=wall_ms,
                                   recordings=self.recordings,
                                   backup_dest=Path(backup_dir) if backup_dir else None,
                                   keep_days=int(rcfg.get("evidence_days", KEEP_DAYS)),
                                   exports=self.runs)
        self.recordings.on_trimmed = lambda n, oldest: loop_alerts.raise_alert(
            kind="recording_trimmed", robot=_SITE, title=f"站点盘紧,删了最旧的 {n} 段录像",
            detail="不到 30 天就删了:站点盘小,加盘或少录几路")
        self.recordings.on_stuck = lambda n, why: loop_alerts.raise_alert(
            kind="recording_delete_failed", robot=_SITE, title=f"站点盘紧,{n} 段录像删不掉",
            detail=f"{why[:200]};盘到底线后巡检的照片、记录也传不上来:查录像目录的权限、挂载")
        self.intake.recordings = self.recordings
        # W19:固定摄像头自带的入侵检测(ONVIF 事件)→ 入侵派遣。订阅在各自的线程里;
        # 报入侵跳回事件循环。
        from d1max_site.cctv import CctvManager, incident_reporter
        self.cctv = CctvManager(self.db, report=incident_reporter(self.incidents, self.loop.submit,
                                                                  wall_ms), now_ms=wall_ms,
                                alerts=loop_alerts)
        def 解不开(robot: str, run: str, rel: str, why: str) -> None:
            loop_alerts.raise_alert(kind="evidence_unopened", robot=robot,
                                    title=f"狗封好的证据解不开:{run}/{rel}",
                                    detail=f"{why};封好的那份留在站点,配好证据私钥后 "
                                           "d1max-site evidence-open 补解")
        self.evidence.on_unopened = self.recordings.on_unopened = 解不开
        self.evidence_watch.alerts = self.alerts
        self.intake.on_refused = lambda robot, run, rel, why: loop_alerts.raise_alert(
            kind="upload_refused", robot=robot, title=f"站点不收 {run}/{rel}",
            detail=f"{why}(那一趟留在狗上,不会自己删)")
        self.api = SiteApi(host=api_host, port=api_port, loop=self.loop,
                           dispatcher=self.dispatcher, accounts=self.accounts, tls=tls,
                           scheduler=self.scheduler, standby=self.standby,
                           incidents=self.incidents, alerts=self.alerts, video=self.video,
                           teleop=self.teleop, runs=self.runs, backup=self.backup,
                           maps=self.maps, releases=self.releases,
                           supervision=self.supervision, zones=self.zones, now_ms=wall_ms)
        self.teleop.audit = self.api.audit
        self.api.arming = self.arming
        self.api.deterrence = self.deterrence
        self.api.weather = self.weather
        from d1max_site.push import PushDesk, load_sender
        sender, why_off = load_sender(cfg)
        if why_off:
            log.warning("P1 不推到手机:%s", why_off)
        #: P1 推到手机(商业化 A6,决策 53:极光,只推标题)。
        self.push = PushDesk(self.db, now_ms=wall_ms, sender=sender,
                             site_name=str(cfg.get("site_name") or cfg.get("site_id") or ""),
                             why_off=why_off)
        self.push.alerts = self._loop_alerts      # 推送在线程里跑:告警经事件循环
        self.alerts.push = sender is not None
        self.api.push = self.push
        from d1max_site import keyvault
        from d1max_site.health import HealthDesk
        #: 体检、指标历史、诊断包(商业化 A1)。
        self._beat = time.monotonic()
        self._lane_started: dict[str, float] = {}
        self.health = HealthDesk(self.db, home=home, now_ms=wall_ms, dispatcher=self.dispatcher,
                                 backup=self.backup, push=self.push,
                                 loop_lag=lambda: time.monotonic() - self._beat,
                                 lanes=lambda: dict(self._lane_started),
                                 keys=lambda: keyvault.status(cfg))      # A3
        self.health.alerts = self._loop_alerts            # 杂事线程里报:经事件循环
        self.api.health = self.health
        self.api.home = home                              # A4:狗领证书包
        from d1max_site.compat import CompatWatch
        #: 狗的版本配不配站点(商业化 A7)。
        self.compat = CompatWatch(self.dispatcher)
        self.compat.alerts = self.alerts
        self.api.privacy = self.privacy                # W30:运行记录标「留着」
        self.api.charge = self.charge                  # W13:充电桩
        self.arming.on_expired = lambda back, row: self.api.audit.record(
            actor="site", action="mode visitor_expired", target=back, status=200,
            detail={"visitor_zones": row["visitor_zones"], "set_by": row["set_by"]}, remote="")
        #: 基站改正数据转发(W09e):配了 ``--rtcm-source`` 才有。
        self.rtk = None
        if rtcm_source:
            from d1max_site.alert_sources import SITE as SITE_ROBOT
            from d1max_site.rtk_relay import RtcmRelay

            def _base_moved(m: float) -> None:
                self.alerts.raise_alert(
                    kind="rtk", robot=SITE_ROBOT,
                    title=f"基站坐标跳了 {m:.2f} m",
                    detail="基站要用固定坐标(自测平均每次重启都会漂);狗上的 RTK 核对会停用")
            self.rtk = RtcmRelay(rtcm_source, now_ms=wall_ms, publish=lambda b: self.loop.submit(
                lambda: self.dispatcher.publish_rtcm(b)), on_base_moved=_base_moved)
        self.api.rtk = self.rtk
        self.api.recordings = self.recordings
        self.api.cctv = self.cctv
        self.cctv.on_changed = self.api.drop_cctv_view   # 删了、改了:正在看的旧画面当场关
        self._stop = threading.Event()
        self._chores = threading.Thread(target=self._chore_loop, daemon=True,
                                        name="site-chores")

    def start(self) -> None:
        self.loop.call(self.dispatcher.start, timeout_s=30)
        self.loop.submit(self._sync_loop)
        self.loop.submit(self._schedule_loop)
        self.loop.submit(self._alert_loop)
        self.loop.submit(self._beat_loop)
        self.api.start()
        self.intake.start()
        self._chores.start()
        try:
            self.cctv.sync()                       # 起来就连摄像头,不等第一拍杂事
        except Exception:
            log.exception("摄像头起不来")
        if self.rtk is not None:
            self.rtk.start()

    def _chore_loop(self) -> None:
        """后台杂事(不在事件循环里:判读要等模型、备份要拷盘):每 30 s 自动判读一拍,
        每拍看一眼备份到点没有。一拍炸了记下来、下一拍照走。"""
        while not self._stop.wait(CHORE_PERIOD_S):
            for what, fn in (("自动判读", self.runs.step), ("备份", self.backup.step),
                             ("备份抽查", lambda: self.backup.verify_step(self._backup_key)),
                             ("录像留存", self._prune_recordings),
                             ("体检", self.health.tick),                  # A1:证书、盘、卡住的道
                             ("摄像头", self.cctv.sync),
                             ("拦截点对账", self.incidents.recheck_intercepts)):
                try:
                    fn()
                except Exception:
                    log.exception("%s这一拍没办成", what)

    _next_rec_prune = 0.0

    def _prune_recordings(self) -> None:
        """录像留存(W18):每 10 分钟删一次过期的、盘紧时删最旧的。"""
        now = time.monotonic()
        if now < self._next_rec_prune:
            return
        self._next_rec_prune = now + 600
        self.recordings.prune()
        self.privacy.prune()                           # W30:证据过了留存期删(连备份)
        self._retry_old_incoming()

    def _retry_old_incoming(self) -> None:
        """以前版本暂存的已删证据半截(PR #87 复查 R3)起来时没删掉的:定时再删,删不掉报 P2,
        删掉了自动解决(PR #88 复查)。"""
        from d1max_site.alert_sources import SITE
        from d1max_site.evidence import drop_old_incoming
        why = []
        for store in (self.evidence, self.recordings):
            if getattr(store, "incoming_error", ""):
                store.incoming_error = drop_old_incoming(store.root)
            if getattr(store, "incoming_error", ""):
                why.append(store.incoming_error)
        # 杂事线程里调:告警经事件循环(A1 顺手修:以前直接用了只许在事件循环里用的告警台)
        alerts = getattr(self, "_loop_alerts", None) or self.alerts
        if why:
            if not alerts.has_open(SITE, "purged_incoming_stuck"):
                alerts.raise_alert(kind="purged_incoming_stuck", robot=SITE,
                                        title="以前暂存的已删证据删不掉",
                                        detail="里面是已经删除的原文,要人看一下权限、盘:"
                                               + ";".join(why)[:250])
        elif alerts.has_open(SITE, "purged_incoming_stuck"):
            alerts.resolve_all(SITE, "purged_incoming_stuck", who="site:incoming_dropped")

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
        """告警:每几秒看一次掉线、让 P1 未确认的升档。一拍炸了记下来、下一拍照走。

        系统审查 S06:**只对账、不等回执的**每拍直接做(掉线、P1 升档、受力报警、看见人、证据、
        访客到点);**要等狗回执的**(驱离、回充、受力警笛、天气限速续发)各走各的道(:meth:`_lane`):上一轮还没跑完
        就这一轮跳过,不叠着跑、也不挡别人 —— 一台掉线的狗收尾等回执,不许拖住别的狗的受力报警、
        掉线检查、限速续期。"""
        from d1max_site.alert_sources import STEP_S
        while not self._stop.is_set():
            await asyncio.sleep(STEP_S)
            # 每一样各自 try(连取部件也在 try 里):一样炸了不带走别的
            for what, part, meth in (("告警", "alert_sources", "step"),
                                     ("入侵告警补报", "incidents", "retell"),    # W16 外审
                                     ("看见人", "sightings", "tick"),            # W33
                                     ("受力", "force", "reconcile"),             # W26
                                     ("证据没封", "evidence_watch", "tick"),     # W30b
                                     ("版本", "compat", "tick"),                 # A7
                                     ("访客到点退回", "arming", "tick")):        # W20
                try:
                    getattr(getattr(self, part), meth)()
                except Exception:
                    log.exception("%s这一拍没办成", what)
            for what, part, meth in (("驱离", "deterrence", "tick"),      # W22:到点收、续声光
                                     ("回充", "charge", "tick"),          # W13
                                     ("受力警笛", "force", "sound"),      # W26:布防时被抱起来
                                     ("天气", "weather", "tick"),         # W29:雷暴撤排程、对账限速
                                     ("推送", "push", "tick")):           # A6:P1 推到手机
                try:
                    # 推送是同步的、要等网络:放线程里;别的本来就是协程
                    self._lane(what, getattr(getattr(self, part), meth), thread=part == "push")
                except Exception:
                    log.exception("%s这一拍没起来", what)
        lanes = [t for t in getattr(self, "_lanes", {}).values() if not t.done()]
        if lanes:
            await asyncio.wait(lanes, timeout=15)

    def _lane(self, name: str, fn: Callable[[], Any], *, thread: bool = False) -> None:
        """``fn`` 放到自己那条道上跑一轮;上一轮还没完就不起新的(不叠着跑,状态机不乱)。
        ``thread``:``fn`` 是同步的、会卡住(要等网络,比如推送)—— 放到线程里跑,不卡事件循环。"""
        if not hasattr(self, "_lanes"):
            self._lanes: dict[str, asyncio.Task] = {}
        t = self._lanes.get(name)
        if t is not None and not t.done():
            return

        started = getattr(self, "_lane_started", None)
        if started is not None:
            started[name] = time.monotonic()           # A1:体检看有没有卡住的道

        async def run() -> None:
            try:
                r = await asyncio.to_thread(fn) if thread else fn()
                if inspect.isawaitable(r):
                    await r
            except Exception:
                log.exception("%s这一拍没办成", name)
            finally:
                if started is not None:
                    started.pop(name, None)
        self._lanes[name] = asyncio.get_running_loop().create_task(run(), name=f"lane:{name}")

    async def _beat_loop(self) -> None:
        """事件循环的心跳(商业化 A1):每秒记一下;体检看它多久没跳,就知道事件循环卡没卡。"""
        while not self._stop.is_set():
            self._beat = time.monotonic()
            await asyncio.sleep(1.0)

    async def _sync_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(SYNC_PERIOD_S)
            try:
                added = await self.dispatcher.sync_robots()
                if added:
                    log.info("新登记的狗挂上了: %s", ", ".join(added))
                sent = await self.dispatcher.sync_zones()
                if sent:
                    log.info("补发区域给: %s", ", ".join(sent))
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
        if self.rtk is not None:
            self.rtk.close()
        for what, fn in (("遥控", self.teleop.close_all), ("API", self.api.stop),
                         ("接收口", self.intake.stop),
                         ("视频", self.video.close),
                         ("摄像头", self.cctv.close),
                         ("摄像头画面", self.api.close_cctv_views),
                         ("待命点", lambda: self.loop.call(self.standby.close, 10)),
                         ("事件台", lambda: self.loop.call(self.incidents.close, 10)),
                         ("派遣器", lambda: self.loop.call(self.dispatcher.close, 10)),
                         ("事件循环", self.loop.stop), ("数据库", self.db.close)):
            try:
                fn()
            except Exception:
                log.exception("收尾:%s 失败,后面的照做", what)


def cmd_serve(home: Path, api_host: str, api_port: int, broker_url: str | None,
              rtcm_source: str | None = None) -> int:
    cfg = _load(home)
    url = broker_url or f"mqtts://127.0.0.1:{cfg['broker_port']}"
    if rtcm_source:
        from d1max_site.rtk_relay import SourceError, parse_source
        try:
            parse_source(rtcm_source)
        except SourceError as exc:
            print(f"d1max-site 起不来: {exc}", file=sys.stderr, flush=True)
            return 2
    srv = Server(home, api_host=api_host, api_port=api_port, broker_url=url,
                 rtcm_source=rtcm_source)
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
    e.add_argument("--code", action="store_true",
                   help="顺带出一个开通码(A4):狗上 d1max-patrol provision --code 一条命令开通")
    e.add_argument("--host", default=None, help="开通码里狗连站点用的主机名(缺省第一个 --hostname)")
    ec = sub.add_parser("enroll-code", help="给已登记的狗重新出一个开通码(A4,旧码作废)")
    ec.add_argument("robot_id")
    ec.add_argument("--host", default=None, help="狗连站点用的主机名(缺省第一个 --hostname)")
    ec.add_argument("--hours", type=float, default=24.0, help="多少小时内有效")
    ra_ = sub.add_parser("robot-auto", help="让狗接自动派遣(排程、入侵、回充、自动回待命点)(W33)")
    ra_.add_argument("robot_id")
    rm_ = sub.add_parser("robot-manual", help="设成只许手动派:站点不自己让它动(W33)")
    rm_.add_argument("robot_id")
    rm_.add_argument("--note", default="")
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
    ca = sub.add_parser("camera-add", help="登记(或改)固定摄像头:它自带的入侵检测 → 入侵派遣(W19)")
    ca.add_argument("name")
    ca.add_argument("--onvif", required=True, help="摄像头或 NVR 的地址(http://ip[:端口])")
    ca.add_argument("--user", default="", help="ONVIF 账号;口令从 D1MAX_CAMERA_PASSWORD 或交互输入")
    ca.add_argument("--zone", required=True, help="这台摄像头报的入侵算哪个防区(W16 的防区)")
    ca.add_argument("--rtsp", default="", help="看实时画面用的 RTSP 地址(不给就没有画面)")
    ca.add_argument("--motion", action="store_true",
                    help="普通「画面动了」也算入侵(默认不算:树影、车灯太容易误报)")
    cr = sub.add_parser("camera-rm", help="删固定摄像头")
    cr.add_argument("name")
    sub.add_parser("camera-list", help="列出固定摄像头(不显示口令)")
    pp = sub.add_parser("privacy-purge",
                        help="按时间段删证据和录像(W30,PDPA:当事人要求删除;连备份;标了留着的不删)")
    pp.add_argument("--since", required=True,
                    help="开始(ISO 时刻,带时区,如 2026-10-08T21:00+08:00)")
    pp.add_argument("--until", required=True, help="结束(同上)")
    pp.add_argument("--robot", default=None, help="只删这台狗的(不给就是所有狗)")
    pp.add_argument("--reason", required=True, help="为什么删:谁要求的、哪一条请求(记审计)")
    eo = sub.add_parser("evidence-open", help="狗封好、站点还没解开的照片、录像补解(W30b)")
    eo.add_argument("--key", default=None, help="证据私钥(缺省按 site.json / /etc/d1max-site)")
    bo = sub.add_parser("backup-open", help="把加密的备份解开到另一个目录(W30,恢复用)")
    bo.add_argument("src", help="备份目录(或其中的一部分)")
    bo.add_argument("dst", help="解到哪儿(要空目录)")
    bo.add_argument("--key", default="/etc/d1max-site/backup.key",
                    help="备份密钥(装机时离线另存的那一份)")
    sub.add_parser("versions", help="站点、每只狗的版本、兼容级别、配不配(A7;狗的要站点在跑)")
    ks = sub.add_parser("keys-status", help="站点密钥在不在、长度、权限、短指纹(A3)")
    ks.add_argument("--json", action="store_true")
    ke = sub.add_parser("keys-export", help="站点密钥打成一个口令加密的托管包,离线保管(A3)")
    ke.add_argument("--out", required=True, help="托管包写到哪儿(U 盘)")
    ki = sub.add_parser("keys-import", help="托管包里的密钥放回 /etc/d1max-site(A3,以 root 跑)")
    ki.add_argument("src", help="托管包")
    ki.add_argument("--force", action="store_true", help="已经有了、而且不一样的也盖(小心)")
    sb = sub.add_parser("support-bundle",
                        help="导出诊断包(A1):体检、版本、打码的配置、日志")
    sb.add_argument("--out", default=None,
                    help="写到哪儿(缺省当前目录 d1max-support-<时刻>.tar.gz)")
    sb.add_argument("--hours", type=int, default=24, help="带最近多少小时的日志")
    bv = sub.add_parser("backup-verify", help="校验备份能不能恢复(A2):库、身份、每一趟、每个文件")
    bv.add_argument("dest", help="备份目录")
    bv.add_argument("--key", default="/etc/d1max-site/backup.key", help="备份密钥")
    bv.add_argument("--sample", type=int, default=None, help="只抽这么多个文件核 MAC(缺省全核)")
    rs = sub.add_parser("restore", help="把备份恢复成站点目录(A2;--home 要是空目录)")
    rs.add_argument("dest", help="备份目录")
    rs.add_argument("--key", default="/etc/d1max-site/backup.key", help="备份密钥")
    bd = sub.add_parser("backup-drill", help="灾备演练(A2):恢复到临时目录、校验、对数、删掉")
    bd.add_argument("--key", default=None, help="备份密钥(缺省按 site.json / /etc/d1max-site)")
    so = sub.add_parser("source-add", help="登记事件源,打印共享密钥")
    so.add_argument("name")
    sr = sub.add_parser("source-rotate", help="换事件源的共享密钥(旧的当场作废),打印新的")
    sr.add_argument("name")
    sm = sub.add_parser("source-rm", help="删事件源(它的回调从此验签不过)")
    sm.add_argument("name")
    sub.add_parser("source-list", help="列出登记过的事件源(不显示密钥)")
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
    aa.add_argument("--display-name", default="", help="给人看的真名(W20)")
    sr = sub.add_parser("set-role", help="改账号的角色")
    sr.add_argument("name")
    sr.add_argument("role", choices=("admin", "guard", "owner"))
    for verb in ("disable", "enable"):
        sub.add_parser(verb, help=f"{verb} 账号").add_argument("name")
    s = sub.add_parser("serve", help="跑站点:派遣器 + 站点 API")
    s.add_argument("--api-host", default="127.0.0.1")
    s.add_argument("--api-port", type=int, default=8443)
    s.add_argument("--broker", default=None, help="默认 mqtts://127.0.0.1:<init 时的端口>")
    s.add_argument("--rtcm-source", default=None,
                   help="自建基站的改正数据(W09e):serial:<设备>:<波特率> 或 "
                        "tcp:<主机>:<端口>;不给不转发")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    home: Path = args.home
    try:
        if args.cmd != "keys-import":      # A3:导回密钥要写 /etc/d1max-site,以 root 跑
            _check_owner(home)
        if args.cmd == "init":
            cmd_init(home, args.site_id, args.hostname, args.broker_port)
            print(f"站点目录建好了: {home}")
            print(f"站点证书指纹(手机添加站点时核对):{cmd_fingerprint(home)}")
        elif args.cmd == "fingerprint":
            print(cmd_fingerprint(home))
        elif args.cmd == "enroll":
            d = cmd_enroll(home, args.robot_id, args.days)
            ev = " evidence-pub.key" if (d / "evidence-pub.key").is_file() else ""
            print(f"证书包: {d}\n拷到狗上: ca.crt robot.crt robot.key → /etc/d1max/tls/,"
                  f"registration.json{ev} → /etc/d1max/")
            if not ev:
                print("注意:站点没配证据私钥,证书包里没有证据公钥 —— 狗上的照片、录像会明文存"
                      "(装机脚本会生成 /etc/d1max-site/evidence.key)")
            if args.code:
                print(_code_text(cmd_enroll_code(home, args.robot_id, args.host, 24.0)))
        elif args.cmd == "enroll-code":
            print(_code_text(cmd_enroll_code(home, args.robot_id, args.host, args.hours)))
        elif args.cmd in ("robot-auto", "robot-manual"):
            print(cmd_robot_service(home, args.robot_id, args.cmd == "robot-manual",
                                    getattr(args, "note", "")))
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
        elif args.cmd == "camera-add":
            pw = ""
            if args.user:
                pw = os.environ.get("D1MAX_CAMERA_PASSWORD") or getpass.getpass("摄像头口令: ")
            cmd_camera(home, "add", name=args.name, onvif=args.onvif, user=args.user,
                       password=pw, zone=args.zone, rtsp=args.rtsp, motion=args.motion)
            print(f"摄像头 {args.name} 登记好了(防区 {args.zone});站点 30 s 内连上它")
        elif args.cmd == "camera-rm":
            if not cmd_camera(home, "rm", name=args.name):
                raise SiteError(f"没有摄像头 {args.name}")
            print(f"摄像头 {args.name} 删了")
        elif args.cmd == "privacy-purge":
            got = cmd_privacy_purge(home, args.since, args.until, args.robot, args.reason)
            print(f"删了 {got['runs']} 趟运行记录、{got['recordings']} 段录像"
                  + (f";标了留着没删:运行记录 {got['held_runs']}、录像 {got['held_recordings']}"
                     if got["held_runs"] or got["held_recordings"] else "")
                  + (f";连带删了 {got['exports']} 份导出" if got.get("exports") else "")
                  + (f";{got['failed']} 样删不掉(看日志,再跑一次)" if got["failed"] else ""))
        elif args.cmd == "evidence-open":
            ok, bad = cmd_evidence_open(home, args.key)
            print(f"解开了 {ok} 个,还解不开 {bad} 个")
            if bad:
                return 1
        elif args.cmd == "versions":
            from d1max_site.compat import versions
            print(json.dumps(versions(None), ensure_ascii=False, indent=1))
            print("(狗的版本要看正在跑的站点:管理员登录后 GET /api/versions)")
        elif args.cmd in ("keys-status", "keys-export", "keys-import"):
            return cmd_keys(home, args)
        elif args.cmd == "support-bundle":
            print(cmd_support_bundle(home, args.out, args.hours))
            return 0
        elif args.cmd == "backup-verify":
            from d1max_site.restore import verify
            r = verify(Path(args.dest), Path(args.key) if Path(args.key).is_file() else None,
                       sample=args.sample)
            print(json.dumps(r.to_dict(), ensure_ascii=False, indent=1))
            return 0 if r.ok else 1
        elif args.cmd == "restore":
            from d1max_site.restore import restore
            from d1max_site.sealbox import SealError
            try:
                r = restore(Path(args.dest),
                            Path(args.key) if Path(args.key).is_file() else None, home)
            except SealError as exc:
                raise SiteError(str(exc)) from exc
            print(json.dumps(r.to_dict(), ensure_ascii=False, indent=1))
            print(f"恢复到 {home}。还要:把离线另存的 secrets.key、evidence.key 放回 "
                  "/etc/d1max-site/;起服务;手机上核一下站点证书指纹(d1max-site fingerprint)"
                  "跟原来一样")
            return 0 if r.ok else 1
        elif args.cmd == "backup-drill":
            r = cmd_backup_drill(home, args.key)
            print(json.dumps(r, ensure_ascii=False, indent=1))
            return 0 if r["ok"] else 1
        elif args.cmd == "backup-open":
            n = cmd_backup_open(Path(args.src), Path(args.dst), Path(args.key))
            print(f"解开了 {n} 个文件 → {args.dst}")
        elif args.cmd == "camera-list":
            print(cmd_camera(home, "list") or "(还没有摄像头)")
        elif args.cmd == "source-add":
            secret = cmd_incident_admin(home, "source", name=args.name)
            print(f"事件源 {args.name} 登记好了。共享密钥"
                  f"(只显示这一次,配到摄像头/NVR 的转发器上)。{_HEX_HINT}\n{secret}")
        elif args.cmd == "source-rotate":
            secret = cmd_incident_admin(home, "source-rotate", name=args.name)
            print(f"事件源 {args.name} 的密钥换好了,旧的已经作废。新密钥"
                  f"(只显示这一次,配到摄像头/NVR 的转发器上)。{_HEX_HINT}\n{secret}")
        elif args.cmd == "source-rm":
            cmd_incident_admin(home, "source-rm", name=args.name)
            print(f"事件源 {args.name} 删了:它的回调从此验签不过")
        elif args.cmd == "source-list":
            print(cmd_incident_admin(home, "source-list") or "(还没有事件源)")
        elif args.cmd == "intercept":
            cmd_incident_admin(home, "intercept", name=args.name, map=args.map, pose=args.pose)
            print(f"拦截点 {args.name} 登记好了")
        elif args.cmd == "zone":
            cmd_incident_admin(home, "zone", zone=args.zone, intercept=args.intercept)
            print(f"防区 {args.zone} → 拦截点 {args.intercept}")
        elif args.cmd in ("add-admin", "add-account"):
            pw = os.environ.get("D1MAX_SITE_PASSWORD") or getpass.getpass("口令: ")
            role = getattr(args, "role", "admin")
            cmd_add_admin(home, args.name, pw, role, getattr(args, "display_name", ""))
            print(f"账号 {args.name}({role})加好了")
        elif args.cmd in ("set-role", "disable", "enable"):
            cmd_account(home, args.cmd, args.name, getattr(args, "role", ""))
            print(f"{args.cmd} {args.name}:好了(这个账号的会话已全部吊销)")
        else:
            return cmd_serve(home, args.api_host, args.api_port, args.broker, args.rtcm_source)
    except (SiteError, CAError, RegistryError, AuthError) as exc:
        print(f"d1max-site: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
