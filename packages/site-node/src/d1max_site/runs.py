"""运行记录台(W00c5d,决策 8):看一趟、判读、复核、导出,都在站点。

- **判读**:一趟跑完(清单里有汇总)、而且一分钟没有新文件进来(照片比清单晚到),站点就判一遍
  (没配模型密钥就不判,屏上是「待判读」)。判出异常的每张照片出一条 ``finding`` 告警。人可以随时
  让它重判。
- **复核**:人给每张照片一个结论(正常 / 异常 / 看不清)和一句话,写 ``review.json``,判读重跑不动它。
- **导出**:按时间段(和狗)把站点证据库里的原样文件打成 zip,附一份清单(每个文件的大小与 sha256);
  整个 zip 也算 sha256。导出留 7 天。
"""

from __future__ import annotations

import json
import logging
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

#: 一趟跑完之后多久没新文件进来,才判读(毫秒)。
QUIET_MS = 60_000
#: 导出留几天。
EXPORT_KEEP_DAYS = 7


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

    def judge(self, run_id: int, *, client: Any = None) -> list[dict[str, Any]]:
        r = self._run(run_id)
        d = self.store.dir_of(r)
        vlm = client if client is not None else self._client_factory()
        with self._judging:
            findings = judge_mod.judge_run(
                d, client=vlm, history_root=self.store.root / r["robot_id"],
                baselines_root=self.baselines / r["robot_id"])
        counts: dict[str, int] = {}
        for f in findings:
            counts[f.verdict] = counts.get(f.verdict, 0) + 1
        with self.db.tx() as c:
            c.execute("UPDATE runs SET judged_ms=?, verdicts=? WHERE id=?",
                      (self._now(), json.dumps(counts), run_id))
        if self.alerts is not None:
            for f in findings:
                if f.verdict == "abnormal":
                    self.alerts.raise_alert(
                        kind="finding", robot=r["robot_id"],
                        title=f"判读有异常:{f.waypoint}",
                        detail=f"{r['mission']} {r['stamp']} {f.photo}:{f.reason}"[:300])
        return [f.to_wire() for f in findings]

    def pending_judge(self) -> list[int]:
        """该判的:跑完了、一分钟没新文件、还没判过或判过之后又来了新文件。"""
        cutoff = self._now() - QUIET_MS
        rows = self.db.query("SELECT id FROM runs WHERE finished=1 AND photos>0 AND last_ms<? "
                             "AND (judged_ms IS NULL OR judged_ms<last_ms) ORDER BY id",
                             (cutoff,))
        return [r["id"] for r in rows]

    def step(self) -> int:
        """自动判读一拍(后台线程调)。没配模型密钥就不判。返回判了几趟。"""
        client = self._client_factory()
        if client is None:
            return 0
        n = 0
        for run_id in self.pending_judge():
            try:
                self.judge(run_id, client=client)
                n += 1
            except Exception:
                log.exception("自动判读第 %d 趟炸了", run_id)
        return n

    # ------------------------------------------------------------ 复核

    def review(self, run_id: int, photo: str, *, verdict: str, note: str) -> dict[str, Any]:
        r = self._run(run_id)
        d = self.store.dir_of(r)
        try:
            judge_mod.save_review(d, photo, verdict, str(note)[:500])
        except ValueError as exc:
            raise RunError(str(exc)) from exc
        n = len(judge_mod.read_reviews(d))
        with self.db.tx() as c:
            c.execute("UPDATE runs SET reviewed=? WHERE id=?", (n, run_id))
        return {"photo": photo, "verdict": verdict, "reviewed": n}

    # ------------------------------------------------------------ 导出

    def export(self, *, since_ms: int, until_ms: int, robot_id: str | None = None
               ) -> dict[str, Any]:
        if until_ms <= since_ms:
            raise RunError("导出的时间段不对")
        runs = self.store.runs(robot_id=robot_id, since_ms=since_ms, until_ms=until_ms,
                               limit=1000)
        if not runs:
            raise RunError("这个时间段里没有记录")
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self._prune_exports()
        name = f"export-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(self._now() / 1000))}-" \
               f"{uuid.uuid4().hex[:6]}"
        path = self.exports_dir / f"{name}.zip"
        tmp = path.with_name(path.name + ".tmp")
        listing = []
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as z:
            for r in runs:
                d = self.store.dir_of(r)
                for f in sorted(p for p in d.rglob("*") if p.is_file()
                                and not p.name.endswith(".tmp")):
                    arc = f"{r['robot_id']}/{r['mission']}/{r['stamp']}/" \
                          f"{f.relative_to(d).as_posix()}"
                    z.write(f, arc)
                    listing.append({"path": arc, "size": f.stat().st_size,
                                    "sha256": sha256_file(f)})
            z.writestr("清单.json", json.dumps({"since_ms": since_ms, "until_ms": until_ms,
                                                "robot_id": robot_id, "files": listing},
                                               ensure_ascii=False, indent=2))
        tmp.replace(path)
        meta = {"name": path.name, "size": path.stat().st_size, "sha256": sha256_file(path),
                "runs": len(runs), "files": len(listing), "created_ms": self._now()}
        path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False),
                                             encoding="utf-8")
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
            raise RunError(f"没有这份导出: {name}")
        return p

    def _prune_exports(self) -> None:
        cutoff = time.time() - EXPORT_KEEP_DAYS * 86400
        for p in self.exports_dir.glob("export-*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue

