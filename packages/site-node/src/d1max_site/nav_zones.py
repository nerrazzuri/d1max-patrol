"""禁行区、限速区(W10 设计稿 §2):站点是权威,每个几何版本一份,修订号每改 +1。

- 改(``put``):整份换,带 ``base_revision``(改之前看到的修订号),跟库里的对不上 →
  :class:`ZonesConflict`(两个人同时改,后到的不许悄悄盖掉先到的)。
- **发布前人工确认**(``confirm``,W08 决定 5):确认的是「水体、落差(台阶下沿、路缘、池边)、
  陡坡、高楼楼梯都已画成禁行区」这一句,记确认人、时刻、确认的是哪一版修订。
- 换图门槛、派单门槛:这个几何版本的区域**已确认、且确认的就是当前修订**(:meth:`confirmed_current`)。

决策 66(2026-10-14):

- **收紧免确认**:改完跟**最后确认过的那一份**(``confirmed_body``)比是只收紧(:func:`tightens`,
  按几何:加禁行区、放大、加硬禁区标记、调低限速、到期推后)→ 这一版当场算确认了(确认人 = 改的人,
  ``confirmed_how = 'tighten'``,审计里有 ``auto_confirmed``),入侵派遣、巡检照派。**放宽**(删、缩、
  挪、升限速、去掉硬禁区标记、到期提前)或又紧又松的,照旧要管理员确认才派。从没确认过的(新版本)
  收紧也不算确认:那一句还没人说过。
- **临时区**:区域可以带 ``until_ms``(或者接口给 ``for_s`` 时长,按**站点的钟**换成到期时刻 ——
  手机的钟可能不准)。到期由站点删(:meth:`expire`,每 10 秒一轮),删完是新修订、照常发给狗。
  到期是放宽,但到期时间是写在确认过的那一份里的,**算预先确认过**:跟原来在生效的那一份比(到期的不用
  再盖住)还是只收紧就带上确认(``confirmed_how = 'expiry'``,确认人 ``site:expiry``);当前这一版本来
  就没确认的,删完照样没确认。狗不按自己的钟删(狗的钟可能是错的;过期了还守着是往安全那边错)。

外审 W38-I1(2026-10-15):**草稿和在生效的分开**(:meth:`effective`)。原来改了就把新的那一份发给狗
—— 删掉一个硬禁区、还没确认,狗已经收下落了盘,遥控守卫里就没有它了(确认只挡住站点之后派的自主
任务)。现在凡是发给狗、跟狗报的核对的,都用在生效的那一份:当前这一份没确认时,是它再加上最后确认
过的那一份里**没被它盖住**的区域(:func:`~d1max_contract.zones.covered`)—— 狗守手上的每一个区域,
所以收紧的当场生效、放宽的等确认。**同一个修订号的内容不变**(狗不收同号不同内容):确认一份放宽了的,
修订号加一;临时区到期由 :meth:`expire` 从草稿、确认过的、在生效的三份里一起删,也出新修订号。

外审 R1(2026-10-15 下午):在生效的那一份**存下来**(``effective_body``),不再每次临时拼 —— 原来只拼
「草稿 + 最后确认的那一份」,连续改两次就会忘掉上一轮已经生效、但从没确认过的新保护(删泳池加台阶,
再删台阶:台阶没人确认就没了)。现在改草稿时:跟**在生效的**那一份比只收紧(并且有确认过的底)才当场
算确认;否则在生效的 = 新草稿 + 原来在生效、没被新草稿盖住的。只有确认能放宽(到期算预先确认过)。

R2(2026-10-14):**新版本把上一版的东西带过来给人核**(:meth:`proposal`)。新版本(站点目录里有、还没
画过区域)头一回被看的时候,把上一版的区域(没到期的)、待命点、拦截点抄一份**建议**(不生效、不发狗);
手机区域页顶上一条横幅:「从第 X 版带过来的,坐标可能不一样,每一个都要对着新图核」;两版的
``frames.json`` 不一样就明说坐标系变了。确认的时候带 ``adopt``(核过、要留下的那几个)才生效 —— 确认
就是平常的确认。丢掉(:meth:`discard_proposal`)之后不再生成。不做坐标换算。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.zones import MAX_ZONES, ZoneSet, covered, tightens, wall_polygon

log = logging.getLogger(__name__)

CONFIRM_TEXT = "水池、高楼楼梯、陡坡、落差(路缘、池边)都已画成禁行区"
#: 临时区最长多久(决策 66):挡手滑(秒当毫秒、多打几个零)。
MAX_TEMP_MS = 366 * 86_400_000
#: 接口给时长(``for_s``)最短多久。
MIN_TEMP_S = 60
#: 到期删区域、带上确认的那个「人」。
EXPIRY_BY = "site:expiry"
_ID_KEEP = re.compile(r"[^A-Za-z0-9._-]")


class ZonesError(Exception):
    pass


class ZonesConflict(ZonesError):
    pass


class NavZones:
    def __init__(self, db: Any, *, now_ms: Callable[[], int]) -> None:
        self.db = db
        self._now = now_ms

    def _row(self, map_id: str, version: str) -> Any:
        rows = self.db.query("SELECT * FROM nav_zones WHERE map_id=? AND map_version=?",
                             (map_id, version))
        return rows[0] if rows else None

    def current(self, map_id: str, version: str) -> ZoneSet:
        """当前那一份;没画过是修订 0、空的。"""
        r = self._row(map_id, version)
        if r is None:
            return ZoneSet(map_id, version, 0, ())
        return ZoneSet.from_wire(json.loads(r["body"]))

    def effective(self, map_id: str, version: str) -> ZoneSet:
        """**在生效的**那一份(外审 W38-I1、R1):发给狗、跟狗报的核对都用它。见模块说明。"""
        return self._effective_of(self._row(map_id, version), map_id, version)

    def _effective_of(self, r: Any, map_id: str, version: str) -> ZoneSet:
        """存着的那一份(R1);没存过(升上来的老库)按原来的算法:当前这一份 + 确认过的那一份里没被
        盖住的。不按此刻的钟去掉到期的:到期由 :meth:`expire` 删、出新修订号 —— 同一个修订号的内容
        不能自己变(狗不收,摘要也对不上)。"""
        if r is None:
            return ZoneSet(map_id, version, 0, ())
        cur = ZoneSet.from_wire(json.loads(r["body"]))
        raw = r["effective_body"] if "effective_body" in r.keys() else None
        if raw:
            try:
                eff = ZoneSet.from_wire(json.loads(raw))
                if eff.revision == cur.revision:
                    return eff
            except (ValueError, ContractError):
                log.error("%s:%s 存着的在生效的区域坏了:按确认过的那一份重拼", map_id, version)
        ok = self._confirmed_set(r)
        if ok is None or r["confirmed_rev"] == r["revision"]:
            return cur
        return self._merge(ok, cur)

    @staticmethod
    def _merge(prev: ZoneSet, new: ZoneSet) -> ZoneSet:
        """新草稿 + ``prev`` 里**没被新草稿盖住**的(:func:`covered`);同 id 的换个名字一起守。
        装不下(契约最多 MAX_ZONES 个)就整份守 ``prev`` —— 放宽的不生效,这次新加的也要等确认。
        修订号用新的。"""
        held = [z for z in prev.zones if not covered(z, new.zones)]
        if not held:
            return new
        used = {z.id for z in new.zones}
        out = list(new.zones)
        for z in held:
            zid, k = z.id, 1
            while zid in used:                    # 草稿里同名的是放宽了的那个:这个换个名字一起守
                zid = f"{_ID_KEEP.sub('', z.id)[:54]}-held{k}"
                k += 1
            used.add(zid)
            out.append(z if zid == z.id else replace(z, id=zid))
        if len(out) > MAX_ZONES:
            log.warning("%s:%s 在生效的区域超过 %d 个:先按上一份在生效的守", new.map_id,
                        new.map_version, MAX_ZONES)
            return ZoneSet(new.map_id, new.map_version, new.revision, prev.zones)
        return ZoneSet(new.map_id, new.map_version, new.revision, tuple(out))

    @staticmethod
    def _store_effective(tx: Any, eff: ZoneSet) -> None:
        tx.execute("UPDATE nav_zones SET effective_body=? WHERE map_id=? AND map_version=?",
                   (json.dumps(eff.to_wire(), ensure_ascii=False), eff.map_id, eff.map_version))

    def view(self, map_id: str, version: str) -> dict[str, Any]:
        r = self._row(map_id, version)
        zs = self.current(map_id, version)
        out: dict[str, Any] = zs.to_wire() | {"confirm_text": CONFIRM_TEXT, "confirmed": None,
                                              "updated_by": None, "updated_ms": None}
        # 还没确认、狗上照旧守着的(确认过的那一份里被这次删了、缩了、放松了的):
        # 给人看「确认之后才生效」
        eff = self._effective_of(r, map_id, version)
        out["held"] = [z.to_wire() for z in eff.zones if z not in zs.zones]
        if r is not None:
            out["updated_by"], out["updated_ms"] = r["updated_by"], r["updated_ms"]
            if r["confirmed_rev"] is not None:
                out["confirmed"] = {"revision": r["confirmed_rev"], "by": r["confirmed_by"],
                                    "at_ms": r["confirmed_ms"],
                                    "current": r["confirmed_rev"] == r["revision"],
                                    "how": r["confirmed_how"] or "confirm"}
        out["proposal"] = self.proposal(map_id, version)
        prev = self._previous(map_id, version)
        out["previous"] = None
        if prev is not None:
            pz = self.current(map_id, prev["version"])
            out["previous"] = {"version": prev["version"], "revision": pz.revision,
                               "zones": len(pz.zones)}
        return out

    # ------------------------------------------------------------ 改

    def _prepare(self, zones: Any, cur: ZoneSet, now: int) -> Any:
        """虚拟墙:带 ``wall`` 的按墙线算多边形(:func:`~d1max_contract.zones.wall_polygon`)。
        临时区(决策 66):``for_s`` 按站点的钟换成 ``until_ms``;到期时刻要在以后、不超过一年。已经
        到期、站点还没来得及删的那一个原样带回来(手机拿的是删之前那一份)就悄悄去掉。"""
        if not isinstance(zones, list):
            return zones
        stale = {(z.id, z.until_ms) for z in cur.zones if z.expired(now)}
        out = []
        for d in zones:
            if not isinstance(d, dict):
                out.append(d)
                continue
            d = dict(d)
            if d.get("wall") is not None:
                # 虚拟墙(2026-10-15):多边形一律由站点按墙线算(客户端带来的不信),狗按多边形守
                try:
                    d["polygon"] = [list(p) for p in wall_polygon(d["wall"])]
                except ContractError as exc:
                    raise ZonesError(f"区域 {d.get('id')}:{exc}") from exc
            if "for_s" in d:
                f = d.pop("for_s")
                if d.get("until_ms") is not None:
                    raise ZonesError(f"区域 {d.get('id')}:for_s 跟 until_ms 只给一个")
                if isinstance(f, bool) or not isinstance(f, int) \
                        or not MIN_TEMP_S <= f <= MAX_TEMP_MS // 1000:
                    raise ZonesError(f"区域 {d.get('id')}:for_s 要是 {MIN_TEMP_S} 秒到一年之间的"
                                     "整数")
                d["until_ms"] = now + f * 1000
            u = d.get("until_ms")
            if isinstance(u, int) and not isinstance(u, bool):
                if u <= now:
                    if (d.get("id"), u) in stale:
                        continue
                    raise ZonesError(f"区域 {d.get('id')}:到期时间已过了(按站点的钟)")
                if u > now + MAX_TEMP_MS:
                    raise ZonesError(f"区域 {d.get('id')}:到期时间太远(最多一年)")
            out.append(d)
        return out

    @staticmethod
    def _confirmed_set(r: Any) -> ZoneSet | None:
        if r is None or not r["confirmed_body"]:
            return None
        try:
            return ZoneSet.from_wire(json.loads(r["confirmed_body"]))
        except (ValueError, ContractError):
            return None

    def put(self, map_id: str, version: str, zones: Any, *, base_revision: Any,
            by: str) -> tuple[ZoneSet, bool]:
        """整份换 → (新的那份, 是不是比上一版只收紧)。``base_revision`` 对不上抛
        :class:`ZonesConflict`;区域不合规矩抛 :class:`ZonesError`。跟最后确认过的那一份比
        只收紧的,当场算确认了(决策 66;调用方拿 :meth:`confirmed_current` 看)。"""
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            raise ZonesError("base_revision 要是整数(改之前看到的修订号)")
        now = self._now()
        with self.db.tx() as tx:
            rows = tx.execute("SELECT * FROM nav_zones WHERE map_id=? AND map_version=?",
                              (map_id, version)).fetchall()
            cur = (ZoneSet.from_wire(json.loads(rows[0]["body"])) if rows
                   else ZoneSet(map_id, version, 0, ()))
            if base_revision != cur.revision:
                raise ZonesConflict(f"区域已被修改(现在是第 {cur.revision} 版,你看到的是第 "
                                    f"{base_revision} 版):请刷新后再改")
            try:
                new = ZoneSet.from_wire({"map_id": map_id, "map_version": version,
                                         "revision": cur.revision + 1,
                                         "zones": self._prepare(zones, cur, now)})
            except ContractError as exc:
                raise ZonesError(str(exc)) from exc
            prev_eff = self._effective_of(rows[0] if rows else None, map_id, version)
            body = json.dumps(new.to_wire(), ensure_ascii=False)
            if rows:
                tx.execute("UPDATE nav_zones SET revision=?, body=?, updated_by=?, updated_ms=? "
                           "WHERE map_id=? AND map_version=?",
                           (new.revision, body, by, now, map_id, version))
            else:
                tx.execute("INSERT INTO nav_zones(map_id, map_version, revision, body, updated_by,"
                           " updated_ms) VALUES (?,?,?,?,?,?)",
                           (map_id, version, new.revision, body, by, now))
            ok = self._confirmed_set(rows[0] if rows else None)
            if ok is not None and tightens(prev_eff, new, now_ms=now):
                # 决策 66 + 外审 R1:跟**在生效的**那一份比只收紧(它本身不比最后确认的松,所以也比最后
                # 确认的只收紧)→ 改的人就算确认了这一版
                self._mark_confirmed(tx, map_id, version, new, by, now, "tighten")
                self._store_effective(tx, new)
            else:
                # 放宽、混合、从没确认过:新的当场生效,原来在生效、没被盖住的照旧守,等确认
                self._store_effective(tx, self._merge(prev_eff, new))
        return new, tightens(cur, new, now_ms=now)

    @staticmethod
    def _mark_confirmed(tx: Any, map_id: str, version: str, zs: ZoneSet, by: str, now: int,
                        how: str) -> None:
        tx.execute("UPDATE nav_zones SET confirmed_rev=?, confirmed_by=?, confirmed_ms=?, "
                   "confirmed_body=?, confirmed_how=? WHERE map_id=? AND map_version=?",
                   (zs.revision, by, now, json.dumps(zs.to_wire(), ensure_ascii=False), how,
                    map_id, version))

    # ------------------------------------------------------------ 确认

    def confirm(self, map_id: str, version: str, *, revision: Any, by: str,
                adopt: Any = None) -> dict[str, Any]:
        """人工确认这一版修订(没画过的就是确认第 0 版:这张图没什么要画的)。

        ``adopt``(R2):``{"zones": [带过来的建议里核过、要留下的区域 id]}`` —— 并进当前这一份
        (id 撞了换一个)、修订 +1、确认的就是并完的那一版、建议收起来。
        回 ``{"zones": {原 id: 新 id}}``。
        待命点、拦截点由调用方按建议另外搬(它们有自己的校验)。"""
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ZonesError("revision 要是整数(确认的是哪一版)")
        if adopt is not None and not isinstance(adopt, dict):
            raise ZonesError("adopt 要是对象")
        now = self._now()
        report: dict[str, Any] = {"zones": {}}
        with self.db.tx() as tx:
            rows = tx.execute("SELECT * FROM nav_zones WHERE map_id=? AND "
                              "map_version=?", (map_id, version)).fetchall()
            cur = (ZoneSet.from_wire(json.loads(rows[0]["body"])) if rows
                   else ZoneSet(map_id, version, 0, ()))
            if revision != cur.revision:
                raise ZonesConflict(f"区域现在是第 {cur.revision} 版,确认的是第 {revision} 版:"
                                    "看过最新的再确认")
            new = cur
            if adopt is not None:
                new = self._adopt(tx, map_id, version, cur, adopt, by, now, report)
            if new is cur and rows and \
                    self._effective_of(rows[0], map_id, version).zones != cur.zones:
                # 外审 W38-I1:确认的是一份放宽了的,狗上守着的(在生效的)跟它不一样 —— 内容变了要新的
                # 修订号(狗同一个修订号不收不一样的内容)。区域本身没人改:改的人、时刻不动
                new = ZoneSet(map_id, version, cur.revision + 1, cur.zones)
                tx.execute("UPDATE nav_zones SET revision=?, body=? WHERE map_id=? AND "
                           "map_version=?", (new.revision, json.dumps(new.to_wire(),
                                                                      ensure_ascii=False),
                                             map_id, version))
                cur = new
            body = json.dumps(new.to_wire(), ensure_ascii=False)
            if rows:
                if new is not cur:
                    tx.execute("UPDATE nav_zones SET revision=?, body=?, updated_by=?, "
                               "updated_ms=? WHERE map_id=? AND map_version=?",
                               (new.revision, body, by, now, map_id, version))
            else:
                tx.execute("INSERT INTO nav_zones(map_id, map_version, revision, body, updated_by,"
                           " updated_ms) VALUES (?,?,?,?,?,?)",
                           (map_id, version, new.revision, body, by, now))
            self._mark_confirmed(tx, map_id, version, new, by, now, "confirm")
            self._store_effective(tx, new)
        return report

    def _adopt(self, tx: Any, map_id: str, version: str, cur: ZoneSet, adopt: dict, by: str,
               now: int, report: dict[str, Any]) -> ZoneSet:
        prow = tx.execute("SELECT body FROM nav_zone_proposals WHERE map_id=? AND map_version=? "
                          "AND state='open'", (map_id, version)).fetchone()
        if prow is None:
            raise ZonesError(f"{map_id}:{version} 没有待核的、从上一版带过来的东西")
        prop = json.loads(prow["body"])
        offered = {z.get("id"): z for z in prop.get("zones", []) if isinstance(z, dict)}
        ids = adopt.get("zones", [])
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) \
                or len(set(ids)) != len(ids):
            raise ZonesError("adopt.zones 要是不重复的区域 id 列表")
        missing = [i for i in ids if i not in offered]
        if missing:
            raise ZonesError(f"带过来的建议里没有这几个区域:{', '.join(missing)}")
        merged = [z.to_wire() for z in cur.zones]
        used = {z.id for z in cur.zones}
        for zid in ids:
            z = dict(offered[zid])
            u = z.get("until_ms")
            if isinstance(u, int) and not isinstance(u, bool) and u <= now:
                report["zones"][zid] = ""                  # 带过来之后到期了:不要
                continue
            nid, k = zid, 1
            while nid in used:
                nid = f"{_ID_KEEP.sub('', zid)[:56]}-c{k}"
                k += 1
            used.add(nid)
            merged.append(z | {"id": nid})
            report["zones"][zid] = nid
        tx.execute("UPDATE nav_zone_proposals SET state='adopted', closed_by=?, closed_ms=? "
                   "WHERE map_id=? AND map_version=?", (by, now, map_id, version))
        if not any(report["zones"].values()):
            return cur
        try:
            return ZoneSet.from_wire({"map_id": map_id, "map_version": version,
                                      "revision": cur.revision + 1, "zones": merged})
        except ContractError as exc:
            raise ZonesError(f"带过来的区域并不进去:{exc}") from exc

    def confirmed_current(self, map_id: str, version: str) -> bool:
        r = self._row(map_id, version)
        return r is not None and r["confirmed_rev"] is not None \
            and r["confirmed_rev"] == r["revision"]

    # ------------------------------------------------------------ 临时区到期(决策 66)

    def expire(self) -> list[ZoneSet]:
        """删掉到期了的临时区:每个有到期区域的几何版本出一个新修订(``updated_by = site:expiry``);
        跟原来**在生效的**那一份(去掉到期的)比还是只收紧的,带上确认(``confirmed_how = 'expiry'``),
        还有别的放宽等人确认的照旧不确认(外审 R1-续1)。回改了的那几份(调用方发给狗)。"""
        now = self._now()
        out: list[ZoneSet] = []
        for r in self.db.query("SELECT map_id, map_version FROM nav_zones "
                               "WHERE body LIKE '%until_ms%' OR confirmed_body LIKE '%until_ms%' "
                               "OR effective_body LIKE '%until_ms%'"):
            with self.db.tx() as tx:
                row = tx.execute("SELECT * FROM nav_zones WHERE map_id=? AND map_version=?",
                                 (r["map_id"], r["map_version"])).fetchone()
                if row is None:
                    continue
                cur = ZoneSet.from_wire(json.loads(row["body"]))
                keep = cur.without_expired(now)
                ok = self._confirmed_set(row)
                ok_keep = ok.without_expired(now) if ok is not None else None
                eff = self._effective_of(row, cur.map_id, cur.map_version)
                eff_keep = eff.without_expired(now)
                # 外审 W38-I1、R1:确认过的、在生效的那一份里到期的也删(没确认的放宽期间狗还替它
                # 守着):
                # 在生效的内容变了,同样出新修订号
                if keep is cur and ok_keep is ok and eff_keep is eff:
                    continue
                new = ZoneSet(cur.map_id, cur.map_version, cur.revision + 1, keep.zones)
                tx.execute("UPDATE nav_zones SET revision=?, body=?, updated_by=?, updated_ms=? "
                           "WHERE map_id=? AND map_version=?",
                           (new.revision, json.dumps(new.to_wire(), ensure_ascii=False),
                            EXPIRY_BY, now, new.map_id, new.map_version))
                # 外审 R1-续1:跟**在生效的**那一份比(到期的不用再盖),不跟最后确认的比 —— 不然一个
                # 不相干的临时区到期,会把另一项还等人确认的放宽(删永久硬禁区、缩、调高限速)一起批了
                confirmed = ok is not None and tightens(eff, new, now_ms=now)
                if confirmed:
                    self._mark_confirmed(tx, new.map_id, new.map_version, new, EXPIRY_BY, now,
                                         "expiry")
                    self._store_effective(tx, new)
                else:
                    if ok_keep is not ok:
                        tx.execute("UPDATE nav_zones SET confirmed_body=? WHERE map_id=? AND "
                                   "map_version=?", (json.dumps(ok_keep.to_wire(),
                                                                ensure_ascii=False),
                                                     new.map_id, new.map_version))
                    self._store_effective(tx, self._merge(
                        ZoneSet(new.map_id, new.map_version, new.revision, eff_keep.zones), new))
            gone = sorted({z.id for z in cur.zones} - {z.id for z in new.zones})
            self._audit("zones_expired", f"{new.map_id}:{new.map_version}",
                        {"revision": new.revision, "removed": gone, "confirmed": confirmed})
            out.append(new)
        return out

    def _audit(self, action: str, target: str, detail: dict[str, Any]) -> None:
        from d1max_site.audit import AuditLog
        AuditLog(self.db, now_ms=self._now).record(actor=EXPIRY_BY, action=action, target=target,
                                                   status=200, detail=detail)

    # ------------------------------------------------------------ R2:新版本带过来的建议

    def _previous(self, map_id: str, version: str) -> Any:
        """站点目录里这一版之前的那一版(按登记时刻);这一版不在目录里、是第一版 → ``None``。"""
        try:
            me = self.db.query("SELECT rowid AS k, created_ms FROM maps WHERE map_id=? AND "
                               "version=?", (map_id, version))
            if not me:
                return None
            t, k = me[0]["created_ms"], me[0]["k"]
            rows = self.db.query(
                "SELECT version, files FROM maps WHERE map_id=? AND (created_ms<? OR "
                "(created_ms=? AND rowid<?)) ORDER BY created_ms DESC, rowid DESC LIMIT 1",
                (map_id, t, t, k))
        except Exception:  # noqa: BLE001 —— 没有地图目录的老库:当没有上一版
            return None
        return rows[0] if rows else None

    def proposal(self, map_id: str, version: str) -> dict[str, Any] | None:
        """这一版待核的建议(没有、采纳过、丢掉了 → ``None``)。全新的版本头一回问的时候生成。"""
        self._ensure_proposal(map_id, version)
        rows = self.db.query("SELECT * FROM nav_zone_proposals WHERE map_id=? AND map_version=?",
                             (map_id, version))
        if not rows or rows[0]["state"] != "open":
            return None
        return json.loads(rows[0]["body"]) | {"from_version": rows[0]["from_version"],
                                              "created_ms": rows[0]["created_ms"]}

    def _ensure_proposal(self, map_id: str, version: str) -> None:
        if self.db.query("SELECT 1 FROM nav_zone_proposals WHERE map_id=? AND map_version=?",
                         (map_id, version)) or self._row(map_id, version) is not None:
            return                                   # 生成过;或者这一版已经画过、确认过(不是全新的)
        prev = self._previous(map_id, version)
        if prev is None:
            return
        pv = prev["version"]
        now = self._now()
        pz = self.current(map_id, pv)
        zones = [z.to_wire() for z in pz.zones if not z.expired(now)]
        standby = [{"robot_id": r["robot_id"], "name": r["name"], "x": r["x"], "y": r["y"],
                    "yaw": r["yaw"], "default": bool(r["is_default"])}
                   for r in self.db.query(
                       "SELECT * FROM standby_points WHERE map_id=? AND map_version=? "
                       "ORDER BY robot_id, name", (map_id, pv))]
        intercepts = [{"name": r["name"], "x": r["x"], "y": r["y"], "yaw": r["yaw"]}
                      for r in self.db.query(
                          "SELECT * FROM intercepts WHERE map_id=? AND map_version=? "
                          "ORDER BY name", (map_id, pv))]
        if not (zones or standby or intercepts):
            return
        me = self.db.query("SELECT files FROM maps WHERE map_id=? AND version=?",
                           (map_id, version))
        body = {"from_revision": pz.revision, "zones": zones, "standby": standby,
                "intercepts": intercepts,
                "frame": _frame(me[0]["files"] if me else "[]", prev["files"], pv)}
        with self.db.tx() as tx:
            tx.execute("INSERT OR IGNORE INTO nav_zone_proposals(map_id, map_version, "
                       "from_version, body, created_ms) VALUES (?,?,?,?,?)",
                       (map_id, version, pv, json.dumps(body, ensure_ascii=False), now))

    def discard_proposal(self, map_id: str, version: str, *, by: str) -> None:
        self._ensure_proposal(map_id, version)
        with self.db.tx() as tx:
            n = tx.execute("UPDATE nav_zone_proposals SET state='discarded', closed_by=?, "
                           "closed_ms=? WHERE map_id=? AND map_version=? AND state='open'",
                           (by, self._now(), map_id, version)).rowcount
        if not n:
            raise ZonesError(f"{map_id}:{version} 没有待核的、从上一版带过来的东西")


def _frame(mine: str, prev: str, prev_version: str) -> dict[str, Any]:
    """两版的坐标系对不对得上(R2):只看 ``frames.json``(雷达装法、哪边是上 —— 镜像的图就差在这)。
    一样也不说明坐标一样(重新建的图起点不同、回环会挪),所以三种都要人核。"""
    def sha(files: str) -> str | None:
        try:
            for f in json.loads(files or "[]"):
                if isinstance(f, dict) and f.get("name") == "frames.json":
                    return str(f.get("sha256"))
        except ValueError:
            pass
        return None
    a, b = sha(mine), sha(prev)
    if a and b and a == b:
        return {"same": True, "note": f"坐标系参数(frames.json)跟第 {prev_version} 版一样;"
                                      "重新建的图坐标照样可能整体挪了,每一个都要对着新图核"}
    if a and b:
        return {"same": False, "note": f"坐标系参数(frames.json)跟第 {prev_version} 版不同:"
                                       "带过来的坐标很可能是错的,不匹配的删掉重画"}
    return {"same": None, "note": "看不出两版坐标系是不是一样(有一版没有 frames.json):"
                                  "每一个都要对着新图核"}
