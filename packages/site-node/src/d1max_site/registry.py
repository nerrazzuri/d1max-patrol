"""机器人注册表:站点认不认这台狗(总设计 §3.6)。证书在 CA 里,这里只记指纹、有效期、吊销、
``control_epoch``。派遣器只给 ``active`` 的狗派单。"""

from __future__ import annotations

from dataclasses import dataclass

from d1max_site.ca import CAError, check_robot_id
from d1max_site.db import SiteDB


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RobotRecord:
    robot_id: str
    fingerprint: str
    issued_at: int
    expires_at: int
    revoked: bool
    control_epoch: int


class Registry:
    def __init__(self, db: SiteDB, *, site_id: str) -> None:
        self.db = db
        self.site_id = site_id

    def _check_id(self, robot_id: str) -> None:
        try:
            check_robot_id(self.site_id, robot_id)
        except CAError as exc:
            raise RegistryError(str(exc)) from exc

    def enroll(self, robot_id: str, *, fingerprint: str, issued_at: int, expires_at: int,
               now_ms: int | None = None) -> None:
        self._check_id(robot_id)
        with self.db.tx() as c:
            row = c.execute("SELECT revoked FROM robots WHERE robot_id=?", (robot_id,)).fetchone()
            if row is not None and not row["revoked"]:
                raise RegistryError(f"{robot_id} 已登记且未吊销;先 revoke")
            if row is None:
                c.execute("INSERT INTO robots(robot_id, fingerprint, issued_at, expires_at, "
                          "revoked, control_epoch, enrolled_at) VALUES (?,?,?,?,0,1,?)",
                          (robot_id, fingerprint, issued_at, expires_at, now_ms or issued_at))
            else:
                # 吊销后重登:换证书,control_epoch 保留(代理只认更大的代次,不许倒退)。
                c.execute("UPDATE robots SET fingerprint=?, issued_at=?, expires_at=?, "
                          "revoked=0, enrolled_at=? WHERE robot_id=?",
                          (fingerprint, issued_at, expires_at, now_ms or issued_at, robot_id))

    def revoke(self, robot_id: str) -> None:
        with self.db.tx() as c:
            cur = c.execute("UPDATE robots SET revoked=1 WHERE robot_id=?", (robot_id,))
            if cur.rowcount == 0:
                raise RegistryError(f"没有登记过 {robot_id}")

    @staticmethod
    def _rec(r) -> RobotRecord:
        return RobotRecord(robot_id=r["robot_id"], fingerprint=r["fingerprint"],
                           issued_at=r["issued_at"], expires_at=r["expires_at"],
                           revoked=bool(r["revoked"]), control_epoch=r["control_epoch"])

    def get(self, robot_id: str) -> RobotRecord | None:
        rows = self.db.query("SELECT * FROM robots WHERE robot_id=?", (robot_id,))
        return self._rec(rows[0]) if rows else None

    def list(self) -> list[RobotRecord]:
        return [self._rec(r) for r in self.db.query("SELECT * FROM robots ORDER BY robot_id")]

    def active(self, robot_id: str, *, now_ms: int) -> bool:
        r = self.get(robot_id)
        return r is not None and not r.revoked and r.issued_at <= now_ms < r.expires_at

    def control_epoch(self, robot_id: str) -> int:
        r = self.get(robot_id)
        if r is None:
            raise RegistryError(f"没有登记过 {robot_id}")
        return r.control_epoch
