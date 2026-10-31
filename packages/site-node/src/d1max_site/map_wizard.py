"""建图向导(W54,用户 2026-10-31):只买设备的客户自己把第一张图建出来。

- **结论**(:func:`quality`):按这一版的 ``build.json``(回环、修正量、路程)给「能用 / 有问题」和原因;
  导入的老图没有建图记录是「说不清」。阈值先用最朴素、能讲给人听的几条,真机多建几张再调。
- **版本号**(:func:`next_version`):客户不碰地图号、版本。地图号缺省 ``main``,版本按日期加序号
  (``20261031-1``,站点时区)。
- **向导状态**(:class:`MapWizard`):一个站点同时只有一份,存在 ``site_settings`` 的 ``map_wizard`` 键;
  换手机、关了 App 都能接着做。建图结果(建好 / 没建成)不另存,看的时候从地图目录、事件表现算。

步骤:准备 → 怎么开 → 走一圈 → 建图 → 禁行区 → 原点 → 完成。禁行区排在原点前面:没确认禁行区的图
下发不了(W10),标原点又要机器人先载上这一版。
"""

from __future__ import annotations

import json
import math
import re
import secrets
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

KEY = "map_wizard"
STEPS = ("prepare", "drive", "walk", "build", "zones", "origin", "done")
#: 调好参数的场地类型;其余的「即将支持」(室外、玻璃多要真场地录包调参,另开一单)。
SITE_TYPES = ("indoor",)
COMING_SOON = ("outdoor", "glass")
DRIVES = ("remote", "phone")
DEFAULT_MAP = "main"
MAX_LABEL = 40

#: 一个站点一份向导:查、分版本、存要一起做(接口每个请求新建一个 MapWizard,锁放在模块上)。
_LOCK = threading.Lock()

#: 结论的阈值(设计 D6)。
MAX_SHIFT_M = 0.3
MIN_PATH_M = 15.0


class WizardError(ValueError):
    def __init__(self, status: int, text: str, reason: str = "") -> None:
        super().__init__(text)
        self.status = status
        self.reason = reason


# ------------------------------------------------------------ 结论


def _num(v: Any) -> float | None:
    """有限、不为负的数(米、个);别的(超大整数、inf、nan、负数、布尔、字符串)当没有。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) and f >= 0 else None


def _path_len(path: Any) -> float | None:
    if not isinstance(path, list) or len(path) < 2:
        return None
    total = 0.0
    prev = None
    for p in path:
        if not (isinstance(p, list) and len(p) >= 2 and all(
                isinstance(c, (int, float)) and not isinstance(c, bool) for c in p[:2])):
            return None
        if prev is not None:
            try:
                total += math.hypot(float(p[0]) - float(prev[0]), float(p[1]) - float(prev[1]))
            except OverflowError:
                return None
        prev = p
    return total if math.isfinite(total) else None


def quality(build: Any, coverage_path: Any) -> dict[str, Any]:
    """``build`` 是 ``build.json`` 的内容(没有就 ``None``),``coverage_path`` 是 ``coverage.json`` 的
    ``path``(没回环、``build.json`` 里没路程时拿它算走了多远)。回 ``{verdict, reasons, closures,
    max_shift_m, path_m}``;``reasons`` 是代号(``no_loop`` 没回到起点闭合、``big_shift`` 修正太大、
    ``short_path`` 走得太少),文字在 App 里。"""
    if not isinstance(build, dict):
        return {"verdict": "unknown", "reasons": [], "closures": None, "max_shift_m": None,
                "path_m": None}
    lc = build.get("loop_closure") if isinstance(build.get("loop_closure"), dict) else {}
    closures = lc.get("closures") if isinstance(lc.get("closures"), int) \
        and not isinstance(lc.get("closures"), bool) else None
    shift = _num(lc.get("max_shift_m"))
    path = _num(lc.get("path_m"))
    if path is None:
        path = _path_len(coverage_path)
    reasons = []
    if shift is not None and shift >= MAX_SHIFT_M:
        reasons.append("big_shift")              # 闭合了但挪得多,或挪得太多被丢掉
    elif not lc.get("used") or not closures:
        reasons.append("no_loop")
    if path is not None and path < MIN_PATH_M:
        reasons.append("short_path")
    return {"verdict": "problem" if reasons else "usable", "reasons": reasons,
            "closures": closures, "max_shift_m": shift,
            "path_m": None if path is None else round(path, 1)}


def read_quality(cat: Any, map_id: str, version: str) -> dict[str, Any]:
    """地图目录里这一版的结论;读不了的文件当没有(说不清)。没有这一版由 ``cat.get`` 抛。"""
    ref = cat.get(map_id, version)
    names = {f.name for f in ref.files}
    d = cat.root / map_id / version

    def load(name: str) -> Any:
        if name not in names:
            return None
        try:
            p = d / name
            if p.stat().st_size > 4 * 2**20:
                return None
            return json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            return None
    cov = load("coverage.json")
    try:
        return quality(load("build.json"), cov.get("path") if isinstance(cov, dict) else None)
    except Exception:  # noqa: BLE001 —— 一版的记录坏了不能拖垮总览、向导:说不清
        return quality(None, None)


# ------------------------------------------------------------ 版本号

_SEQ = re.compile(r"^(\d{8})-(\d{1,4})$")


def next_version(existing: set[str], day: str) -> str:
    """``day`` 是 ``YYYYMMDD``;回这一天还没用过的下一个序号。"""
    used = [int(m.group(2)) for v in existing if (m := _SEQ.match(v)) and m.group(1) == day]
    return f"{day}-{max(used, default=0) + 1}"


def _site_day(db: Any, now_ms: int) -> str:
    from d1max_site.sitetime import site_tz
    return datetime.fromtimestamp(now_ms / 1000, site_tz(db)).strftime("%Y%m%d")


# ------------------------------------------------------------ 向导状态


class MapWizard:
    """``settings`` 是 :class:`d1max_site.site_settings.SiteSettings`,``cat`` 是地图目录,``db``
    读事件表(这一版没建成的原因)。``known(robot_id)``:站点认不认识这台机器人。"""

    def __init__(self, settings: Any, cat: Any, db: Any, *, now_ms: Callable[[], int],
                 known: Callable[[str], bool], day: Callable[[], str] | None = None,
                 zones: Any = None) -> None:
        self.settings = settings
        self.zones = zones
        self.cat = cat
        self.db = db
        self.now_ms = now_ms
        self.known = known
        self.day = day or (lambda: _site_day(self.db, self.now_ms()))

    # ---- 读

    def _raw(self) -> dict[str, Any] | None:
        v = self.settings.get(KEY)
        return v if isinstance(v, dict) and v.get("step") in STEPS else None

    def view(self) -> dict[str, Any] | None:
        st = self._raw()
        if st is None:
            return None
        return st | {"build": self._build(st)}

    def _built(self, map_id: str, version: str) -> bool:
        return any(m["map_id"] == map_id and m["version"] == version for m in self.cat.list())

    def _build(self, st: dict[str, Any]) -> dict[str, Any]:
        """这一版建好了没有:``ready``(带结论)、``failed``(带原因)、``pending``(在录、在建、在传)。"""
        map_id, version = st["map_id"], st["version"]
        if self._built(map_id, version):
            return {"state": "ready", "quality": read_quality(self.cat, map_id, version)}
        rows = self.db.query("SELECT data FROM events WHERE robot_id=? AND kind='map_build_failed' "
                             "AND received_at>=? ORDER BY received_at DESC LIMIT 50",
                             (st["robot_id"], int(st.get("started_ms", 0))))
        for r in rows:
            try:
                d = json.loads(r["data"])
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("map_id") == map_id and d.get("version") == version:
                return {"state": "failed", "reason": str(d.get("reason", ""))[:300]}
        return {"state": "pending"}

    # ---- 改

    def _taken(self, map_id: str, tried: list[str]) -> set[str]:
        return {m["version"] for m in self.cat.list() if m["map_id"] == map_id} | set(tried)

    def _alloc(self, map_id: str, tried: list[str]) -> str:
        taken = self._taken(map_id, tried)
        while True:
            v = next_version(taken, self.day())
            if not self.cat.build_in_flight(map_id, v):
                return v
            taken.add(v)

    def _save(self, st: dict[str, Any]) -> dict[str, Any]:
        st["updated_ms"] = self.now_ms()
        self.settings.put(KEY, st)
        return self.view() or st

    def apply(self, d: Any, *, by: str) -> dict[str, Any]:
        """``{action: start|step|rewalk|finish|abandon, wizard_id, …}``;回改完的状态(放弃回 {})。
        开始以后的每一步都要带这份向导的 ``wizard_id``:被别人换掉的旧页面改不了新的。"""
        if not isinstance(d, dict):
            raise WizardError(400, "要 JSON 对象")
        with _LOCK:
            return self._apply(d, by)

    def _apply(self, d: dict[str, Any], by: str) -> dict[str, Any]:
        action = d.get("action")
        if action == "start":
            return self._start(d, by)
        st = self._raw()
        if st is None:
            raise WizardError(409, "现在没有在做的建图向导", "no_wizard")
        if d.get("wizard_id") != st.get("id"):
            raise WizardError(409, f"这份建图向导已经换成 {st.get('by', '?')} 开的了,"
                                   "重新进一次", "stale_wizard")
        if action == "abandon":
            self.settings.put(KEY, None)
            return {}
        if action == "rewalk":
            # 重走一遍:旧的那一版(建好了的)留着,换一个新版本号
            st["tried"] = [*st.get("tried", []), st["version"]][-20:]
            st["version"] = self._alloc(st["map_id"], st["tried"])
            st["step"] = "walk"
            st["started_ms"] = self.now_ms()
            return self._save(st)
        if action == "finish":
            m, v, r = st["map_id"], st["version"], st["robot_id"]
            if not self._built(m, v):
                raise WizardError(409, "这一版还没建好", "not_built")
            if self.zones is not None and not self.zones.confirmed_current(m, v):
                raise WizardError(409, f"{m}:{v} 的禁行区还没确认", "zones_unconfirmed")
            if not self.db.query("SELECT 1 FROM homes WHERE robot_id=? AND map_id=? AND "
                                 "map_version=?", (r, m, v)):
                raise WizardError(409, f"{r} 在 {m}:{v} 上还没标原点", "no_home")
            self.settings.put(KEY, None)
            return st | {"step": "done"}
        if action != "step":
            raise WizardError(400, "action 只认 start、step、rewalk、finish、abandon")
        step = d.get("step")
        if step not in STEPS or step == "done":
            raise WizardError(400, f"step 只认 {'、'.join(STEPS[:-1])}")
        if "drive" in d:
            if d["drive"] not in DRIVES:
                raise WizardError(400, f"drive 只认 {'、'.join(DRIVES)}")
            st["drive"] = d["drive"]
        if STEPS.index(step) >= STEPS.index("zones"):
            if not self._built(st["map_id"], st["version"]):
                raise WizardError(409, f"{st['map_id']}:{st['version']} 还没建好", "not_built")
            if st["step"] in ("walk", "build") and st.get("label"):
                # 用这一张:显示名记成这一版的备注(地图管理里看得到)
                self.cat.set_note(st["map_id"], st["version"], st["label"])
        st["step"] = step
        return self._save(st)

    def _start(self, d: dict[str, Any], by: str) -> dict[str, Any]:
        cur = self._raw()
        if cur is not None and d.get("replace") is not True:
            raise WizardError(409, f"{cur.get('by', '?')} 在建图(第 {STEPS.index(cur['step']) + 1}"
                                   " 步);要换掉就带上 replace", "wizard_active")
        site_type = d.get("site_type")
        if site_type in COMING_SOON:
            raise WizardError(400, "这种场地的建图参数还没调好,现在请联系交付工程师", "coming_soon")
        if site_type not in SITE_TYPES:
            raise WizardError(400, f"site_type 只认 {'、'.join(SITE_TYPES + COMING_SOON)}")
        drive = d.get("drive")
        if drive not in DRIVES:
            raise WizardError(400, f"drive 只认 {'、'.join(DRIVES)}")
        robot = d.get("robot_id")
        if not isinstance(robot, str) or not self.known(robot):
            raise WizardError(400, "没有这台机器人")
        label = d.get("label", "")
        if not isinstance(label, str) or len(label.strip()) > MAX_LABEL:
            raise WizardError(400, f"显示名最多 {MAX_LABEL} 个字")
        map_id = d.get("map_id") or DEFAULT_MAP
        if not isinstance(map_id, str) or d.get("map_id") and not any(
                m["map_id"] == map_id for m in self.cat.list()):
            raise WizardError(400, "没有这张图(新图不用给地图号)")
        now = self.now_ms()
        st = {"id": secrets.token_hex(8), "step": "prepare", "robot_id": robot,
              "site_type": site_type, "drive": drive,
              "map_id": map_id, "version": self._alloc(map_id, []), "tried": [],
              "label": label.strip(), "by": by, "started_ms": now}
        return self._save(st)
