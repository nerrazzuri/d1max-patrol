"""狗的开通码(商业化 A4):站点上一条命令出一个**一次性、24 小时有效**的开通码,狗上一条命令就领到
自己的证书包、填好站点地址 —— 不用再拿 U 盘拷五个文件、手改 ``/etc/d1max/env``。

- 开通码(:func:`issue_code`)是一行字:``D1MAX1.<base64url(JSON)>``,里面有站点接口地址、站点服务证书
  的指纹(狗连的时候按它**钉住**,冒充不了)、MQTT 地址、狗的编号、一个随机令牌(24 字节)。能贴、能发
  消息,以后手机上也能显示成二维码。
- 站点只记令牌的**哈希**(``enroll_claims``);同一只狗再出一个码,旧的作废。
- 领(:func:`claim`,``POST /api/enroll/claim``,**不用登录**,令牌就是凭证):对得上、没用过、没过期、狗
  没吊销,才把证书包给它;**给了就作废**(用一次)。对不上一律回同一句话,不说是哪一样不对。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

PREFIX = "D1MAX1."
#: 开通码多久有效(毫秒)。
TTL_MS = 24 * 3600_000
#: 证书包里给狗的文件:名字 → 狗上放哪儿(相对 ``/etc/d1max``)。
BUNDLE = {"ca.crt": "tls/ca.crt", "robot.crt": "tls/robot.crt", "robot.key": "tls/robot.key",
          "registration.json": "registration.json", "evidence-pub.key": "evidence-pub.key"}
#: 必须有的(证据公钥没配证据私钥的站点没有)。
REQUIRED = ("ca.crt", "robot.crt", "robot.key", "registration.json")


class ProvisionError(RuntimeError):
    pass


def _h(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()   # 乱填的(非 ASCII)也不炸


def encode(d: dict[str, Any]) -> str:
    raw = json.dumps(d, separators=(",", ":"), sort_keys=True).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(code: str) -> dict[str, Any]:
    code = code.strip()
    if not code.startswith(PREFIX):
        raise ProvisionError("不是开通码(要以 D1MAX1. 开头)")
    body = code[len(PREFIX):]
    try:
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError) as exc:
        raise ProvisionError("开通码坏了(抄错了?)") from exc
    for k in ("api", "fp", "mqtt", "id", "t"):
        if not isinstance(d.get(k), str) or not d[k]:
            raise ProvisionError(f"开通码里缺 {k}")
    return d


def issue_code(db: Any, cfg: dict[str, Any], home: Path, robot_id: str, *, fingerprint: str,
               now_ms: int, host: str | None = None, api_port: int = 8443,
               ttl_ms: int = TTL_MS) -> str:
    """给已经签发了证书包的 ``robot_id`` 出一个开通码(同一只狗以前的码作废)。"""
    bundle = Path(home) / "ca" / "issued" / robot_id
    missing = [n for n in REQUIRED if not (bundle / n).is_file()]
    if missing:
        raise ProvisionError(f"{robot_id} 没有证书包({'、'.join(missing)}):先 enroll")
    host = host or cfg["hostnames"][0]
    token = secrets.token_urlsafe(24)
    with db.tx() as c:
        c.execute("INSERT INTO enroll_claims(robot_id, token_hash, created_ms, expires_ms, "
                  "used_ms) VALUES (?,?,?,?,NULL) ON CONFLICT(robot_id) DO UPDATE SET "
                  "token_hash=excluded.token_hash, created_ms=excluded.created_ms, "
                  "expires_ms=excluded.expires_ms, used_ms=NULL",
                  (robot_id, _h(token), now_ms, now_ms + ttl_ms))
    return encode({"api": f"https://{host}:{api_port}",
                   "fp": fingerprint.removeprefix("sha256:"),
                   "mqtt": f"mqtts://{host}:{cfg.get('broker_port', 8883)}",
                   "id": robot_id, "t": token})


#: 对不上一律这一句(不说是没这只狗、令牌不对、过期了还是用过了)。
DENIED = "开通码不对、过期了或者已经用过了:在站点上重新出一个"


def claim(db: Any, home: Path, robot_id: str, token: str, *, now_ms: int,
          revoked: Callable[[str], bool] | None = None) -> dict[str, str]:
    """领证书包:回 ``{文件名: base64}``。对不上抛 :class:`ProvisionError`(:data:`DENIED`)。
    **给了就作废**:作废跟核对在同一个事务里,两只狗拿同一个码只有一只领得到。"""
    if not isinstance(robot_id, str) or not isinstance(token, str) or not token:
        raise ProvisionError(DENIED)
    with db.tx() as c:
        row = c.execute("SELECT token_hash, expires_ms, used_ms FROM enroll_claims "
                        "WHERE robot_id=?", (robot_id,)).fetchone()
        ok = (row is not None and row["used_ms"] is None and now_ms <= row["expires_ms"]
              and hmac.compare_digest(row["token_hash"], _h(token))
              and not (revoked is not None and revoked(robot_id)))
        if not ok:
            raise ProvisionError(DENIED)
        c.execute("UPDATE enroll_claims SET used_ms=? WHERE robot_id=?", (now_ms, robot_id))
    bundle = Path(home) / "ca" / "issued" / robot_id
    out = {}
    for name in BUNDLE:
        p = bundle / name
        if p.is_file():
            out[name] = base64.b64encode(p.read_bytes()).decode("ascii")
    if any(n not in out for n in REQUIRED):
        raise ProvisionError("站点上这只狗的证书包不全:重新 enroll")
    return out
