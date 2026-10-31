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
               now_ms: int | None = None, manual_only: bool = False) -> None:
        """``manual_only``:登记成「只许手动派」(W33:站点命令行登记新狗时默认这样,
        管理员确认后才接自动派遣)。"""
        self._check_id(robot_id)
        with self.db.tx() as c:
            if manual_only:
                c.execute("INSERT OR REPLACE INTO robot_service(robot_id, by, at_ms, note) "
                          "VALUES (?,?,?,?)", (robot_id, "enroll", now_ms or issued_at,
                                               "新登记:确认之前只许手动派"))
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
            c.execute("DELETE FROM cert_pending WHERE robot_id=?", (robot_id,))   # W55-2 I2

    def stage(self, robot_id: str, *, fingerprint: str, issued_at: int, expires_at: int,
              now_ms: int, old_fingerprint: str = "") -> bool:
        """W55 自动续签:新证书签好了、机器人还没换上 —— 记下来,旧证书照常认(外审 W55-1 第 6 条)。
        记着发起时登记表里的那张(``old_fingerprint``):转正时登记表还得是它。"""
        with self.db.tx() as c:
            # 外审 W55-3 I2:登记表还得是发起时那张(期间解绑、重登过就不挂)
            if old_fingerprint and c.execute(
                    "SELECT 1 FROM robots WHERE robot_id=? AND revoked=0 AND fingerprint=?",
                    (robot_id, old_fingerprint)).fetchone() is None:
                return False
            c.execute("INSERT INTO cert_pending(robot_id, fingerprint, old_fingerprint, issued_at, "
                      "expires_at, created_ms) VALUES (?,?,?,?,?,?) "
                      "ON CONFLICT(robot_id) DO UPDATE SET fingerprint=excluded.fingerprint, "
                      "old_fingerprint=excluded.old_fingerprint, issued_at=excluded.issued_at, "
                      "expires_at=excluded.expires_at, created_ms=excluded.created_ms",
                      (robot_id, fingerprint, old_fingerprint, issued_at, expires_at, now_ms))
        return True

    def pending(self, robot_id: str) -> dict | None:
        rows = self.db.query("SELECT * FROM cert_pending WHERE robot_id=?", (robot_id,))
        return dict(rows[0]) if rows else None

    def drop_pending(self, robot_id: str, fingerprint: str | None = None) -> None:
        with self.db.tx() as c:
            if fingerprint is None:
                c.execute("DELETE FROM cert_pending WHERE robot_id=?", (robot_id,))
            else:
                c.execute("DELETE FROM cert_pending WHERE robot_id=? AND fingerprint=?",
                          (robot_id, fingerprint))

    def promote(self, robot_id: str, fingerprint: str) -> bool:
        """机器人确认换上了(或拿这张新证书连上来了):登记表换成它。不是挂着的那张 → False。"""
        with self.db.tx() as c:
            rows = c.execute("SELECT * FROM cert_pending WHERE robot_id=? AND fingerprint=?",
                             (robot_id, fingerprint)).fetchall()
            if not rows:
                return False
            r = rows[0]
            # 外审 W55-2 I2:登记表还得是发起续签时那张(期间解绑、重新登记过就不转)
            n = c.execute("UPDATE robots SET fingerprint=?, issued_at=?, expires_at=? "
                          "WHERE robot_id=? AND revoked=0 AND fingerprint=?",
                          (fingerprint, r["issued_at"], r["expires_at"], robot_id,
                           r["old_fingerprint"])).rowcount
            c.execute("DELETE FROM cert_pending WHERE robot_id=?", (robot_id,))
            return n == 1

    def renew(self, robot_id: str, *, fingerprint: str, issued_at: int, expires_at: int) -> None:
        """工单 9:续签 —— 换指纹和有效期;代次、只许手动派这些都不动。没登记、吊销了的不许续。"""
        with self.db.tx() as c:
            row = c.execute("SELECT revoked FROM robots WHERE robot_id=?", (robot_id,)).fetchone()
            if row is None or row["revoked"]:
                raise RegistryError(f"{robot_id} 未登记或已吊销:要 enroll,不是续签")
            c.execute("UPDATE robots SET fingerprint=?, issued_at=?, expires_at=? WHERE robot_id=?",
                      (fingerprint, issued_at, expires_at, robot_id))

    def manual_only(self, robot_id: str) -> dict | None:
        """只许手动派(W33):``{by, at_ms, note}``;接自动派遣 → ``None``。"""
        rows = self.db.query("SELECT by, at_ms, note FROM robot_service WHERE robot_id=?",
                             (robot_id,))
        return dict(rows[0]) if rows else None

    def set_manual_only(self, robot_id: str, on: bool, *, by: str, now_ms: int,
                        note: str = "") -> bool:
        """设成只许手动派 / 接自动派遣。返回之前是不是只许手动。"""
        with self.db.tx() as c:
            was = c.execute("SELECT 1 FROM robot_service WHERE robot_id=?",
                            (robot_id,)).fetchone() is not None
            if on:
                c.execute("INSERT OR REPLACE INTO robot_service(robot_id, by, at_ms, note) "
                          "VALUES (?,?,?,?)", (robot_id, by, now_ms, note[:200]))
            else:
                c.execute("DELETE FROM robot_service WHERE robot_id=?", (robot_id,))
        return was

    def names(self) -> dict[str, str]:
        """管理员起的名字(W44 第二期):``{robot_id: 名字}``,没起名的不在里面。"""
        return {r["robot_id"]: r["display_name"] for r in self.db.query(
            "SELECT robot_id, display_name FROM robots WHERE display_name<>''")}

    def enrolled_at(self, robot_id: str) -> int | None:
        """第一次登记进这个站点的时刻(W53:值守页默认跟第一台加入的)。"""
        rows = self.db.query("SELECT enrolled_at FROM robots WHERE robot_id=?", (robot_id,))
        return int(rows[0]["enrolled_at"]) if rows else None

    def set_name(self, robot_id: str, name: object) -> None:
        """起名:1–32 个字,不许控制字符;空串 = 不起名(用编号)。"""
        if not isinstance(name, str):
            raise RegistryError("名字要是文字")
        name = name.strip()
        if len(name) > 32 or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in name):
            raise RegistryError("名字最多 32 个字,不许有控制字符")
        with self.db.tx() as c:
            if c.execute("UPDATE robots SET display_name=? WHERE robot_id=?",
                         (name, robot_id)).rowcount == 0:
                raise RegistryError(f"没有登记过 {robot_id}")

    def revoke(self, robot_id: str) -> None:
        with self.db.tx() as c:
            cur = c.execute("UPDATE robots SET revoked=1 WHERE robot_id=?", (robot_id,))
            if cur.rowcount == 0:
                raise RegistryError(f"没有登记过 {robot_id}")
            # A 阶段外审 I1:吊销了,没领的开通码一并作废(重新登记要先吊销,所以也不会复活;
            # 就算漏删,claim 还核证书代次)
            c.execute("DELETE FROM enroll_claims WHERE robot_id=?", (robot_id,))
            c.execute("DELETE FROM cert_pending WHERE robot_id=?", (robot_id,))   # W55-2 I2

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
