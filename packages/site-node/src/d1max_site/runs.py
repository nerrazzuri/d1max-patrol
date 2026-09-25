"""运行记录台(W00c5d,决策 8):看一趟、判读、复核、导出,都在站点。

- **判读**:一趟跑完(清单里有汇总)、而且一分钟没有新文件进来(照片比清单晚到),站点就判一遍
  (没配模型密钥就不判,屏上是「待判读」)。判出异常的每张照片出一条 ``finding`` 告警。人可以随时
  让它重判。
- **复核**:人给每张照片一个结论(正常 / 异常 / 看不清)和一句话,写 ``review.json``,判读重跑不动它。
- **导出**:按时间段(和狗)把站点证据库里的原样文件打成 zip,附一份清单(每个文件的大小与 sha256);
  整个 zip 也算 sha256。导出留 7 天。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d1max_site import judge as judge_mod
from d1max_site.evidence import EvidenceStore, PathRefused, safe_join, sha256_file

log = logging.getLogger(__name__)

#: 最后一张照片收齐之后多久没再来新照片,才判读(毫秒)。
QUIET_MS = 60_000
#: 上一轮有没判成的(模型调用失败):隔多久再试、最多几轮。
RETRY_MS = 10 * 60_000
MAX_TRIES = 3
#: 导出留几天。
EXPORT_KEEP_DAYS = 7


def _stamp_of(ms: int) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ms / 1000))


class RunError(ValueError):
    """请求本身不对(没有这一趟、照片名不对、复核结论不对)。"""


class RunDesk:
    def __init__(self, store: EvidenceStore, *, home: Path, now_ms: Callable[[], int],
                 client_factory: Callable[[], Any] = judge_mod.default_client,
                 alerts: Any = None) -> None:
        self.store = store
        self.db = store.db
        self._now = now_ms
        self.baselines = Path(home) / "baselines"
        self.exports_dir = Path(home) / "exports"
        self._client_factory = client_factory
        self.alerts = alerts
        self._judging = threading.Lock()
        #: ``review.json`` 是读、改、写:两个人同时复核不同的照片不许丢一条(内部评审)。
        self._reviewing = threading.Lock()
        #: 正在后台判的(同一趟不叠着判)。
        self._judging_ids: set[int] = set()

    # ------------------------------------------------------------ 看

    def _run(self, run_id: int) -> dict[str, Any]:
        r = self.store.run(run_id)
        if r is None:
            raise RunError(f"没有这一趟: {run_id}")
        return r

    def detail(self, run_id: int) -> dict[str, Any]:
        r = self._run(run_id)
        d = self.store.dir_of(r)
        findings = {f.photo: f.to_wire() for f in judge_mod.read_findings(d)}
        reviews = judge_mod.read_reviews(d)
        photos = []
        for name in judge_mod._photos(d):
            waypoint, camera = judge_mod._split_photo_name(name)
            photos.append({"name": name, "waypoint": waypoint, "camera": camera,
                           "finding": findings.get(name), "review": reviews.get(name)})
        return {"run": r, "photos": photos}

    def photo(self, run_id: int, name: str) -> Path:
        r = self._run(run_id)
        try:
            p = self.store.photo_path(r, name)
        except PathRefused as exc:
            raise RunError(str(exc)) from exc
        if not p.is_file():
            raise RunError(f"这一趟里没有这张照片: {name}")
        return p

    # ------------------------------------------------------------ 判读

    def judge(self, run_id: int, *, client: Any = None, all_photos: bool = True
              ) -> list[dict[str, Any]]:
        """判读一趟。**只判站点上收齐了的照片**(还在传的那张不判、不当基线)。``all_photos`` 为假时
        只判还没判过或上次没判成(pending)的,别的结论原样留着(自动判读用)。"""
        r = self._run(run_id)
        d = self.store.dir_of(r)
        vlm = client if client is not None else self._client_factory()
        done = set(self.store.done_photos(run_id))
        started = self._now()                     # 判读开始的时刻:判到一半又到的照片要再判一轮
        with self._judging:
            if not all_photos:
                have = {f.photo: f.verdict for f in judge_mod.read_findings(d)}
                done = {n for n in done if have.get(n) in (None, "pending")}
            findings = judge_mod.judge_run(
                d, client=vlm, history_root=self.store.root / r["robot_id"],
                baselines_root=self.baselines / r["robot_id"], only=done)
        counts: dict[str, int] = {}
        for f in findings:
            counts[f.verdict] = counts.get(f.verdict, 0) + 1
        with self.db.tx() as c:
            c.execute("UPDATE runs SET judged_ms=?, verdicts=?, judge_tries=judge_tries+1 "
                      "WHERE id=?", (started, json.dumps(counts), run_id))
        if self.alerts is not None:
            for f in findings:
                if f.verdict == "abnormal" and f.photo in done:
                    self.alerts.raise_alert(
                        kind="finding", robot=r["robot_id"],
                        title=f"判读有异常:{f.waypoint}",
                        detail=f"{r['mission']} {r['stamp']} {f.photo}:{f.reason}"[:300])
        return [f.to_wire() for f in findings]

    def judge_async(self, run_id: int) -> dict[str, Any]:
        """人点「重判」:后台判(模型一张一张看,几十张要好几分钟,手机等不起)。同一趟正在判就不再叠一轮。"""
        self._run(run_id)
        with self._reviewing:
            if run_id in self._judging_ids:
                return {"run_id": run_id, "state": "judging"}
            self._judging_ids.add(run_id)

        def go() -> None:
            try:
                self.judge(run_id)
            except Exception:
                log.exception("重判第 %d 趟炸了", run_id)
            finally:
                with self._reviewing:
                    self._judging_ids.discard(run_id)
        threading.Thread(target=go, daemon=True, name="judge").start()
        return {"run_id": run_id, "state": "judging"}

    def pending_judge(self) -> list[int]:
        """该判的:跑完了、最后一张照片收齐之后一分钟没再来新照片,而且有收齐之后还没判过的照片;
        或者上一轮有没判成的(调用失败),隔 ``RETRY_MS`` 再试,最多 ``MAX_TRIES`` 轮。"""
        now = self._now()
        rows = self.db.query(
            "SELECT r.id, r.judged_ms, r.verdicts, r.judge_tries, MAX(p.done_ms) AS last_photo "
            "FROM runs r JOIN run_photos p ON p.run_id = r.id "
            "WHERE r.finished=1 GROUP BY r.id ORDER BY r.id")
        out = []
        for r in rows:
            if r["last_photo"] > now - QUIET_MS:
                continue                          # 照片还在来
            judged = r["judged_ms"]
            if judged is None or judged <= r["last_photo"]:
                out.append(r["id"])
                continue
            pending = json.loads(r["verdicts"] or "{}").get("pending", 0)
            if pending and r["judge_tries"] < MAX_TRIES and judged < now - RETRY_MS:
                out.append(r["id"])
        return out

    def step(self) -> int:
        """自动判读一拍(后台线程调)。没配模型密钥就不判。返回判了几趟。"""
        client = self._client_factory()
        if client is None:
            return 0
        n = 0
        for run_id in self.pending_judge():
            try:
                self.judge(run_id, client=client, all_photos=False)
                n += 1
            except Exception:
                log.exception("自动判读第 %d 趟炸了", run_id)
        return n

    # ------------------------------------------------------------ 复核

    def review(self, run_id: int, photo: str, *, verdict: str, note: str) -> dict[str, Any]:
        r = self._run(run_id)
        d = self.store.dir_of(r)
        with self._reviewing:
            try:
                judge_mod.save_review(d, photo, verdict, str(note)[:500])
            except ValueError as exc:
                raise RunError(str(exc)) from exc
            n = len(judge_mod.read_reviews(d))
        with self.db.tx() as c:
            c.execute("UPDATE runs SET reviewed=? WHERE id=?", (n, run_id))
        return {"photo": photo, "verdict": verdict, "reviewed": n}

    # ------------------------------------------------------------ 导出

    def export(self, *, since_ms: int, until_ms: int, robot_id: str | None = None,
               wait: bool = False) -> dict[str, Any]:
        """按**这一趟开跑的时刻**(目录名上的 UTC 时刻)取一个时间段的记录,打成 zip。在后台打
        (大的要好几分钟,手机等不起),先回一份 ``state: building`` 的说明;``wait`` 为真就打完再回。
        每个文件的 sha256 是**写进 zip 的那份字节**算的(不是回头再读一遍)。不设条数上限。"""
        if until_ms <= since_ms:
            raise RunError("导出的时间段不对")
        runs = self.store.runs_in(robot_id=robot_id, since_stamp=_stamp_of(since_ms),
                                  until_stamp=_stamp_of(until_ms))
        if not runs:
            raise RunError("这个时间段里没有记录")
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self._prune_exports()
        name = f"export-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(self._now() / 1000))}-" \
               f"{uuid.uuid4().hex[:6]}"
        meta = {"name": f"{name}.zip", "state": "building", "runs": len(runs),
                "since_ms": since_ms, "until_ms": until_ms, "robot_id": robot_id,
                "created_ms": self._now()}
        self._write_meta(meta)
        if wait:
            return self._build(meta, runs)
        threading.Thread(target=self._build, args=(meta, runs), daemon=True,
                         name="export").start()
        return meta

    def _write_meta(self, meta: dict[str, Any]) -> None:
        p = (self.exports_dir / meta["name"]).with_suffix(".json")
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    def _build(self, meta: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
        path = self.exports_dir / meta["name"]
        tmp = path.with_name(path.name + ".tmp")
        listing = []
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as z:
                for r in runs:
                    d = self.store.dir_of(r)
                    for f in sorted(p for p in d.rglob("*") if p.is_file()
                                    and not p.name.endswith(".tmp")):
                        arc = f"{r['robot_id']}/{r['mission']}/{r['stamp']}/" \
                              f"{f.relative_to(d).as_posix()}"
                        h, n = hashlib.sha256(), 0
                        with open(f, "rb") as src, z.open(arc, "w", force_zip64=True) as dst:
                            while chunk := src.read(1 << 20):
                                h.update(chunk)
                                n += len(chunk)
                                dst.write(chunk)
                        listing.append({"path": arc, "size": n, "sha256": h.hexdigest()})
                z.writestr("清单.json", json.dumps({
                    "since_ms": meta["since_ms"], "until_ms": meta["until_ms"],
                    "robot_id": meta["robot_id"], "runs": len(runs), "files": listing},
                    ensure_ascii=False, indent=2))
            os.replace(tmp, path)
            meta = meta | {"state": "ready", "size": path.stat().st_size,
                           "sha256": sha256_file(path), "files": len(listing)}
        except Exception as exc:
            log.exception("导出没成")
            tmp.unlink(missing_ok=True)
            meta = meta | {"state": "failed", "error": f"{type(exc).__name__}: {exc}"[:200]}
        self._write_meta(meta)
        return meta

    def exports(self) -> list[dict[str, Any]]:
        if not self.exports_dir.is_dir():
            return []
        out = []
        for m in sorted(self.exports_dir.glob("*.json"), reverse=True):
            try:
                out.append(json.loads(m.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def export_path(self, name: str) -> Path:
        try:
            p = safe_join(self.exports_dir, name)
        except PathRefused as exc:
            raise RunError(str(exc)) from exc
        if not name.endswith(".zip") or not p.is_file():
            raise RunError(f"没有这份导出(或还在打): {name}")
        return p

    def _prune_exports(self) -> None:
        cutoff = time.time() - EXPORT_KEEP_DAYS * 86400
        for p in self.exports_dir.glob("export-*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue

