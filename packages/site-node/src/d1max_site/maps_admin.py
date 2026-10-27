"""地图管理页的后端(管理员):每张图、每个版本是什么、哪几只狗在用、发布 / 回退 / 删除 / 对比。

不动 ``MapCatalog`` 的登记规矩(同一个地图号 + 版本只登记一次);这里只在它上面加管理动作。

**每只狗的换图状态不另存表**,从命令账和事件里推(站点库结构不动):

- ``pending``:发过 ``map_activate``、狗收下了、还没有 ``map_activated`` / ``map_activate_failed``,
  而且没超过 :data:`PENDING_MAX_MS`;
- ``confirmed``:狗发了 ``map_activated``。新代理的事件带 ``confirmed``:``localizer`` = 定位器
  确认收下了新图的先验(R5);``load`` = 这只狗没有定位器,载入成功就算;没有这个字段的是老代理
  (在定位器确认**之前**就报成功),界面标「老代理,没等定位器确认」;
- ``failed``:狗拒收命令(回执不是 accepted),或者发了 ``map_activate_failed``(原因随事件;定位器不收
  的,狗已经退回原来那张);
- ``timeout``:收下了但过了 :data:`PENDING_MAX_MS` 还没动静(狗重启过?),不算成功。

回退 = 让狗换回它上一张。狗新代理的能力 ``running_config.previous`` 说本地留着上一张(不用重新下载);
老代理没有这项,就按命令账里它上一次成功换过的那一版(狗那头可能要重新下载,界面写明)。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from d1max_contract.errors import ContractError
from d1max_contract.maps import MapRef, check_name

log = logging.getLogger(__name__)

#: 狗收下换图命令之后多久还没有结果,当作没成(毫秒)。下载大先验可以很久,所以放得宽。
PENDING_MAX_MS = 40 * 60_000
#: 对比结果缓存几份。
DIFF_CACHE = 4


class AdminError(ValueError):
    """管理动作做不了。``status`` 是给 HTTP 的状态码,``text`` 给人看;``extra`` 跟着回给客户端
    (``reason`` 是给程序看的原因代码)。"""

    def __init__(self, status: int, text: str, **extra: Any) -> None:
        super().__init__(text)
        self.status, self.text, self.extra = status, text, extra


class MapAdmin:
    def __init__(self, site: Any) -> None:
        self.site = site
        self._diffs: dict[tuple, Any] = {}

    # ------------------------------------------------------------ 取用

    @property
    def cat(self) -> Any:
        if self.site.maps is None:
            raise AdminError(404, "这个站点没开地图目录")
        return self.site.maps

    @property
    def db(self) -> Any:
        return self.site.dispatcher.db

    def _now(self) -> int:
        return self.site.dispatcher._now()

    def robot_ids(self) -> list[str]:
        reg = self.site.dispatcher.registry
        return [r.robot_id for r in reg.list() if not r.revoked]

    # ------------------------------------------ 每只狗的换图状态(从命令账和事件推)

    def activations(self, robot_id: str, limit: int = 12) -> list[dict[str, Any]]:
        """这只狗最近的换图,新的在前:``{map_id, version, state, at_ms, by, task_id, reason,
        confirmed}``。"""
        rows = self.db.query(
            "SELECT task_id, payload, issued_by, issued_at, ack_result, ack_reason FROM commands "
            "WHERE robot_id=? AND kind='map_activate' ORDER BY issued_at DESC, rowid DESC LIMIT ?",
            (robot_id, limit))
        if not rows:
            return []
        done: dict[str, dict[str, Any]] = {}
        for e in self.db.query(
                "SELECT kind, data FROM events WHERE robot_id=? AND kind IN "
                "('map_activated', 'map_activate_failed') ORDER BY stamp DESC, seq DESC LIMIT 200",
                (robot_id,)):
            try:
                d = json.loads(e["data"])
            except (TypeError, ValueError):
                continue
            tid = d.get("task_id") if isinstance(d, dict) else None
            if isinstance(tid, str) and tid not in done:
                done[tid] = {"kind": e["kind"], "data": d}
        now = self._now()
        out = []
        for r in rows:
            try:
                p = json.loads(r["payload"])
                map_id, version = str(p.get("map_id")), str(p.get("version"))
            except (TypeError, ValueError, AttributeError):
                continue
            item: dict[str, Any] = {"map_id": map_id, "version": version, "at_ms": r["issued_at"],
                                    "by": r["issued_by"], "task_id": r["task_id"], "reason": "",
                                    "confirmed": None}
            ev = done.get(r["task_id"])
            if r["ack_result"] not in (None, "", "accepted", "duplicate"):
                item["state"], item["reason"] = "failed", str(r["ack_reason"] or r["ack_result"])
            elif ev is not None and ev["kind"] == "map_activated":
                item["state"] = "confirmed"
                item["confirmed"] = ev["data"].get("confirmed") or "legacy"
            elif ev is not None:
                item["state"] = "failed"
                item["reason"] = str(ev["data"].get("reason", ""))[:300]
            elif now - r["issued_at"] > PENDING_MAX_MS:
                item["state"] = "timeout"
            else:
                item["state"] = "pending"
            out.append(item)
        return out

    def _loaded(self, robot_id: str) -> tuple[str, str] | None:
        """狗**现在**加载的版本(它自己报的):先看 ``running_config``,再看 ``patrol`` 的地图字段。"""
        c = self.site.dispatcher.clients.get(robot_id)
        caps = c.capabilities if c is not None else None
        if caps is None:
            return None
        rc = caps.tasks.get("running_config")
        if isinstance(rc, dict) and isinstance(rc.get("map_id"), str) \
                and isinstance(rc.get("map_version"), str):
            return rc["map_id"], rc["map_version"]
        return self.site.dispatcher._loaded(c)

    def _in_use(self) -> dict[tuple[str, str], list[str]]:
        """每一版有哪几只狗在用:狗报的;狗还没报过能力的(站点刚起来)按它最近一次**成功**的换图算
        (宁可多算 —— 这里决定能不能删)。"""
        out: dict[tuple[str, str], list[str]] = {}
        for rid in self.robot_ids():
            cur = self._loaded(rid)
            if cur is None:
                c = self.site.dispatcher.clients.get(rid)
                if c is None or c.capabilities is None:
                    done = [a for a in self.activations(rid) if a["state"] == "confirmed"]
                    if done:
                        cur = (done[0]["map_id"], done[0]["version"])
            if cur is not None:
                out.setdefault(cur, []).append(rid)
        return out

    def _pending_targets(self) -> set[tuple[str, str]]:
        out = set()
        for rid in self.robot_ids():
            for a in self.activations(rid, 3):
                if a["state"] == "pending":
                    out.add((a["map_id"], a["version"]))
        return out

    # ------------------------------------------------------------ 总览

    def overview(self) -> dict[str, Any]:
        cat = self.cat
        in_use = self._in_use()
        previous_on: dict[tuple[str, str], list[str]] = {}
        robots = []
        d = self.site.dispatcher
        for rid in self.robot_ids():
            c = d.clients.get(rid)
            run = d.running_view(c)
            cur = self._loaded(rid)
            acts = self.activations(rid, 5)
            target = self.rollback_target(rid)
            if target is not None:
                previous_on.setdefault((target["map_id"], target["version"]), []).append(rid)
            robots.append({
                "robot_id": rid,
                "online": bool(c is not None and c.status is not None and c.status.online
                               and d._fresh(c)),
                "can_activate": bool(c is not None and c.capabilities is not None
                                     and "map_activate" in c.capabilities.tasks),
                "loaded": None if cur is None else {"map_id": cur[0], "version": cur[1]},
                "running": run,
                "activation": acts[0] if acts else None,
                "history": acts,
                "rollback": target,
            })
        maps: dict[str, list[dict[str, Any]]] = {}
        for m in cat.list():                     # 新的在前
            maps.setdefault(m["map_id"], []).append(m)
        out_maps = []
        for map_id, vs in maps.items():
            versions = []
            for i, m in enumerate(vs):
                key = (map_id, m["version"])
                zs = self.site.zones.current(*key) if self.site.zones is not None else None
                confirmed = (self.site.zones.confirmed_current(*key)
                             if self.site.zones is not None else None)
                versions.append({
                    "version": m["version"], "created_ms": m["created_ms"], "source": m["source"],
                    "note": m.get("note") or "",
                    "size_bytes": sum(int(f.get("size", 0)) for f in m["files"]),
                    "latest": i == 0, "build": cat.build_info(map_id, m["version"]),
                    "counts": {"zones": len(zs.zones) if zs else 0,
                               "nogo": len(zs.nogo()) if zs else 0,
                               "slow": len(zs.slow()) if zs else 0,
                               "zones_rev": zs.revision if zs else 0,
                               "zones_confirmed": confirmed,
                               "standby": self._count("standby_points", "map_version", key),
                               "intercepts": self._count("intercepts", "map_version", key)},
                    "loaded_on": in_use.get(key, []),
                    "previous_on": previous_on.get(key, []),
                    "thumbnail": f"/api/maps/{_q(map_id)}/{_q(m['version'])}/preview.png",
                    "has_raster": any(f["name"] == "floor.yaml" for f in m["files"])})
            out_maps.append({"map_id": map_id, "versions": versions})
        return {"maps": out_maps, "robots": robots, "now_ms": self._now()}

    def _count(self, table: str, ver_col: str, key: tuple[str, str]) -> int:
        rows = self.db.query(f"SELECT COUNT(*) AS n FROM {table} WHERE map_id=? AND {ver_col}=?",
                             key)
        return int(rows[0]["n"])

    # ------------------------------------------------------------ 备注

    def set_note(self, map_id: str, version: str, note: Any) -> str:
        if not isinstance(note, str):
            raise AdminError(400, "note 要是字符串")
        from d1max_site.maps import MapError
        try:
            return self.cat.set_note(*self._names(map_id, version), note)
        except MapError as exc:
            raise AdminError(404, str(exc)) from exc

    @staticmethod
    def _names(map_id: str, version: str) -> tuple[str, str]:
        try:
            return check_name(map_id, "地图号"), check_name(version, "版本")
        except ContractError as exc:
            raise AdminError(400, str(exc)) from exc

    # ------------------------------------------------------------ 发布 / 回退

    def ref(self, map_id: str, version: str) -> MapRef:
        from d1max_site.maps import MapError
        try:
            return self.cat.get(*self._names(map_id, version))
        except MapError as exc:
            raise AdminError(404, str(exc)) from exc

    def rollback_target(self, robot_id: str) -> dict[str, Any] | None:
        """这只狗回退能回到哪一版:``{map_id, version, local}``。``local`` = 狗说它本地留着(不用重新
        下载);不然是命令账里它上一次成功换过的另一版(可能要重新下载);站点目录里已经没有的版本不算。"""
        cat = self.cat
        cur = self._loaded(robot_id)
        c = self.site.dispatcher.clients.get(robot_id)
        caps = c.capabilities if c is not None else None
        rc = caps.tasks.get("running_config") if caps is not None else None
        prev = rc.get("previous") if isinstance(rc, dict) else None
        if isinstance(prev, list) and len(prev) == 2 and all(isinstance(x, str) for x in prev) \
                and self._known(cat, *prev):
            return {"map_id": prev[0], "version": prev[1], "local": True}
        for a in self.activations(robot_id, 12):
            if a["state"] == "confirmed" and (a["map_id"], a["version"]) != cur \
                    and self._known(cat, a["map_id"], a["version"]):
                return {"map_id": a["map_id"], "version": a["version"], "local": False}
        return None

    @staticmethod
    def _known(cat: Any, map_id: str, version: str) -> bool:
        try:
            cat.get(map_id, version)
            return True
        except Exception:  # noqa: BLE001 —— MapError / ContractError:站点没有这一版
            return False

    async def send_all(self, jobs: list[tuple[str, dict[str, Any]]], *, issued_by: str
                       ) -> list[dict[str, Any]]:
        """把已经备好的 ``map_activate`` 命令一起发出去(每只狗一条),每只狗一个结果。运行在站点的
        事件循环里。狗收下 = ``pending``(后台下载、载入、等定位器;结果看总览里的换图状态)。"""
        import asyncio

        from d1max_contract.dispatch import DispatchTimeout
        from d1max_site.dispatcher import DispatchRefused, Unsupported
        d = self.site.dispatcher

        async def one(rid: str, payload: dict[str, Any]) -> dict[str, Any]:
            try:
                r = await d.map_command(rid, "map_activate", payload, issued_by=issued_by)
            except (DispatchRefused, Unsupported) as exc:
                return {"robot_id": rid, "ok": False, "state": "refused", "reason": str(exc)}
            except (DispatchTimeout, TimeoutError) as exc:
                return {"robot_id": rid, "ok": False, "state": "refused",
                        "reason": f"等机器人的回执超时: {exc}"}
            ack = r.get("ack", {}) if isinstance(r, dict) else {}
            ok = ack.get("result") in ("accepted", "duplicate")
            return {"robot_id": rid, "ok": ok, "state": "pending" if ok else "refused",
                    "reason": "" if ok else str(ack.get("reason") or ack.get("result")),
                    "task_id": r.get("task_id") if isinstance(r, dict) else None}
        return list(await asyncio.gather(*(one(rid, p) for rid, p in jobs)))

    # ------------------------------------------------------------ 删除

    # ------------------------------------------------------------ 清噪点(W41)

    def _grid(self, map_id: str, version: str) -> tuple[str, dict[str, Any], str, bytes]:
        from d1max_site import map_preview as mp
        from d1max_site.maps import MapError
        self.ref(map_id, version)
        try:
            yname, meta, image, data, _, _ = self.cat._raster(map_id, version)
        except (MapError, mp.PreviewError) as exc:
            raise AdminError(409, f"{map_id}:{version} 没法清噪点:{exc}") from exc
        return yname, meta, image, data

    def noise(self, map_id: str, version: str) -> dict[str, Any]:
        """这一版里「零星噪点」的候选(``map_denoise.find_specks``),给网页画出来让人挑。"""
        from d1max_site import map_denoise as dn
        map_id, version = self._names(map_id, version)
        _, meta, _, data = self._grid(map_id, version)
        specks = dn.find_specks(data, meta)
        return {"map_id": map_id, "version": version, "digest": dn.digest(data),
                "res": meta["resolution"], "max_m2": dn.SPECK_MAX_M2, "ring_m": dn.RING_M,
                "specks": [{k: s[k] for k in ("id", "x", "y", "px")} for s in specks]}

    def denoise(self, map_id: str, version: str, *, digest: Any, skip: Any,
                by: str) -> dict[str, Any]:
        """把这一版的噪点候选(``skip`` 里的编号除外)清掉,另存成新的一版:文件照抄、只换栅格;
        这一版上每只狗的原点照抄(同一个坐标系)。区域、待命点、拦截点走「新版本带过来、要人核」(R2)。
        ``digest`` 跟这一版现在的栅格对不上(找完之后图换了)→ 409。"""
        import shutil
        import tempfile
        from pathlib import Path

        from d1max_site import map_denoise as dn
        from d1max_site.maps import MapError
        map_id, version = self._names(map_id, version)
        if not isinstance(skip, list) or not all(isinstance(i, int) and not isinstance(i, bool)
                                                for i in skip):
            raise AdminError(400, "skip 要是编号的列表")
        _, meta, image, data = self._grid(map_id, version)
        if digest != dn.digest(data):
            raise AdminError(409, "这一版的栅格在你看过之后换了:重新打开清噪点再看一遍",
                             reason="stale")
        specks = [s for s in dn.find_specks(data, meta) if s["id"] not in set(skip)]
        if not specks:
            raise AdminError(409, "没有要清的(都去掉了,或者没找到)", reason="nothing")
        new = self._next_version(map_id, version)
        src = self.cat.root / map_id / version
        tmp = Path(tempfile.mkdtemp(prefix="denoise-", dir=self.cat.root))
        try:
            for f in self.ref(map_id, version).files:
                shutil.copy2(src / f.name, tmp / f.name)
            (tmp / image).write_bytes(dn.clean(data, meta, specks))
            px = sum(s["px"] for s in specks)
            try:
                self.cat.import_dir(tmp, map_id=map_id, version=new, source="denoise",
                                    note=f"从 {version} 清了 {len(specks)} 处零星噪点({px} 格)")
            except MapError as exc:
                raise AdminError(409, str(exc)) from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        from d1max_site.standby import copy_homes
        homes = copy_homes(self.db, map_id, version, new)
        return {"map_id": map_id, "from": version, "version": new, "cleaned": len(specks),
                "cells": px, "homes_copied": homes}

    def edit(self, map_id: str, version: str, *, digest: Any, ops: Any, note: Any,
             by: str) -> dict[str, Any]:
        """地图编辑第一版(W44 第四期,``map_edit``):橡皮擦、画障碍,另存成新的一版(跟清噪点一样:
        文件照抄、只换栅格,原点照抄,区域等走「新版本带过来、要人核」)。``digest`` 对不上 → 409。"""
        import shutil
        import tempfile
        from pathlib import Path

        from d1max_site import map_denoise as dn
        from d1max_site import map_edit as me
        from d1max_site.maps import MapError
        map_id, version = self._names(map_id, version)
        try:
            clean = me.check_ops(ops)
        except me.EditError as exc:
            raise AdminError(400, str(exc)) from exc
        _, meta, image, data = self._grid(map_id, version)
        if digest != dn.digest(data):
            raise AdminError(409, "这一版的地图在你打开以后换了:重新打开再编辑", reason="stale")
        new_pgm, stats = me.apply_ops(data, meta, clean)
        new = self._next_version(map_id, version)
        src = self.cat.root / map_id / version
        tmp = Path(tempfile.mkdtemp(prefix="edit-", dir=self.cat.root))
        text = str(note or "")[:120]
        try:
            for f in self.ref(map_id, version).files:
                shutil.copy2(src / f.name, tmp / f.name)
            (tmp / image).write_bytes(new_pgm)
            try:
                self.cat.import_dir(tmp, map_id=map_id, version=new, source="edit",
                                    note=text or (f"从 {version} 编辑:擦了 "
                                                  f"{stats['erased_m2']} 平方米、画了 "
                                                  f"{stats['obstacle_m2']} 平方米障碍"))
            except MapError as exc:
                raise AdminError(409, str(exc)) from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        from d1max_site.standby import copy_homes
        homes = copy_homes(self.db, map_id, version, new)
        return {"map_id": map_id, "from": version, "version": new, **stats,
                "homes_copied": homes, "by": by}

    def _next_version(self, map_id: str, version: str) -> str:
        """新版本号:这个地图号下都是数字就取最大的加一;不然 ``<版本>-dn<n>``。"""
        have = [m["version"] for m in self.cat.list() if m["map_id"] == map_id]
        if have and all(v.isdigit() for v in have):
            return str(max(int(v) for v in have) + 1)
        n = 1
        while f"{version}-dn{n}" in have:
            n += 1
        return f"{version}-dn{n}"

    def delete(self, map_id: str, version: str) -> dict[str, Any]:
        """删掉一个旧版本(文件 + 登记 + 挂在它上面的区域、待命点、拦截点、原点)。**拒绝**:有狗在用、
        这个地图号下最新的一版、有狗的换图正在路上要换到它、是哪只狗的「退回目标」(W41:
        2026-10-26 现场误删了 v2,狗要退回去就没处退了)。"""
        from d1max_site.maps import MapError
        map_id, version = self._names(map_id, version)
        rows = [m for m in self.cat.list() if m["map_id"] == map_id]
        if not any(m["version"] == version for m in rows):
            raise AdminError(404, f"没有这张图:{map_id}:{version}")
        users = self._in_use().get((map_id, version), [])
        if users:
            who = "、".join(users)
            raise AdminError(409, f"{map_id}:{version} 还有机器人在用({who}):"
                                  "先让它们换到别的版本再删",
                             reason="in_use", robots=users)
        if rows[0]["version"] == version:
            raise AdminError(409, f"{map_id}:{version} 是 {map_id} 的最新一版,不能删(删旧的)",
                             reason="latest")
        if (map_id, version) in self._pending_targets():
            raise AdminError(409, f"有机器人正在换到 {map_id}:{version},"
                                  "等换完再说", reason="pending")
        back = [rid for rid in self.robot_ids()
                if (t := self.rollback_target(rid)) is not None
                and (t["map_id"], t["version"]) == (map_id, version)]
        if back:
            who = "、".join(back)
            raise AdminError(409, f"{map_id}:{version} 是 {who} 的退回目标"
                                  "(机器人上留着、出事能一键退回去):先让机器人换到别的版本、"
                                  "或者退回目标换了再删",
                             reason="rollback_target", robots=back)
        gone = {"nav_zones": 0, "nav_zone_proposals": 0, "standby_points": 0, "intercepts": 0,
                "homes": 0}
        try:
            size = self.cat.delete(map_id, version)
        except MapError as exc:
            raise AdminError(404, str(exc)) from exc
        with self.db.tx() as c:
            for table, col in (("nav_zones", "map_version"), ("nav_zone_proposals", "map_version"),
                               ("standby_points", "map_version"), ("intercepts", "map_version"),
                               ("homes", "map_version")):
                gone[table] = c.execute(f"DELETE FROM {table} WHERE map_id=? AND {col}=?",
                                        (map_id, version)).rowcount
        return {"deleted": f"{map_id}:{version}", "freed_bytes": size, "removed_rows": gone}

    # ------------------------------------------------------------ 对比

    def diff(self, map_id: str, a: str, b: str):
        """两版的对比(``map_diff.diff_grids``)。缓存最近 :data:`DIFF_CACHE` 份(两版登记后不变)。"""
        from d1max_site import map_diff
        from d1max_site import map_preview as mp
        from d1max_site.maps import MapError
        map_id, a = self._names(map_id, a)
        _, b = self._names(map_id, b)
        if a == b:
            raise AdminError(400, "要选两个不同的版本")
        key = (map_id, a, b)
        hit = self._diffs.get(key)
        if hit is not None:
            self._diffs[key] = self._diffs.pop(key)
            return hit
        cat = self.cat
        try:
            with cat._render_lock:                      # 渲染一次只做一张(吃 CPU 和内存)
                _, meta_a, image_a, data_a, _, _ = cat._raster(map_id, a)
                _, meta_b, image_b, data_b, _, _ = cat._raster(map_id, b)
                try:
                    got = map_diff.diff_grids(mp.grid_ranks(data_a, meta_a), meta_a,
                                              mp.grid_ranks(data_b, meta_b), meta_b)
                except mp.PreviewError as exc:
                    raise AdminError(500, f"对比不了:{exc}") from exc
        except mp.NoRaster as exc:
            raise AdminError(404, str(exc)) from exc
        except MapError as exc:
            raise AdminError(404, str(exc)) from exc
        except mp.PreviewError as exc:
            raise AdminError(500, f"对比不了:{exc}") from exc
        except map_diff.DiffError as exc:
            raise AdminError(422, str(exc)) from exc
        self._diffs[key] = got
        while len(self._diffs) > DIFF_CACHE:
            self._diffs.pop(next(iter(self._diffs)))
        # 两版 frames.json 一样是个好兆头(同一个雷达装法、同一个起点约定);不一样或没有:不下结论
        got.stats["frames_json_same"] = self._frames_same(map_id, a, b)
        return got

    def _frames_same(self, map_id: str, a: str, b: str) -> bool | None:
        def sha(v: str) -> str | None:
            try:
                for f in self.cat.get(map_id, v).files:
                    if f.name == "frames.json":
                        return f.sha256
            except Exception:  # noqa: BLE001
                return None
            return None
        x, y = sha(a), sha(b)
        return None if x is None or y is None else x == y


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")
