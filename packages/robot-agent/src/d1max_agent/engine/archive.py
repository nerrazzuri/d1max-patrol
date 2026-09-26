"""一次运行的归档目录。结构见设计 spec §6.5。

    runs/<mission>/<UTC-timestamp>/
      manifest.json    任务定义快照 + 环境指纹 + 结果汇总
      events.jsonl     状态迁移、导航请求响应、故障、决策依据
      telemetry.jsonl  抽样遥测
      state.json       当前状态(崩溃恢复用)
      photos/P1__front__20260901T101500Z.jpg
      report.md

**两条硬约束,别的都是细节:**

1. ``events.jsonl`` **写一行 flush 一行**。崩溃恢复全靠它,攒在缓冲区里的
   事件等于没写 —— 而"跑到一半断电了,停在哪个点"恰恰是最需要知道的那次。
2. ``state.json`` **整个换掉,不就地改**。它每次状态迁移都被重写,就地覆盖
   时断电正好断在中间就留下半个 JSON,读不出来等于没有恢复。
3. **写失败不往外抛**(W00c6a)。盘满、eMMC 出错后只读重挂,归档的每一处写都可能抛
   ``OSError``;以前一抛就漏进引擎,兜底路径自己又要写、又抛,引擎任务死掉、快照停在
   RUNNING。现在:记下第一条错误(:attr:`RunArchive.error`,代理据此报人);事件流、遥测流
   写坏一次就这一趟停写(不往一个写坏了的流里接着写半行);原子写失败不留临时文件、下一次照试。
   **照片例外**:存不下照样抛给引擎(先删掉半截文件)——引擎按「这个点失败」处理。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from d1max_agent.engine.mission import Mission

log = logging.getLogger(__name__)

#: 目录名与照片名共用的时间戳格式。UTC,因为跨时区看历史报告时本地时间没意义。
STAMP_FMT = "%Y%m%dT%H%M%SZ"


def safe_segment(name: str, *, max_bytes: int = 120) -> str:
    """任务名、点位名要当目录名、文件名用(W00c5d 内部评审):``/``、``\\``、控制字符换成 ``_``,
    开头的点换掉(不当隐藏文件、不当 ``..``),UTF-8 超长截断。空了给 ``_``。
    **只影响落盘的名字**:清单里的任务定义照旧是原样。"""
    out = "".join("_" if c in "/\\" or ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF else c
                  for c in name).strip()
    while out.startswith("."):
        out = "_" + out[1:]
    raw = out.encode("utf-8")
    if len(raw) > max_bytes:
        out = raw[:max_bytes].decode("utf-8", "ignore")
    return out or "_"


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(STAMP_FMT)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_write(path: Path, text: str) -> None:
    """写临时文件再 ``os.replace``。

    ``os.replace`` 在同一文件系统上是原子的:读的人要么看见旧的,要么看见
    新的,不会看见半个。临时文件必须落在同一个目录里,跨文件系统的 replace
    会退化成"复制 + 删除",原子性就没了。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class RunArchive:
    """一次运行的落盘出口。引擎只往这里写,不自己碰文件。"""

    def __init__(self, root: Path, mission: Mission, *,
                 started_at: datetime | None = None, suffix: str = "") -> None:
        self._mission = mission
        self._started = started_at or _now()
        self._path = self._make_dir(Path(root), safe_segment(mission.mission), self._started,
                                    suffix)
        (self._path / "photos").mkdir(exist_ok=True)
        self._events: IO[str] | None = None
        self._telemetry: IO[str] | None = None
        #: 第一条写失败(``"<文件>: <错误>"``);空串 = 一直写得进去。
        self.error = ""
        #: 一共失败了多少次写。
        self.write_failures = 0
        #: 写坏过、这一趟不再写的流(文件名)。
        self._dead: set[str] = set()
        #: 环境指纹记在内存里(W00c6a 内审 S4):开跑时清单没写进去、收尾时盘好了,指纹照样落上。
        self._fingerprint: dict[str, Any] | None = None

    @staticmethod
    def _make_dir(root: Path, mission: str, started: datetime, suffix: str = "") -> Path:
        """建目录。同秒撞车就往后加序号。

        现场手抖点两下"跑"是真会发生的,而两次运行的数据混在同一个目录里
        最难查 —— 事件流交织、照片互相覆盖。宁可多一个目录。

        ``suffix``(W00c5d,数字):发件箱模式下每一趟都带一个随机后缀 —— 传完就删,狗的钟往回拨
        之后同一个时刻可能再出现一次,不带后缀就会跟站点上早就收齐的那一趟撞名、把它覆盖掉。
        """
        base = root / mission
        base.mkdir(parents=True, exist_ok=True)
        stamp = _stamp(started)
        candidate = base / (f"{stamp}-{suffix}" if suffix else stamp)
        n = 2
        while candidate.exists():
            # 带后缀时序号并进后缀里(``…Z-0042172``):时刻后面只许一段数字,本地清理与站点都这么认。
            candidate = base / (f"{stamp}-{suffix}{n}" if suffix else f"{stamp}-{n}")
            n += 1
        candidate.mkdir()
        return candidate

    @property
    def path(self) -> Path:
        return self._path

    def photo_name(self, waypoint: str, camera: str, at: datetime | None = None) -> str:
        return f"{safe_segment(waypoint)}__{safe_segment(camera)}__{_stamp(at or _now())}.jpg"

    @property
    def mission(self) -> Mission:
        return self._mission

    # ------------------------------------------------------------- 事件与遥测

    def _line(self, handle_name: str, filename: str, payload: dict[str, Any]) -> None:
        handle = getattr(self, handle_name)
        if handle is None:
            handle = (self._path / filename).open("a", encoding="utf-8")
            setattr(self, handle_name, handle)
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        # 每行都 flush。归档不是热路径(一次运行几百行),而少写一行就可能
        # 正好是"为什么停下来"的那一行。
        handle.flush()

    def failure_summary(self) -> dict[str, Any] | None:
        """这一趟归档写失败的情况(W00c6a 内审 S4),进汇总:站点、发件箱据此知道这一趟是残缺的。
        一直写得进去就是 ``None``。"""
        if not self.write_failures:
            return None
        return {"error": self.error, "write_failures": self.write_failures,
                "stopped_streams": sorted(self._dead)}

    def _failed(self, what: str, exc: BaseException) -> None:
        self.write_failures += 1
        if not self.error:
            self.error = f"{what}: {exc}"
            log.warning("归档 %s 写不进去(%s),这一趟的记录不全", self._path, self.error)

    def _append(self, handle_name: str, filename: str, payload: dict[str, Any]) -> None:
        """一行进流;写失败记账、这条流这一趟停写,不往外抛。"""
        if filename in self._dead:
            return
        try:
            self._line(handle_name, filename, payload)
        except OSError as exc:
            self._dead.add(filename)
            self._failed(filename, exc)
            handle = getattr(self, handle_name)
            setattr(self, handle_name, None)
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()

    def append_event(self, kind: str, **fields: Any) -> None:
        self._append("_events", "events.jsonl",
                     {"ts_ms": int(_now().timestamp() * 1000), "kind": kind, **fields})

    def append_telemetry(self, **fields: Any) -> None:
        self._append("_telemetry", "telemetry.jsonl",
                     {"ts_ms": int(_now().timestamp() * 1000), **fields})

    def _atomic(self, path: Path, text: str) -> None:
        """原子写;失败记账、收掉临时文件,不往外抛(下一次照试)。"""
        try:
            _atomic_write(path, text)
        except OSError as exc:
            self._failed(path.name, exc)
            with contextlib.suppress(OSError):
                path.with_name(path.name + ".tmp").unlink(missing_ok=True)

    # ------------------------------------------------------------------- 状态

    def write_state(self, state: dict[str, Any]) -> None:
        self._atomic(self._path / "state.json", json.dumps(state, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------- 照片

    def photo_path(self, waypoint: str, camera: str,
                   at: datetime | None = None) -> Path:
        """照片路径。文件名里带点位名,所以同一点位跨日期的照片能天然聚合。"""
        return self._path / "photos" / self.photo_name(waypoint, camera, at)

    def save_photo(self, waypoint: str, camera: str, data: bytes,
                   at: datetime | None = None) -> Path:
        path = self.photo_path(waypoint, camera, at)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            self._failed(path.name, exc)
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)              # 写了半截的不留
            raise
        return path

    # ------------------------------------------------------------- manifest

    def write_manifest(self, fingerprint: dict[str, Any],
                       summary: dict[str, Any] | None = None) -> None:
        """任务定义**快照** + 环境指纹 + 结果汇总。指纹同时记在内存里(见 ``finish``)。

        快照而不是引用:别人改了 ``missions/*.yaml``,历史报告不该跟着变
        (设计 spec §6.1)。指纹里放 SDK 版本 / 协议版本 / 地图 ID,
        让报告能追溯到当时的软件版本。
        """
        self._fingerprint = fingerprint
        self._atomic(self._path / "manifest.json", json.dumps({
            "mission": self._mission.to_wire(),
            "started_at": _stamp(self._started),
            "fingerprint": fingerprint,
            "summary": summary or {},
        }, ensure_ascii=False, indent=2))

    def finish(self, summary: dict[str, Any]) -> None:
        """收尾:把汇总补进 manifest,保留已有的指纹。"""
        fingerprint = self._fingerprint
        if fingerprint is None:                      # 没经过 write_manifest 的(老目录):读盘上的
            path = self._path / "manifest.json"
            try:
                existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, ValueError) as exc:
                log.warning("清单读不出来(%s),指纹记空", exc)
                existing = {}
            fingerprint = existing.get("fingerprint", {}) if isinstance(existing, dict) else {}
        self.write_manifest(fingerprint, summary)

    def close(self) -> None:
        for name in ("_events", "_telemetry"):
            handle = getattr(self, name)
            if handle is not None:
                setattr(self, name, None)
                with contextlib.suppress(OSError):
                    handle.close()


# --------------------------------------------------------------------- 读取


def list_runs(root: Path) -> list[Path]:
    """列出所有运行目录,新的在前。

    按目录名倒序而不是按 mtime:目录名就是 UTC 时间戳,而 mtime 会被
    "事后补生成报告"这种操作搅乱。
    """
    root = Path(root)
    if not root.is_dir():
        return []
    runs = [d for mission_dir in sorted(root.iterdir()) if mission_dir.is_dir()
            for d in mission_dir.iterdir() if d.is_dir()]
    return sorted(runs, key=lambda p: (p.name, p.parent.name), reverse=True)


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    """读事件流。坏行不静默跳过 —— 静默丢帧会把问题藏到现场。"""
    path = Path(run_dir) / "events.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} 第 {n} 行不是合法 JSON: {exc}") from exc
    return out


def read_telemetry(run_dir: Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / "telemetry.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_state(run_dir: Path) -> dict[str, Any] | None:
    path = Path(run_dir) / "state.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def read_manifest(run_dir: Path) -> dict[str, Any] | None:
    path = Path(run_dir) / "manifest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def free_mb(path: Path) -> float:
    """目标目录所在盘的剩余空间,MB。起飞检查用。"""
    return shutil.disk_usage(path).free / (1024 * 1024)
