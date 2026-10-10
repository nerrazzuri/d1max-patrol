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
    cert_fp = _cert_fp((bundle / "robot.crt").read_bytes())
    with db.tx() as c:
        # A 阶段外审 I1:码绑这一代证书。登记表里的、证书包里的必须是同一代,而且没吊销
        row = c.execute("SELECT fingerprint, revoked FROM robots WHERE robot_id=?",
                        (robot_id,)).fetchone()
        if row is None or row["revoked"] or row["fingerprint"] != cert_fp:
            raise ProvisionError(f"{robot_id} 没登记、已吊销,或者证书包不是登记的这一代:先 enroll")
        c.execute("INSERT INTO enroll_claims(robot_id, token_hash, created_ms, expires_ms, "
                  "used_ms, cert_fp) VALUES (?,?,?,?,NULL,?) ON CONFLICT(robot_id) DO UPDATE SET "
                  "token_hash=excluded.token_hash, created_ms=excluded.created_ms, "
                  "expires_ms=excluded.expires_ms, used_ms=NULL, cert_fp=excluded.cert_fp",
                  (robot_id, _h(token), now_ms, now_ms + ttl_ms, cert_fp))
    return encode({"api": f"https://{host}:{api_port}",
                   "fp": fingerprint.removeprefix("sha256:"),
                   "mqtt": f"mqtts://{host}:{cfg.get('broker_port', 8883)}",
                   "id": robot_id, "t": token})


#: 对不上一律这一句(不说是没这只狗、令牌不对、过期了还是用过了)。
DENIED = "开通码不对、过期了或者已经用过了:在站点上重新出一个"


def _cert_fp(pem: bytes) -> str:
    """PEM 证书的指纹(跟 ``SiteCA.fingerprint`` 一样:``sha256:`` + DER 的 SHA-256)。"""
    text = pem.decode("ascii", "replace")
    try:
        body = text.split("-----BEGIN CERTIFICATE-----", 1)[1]
        body = body.split("-----END CERTIFICATE-----", 1)[0]
        der = base64.b64decode("".join(body.split()), validate=True)
    except (IndexError, ValueError):
        return ""
    return "sha256:" + hashlib.sha256(der).hexdigest()


def _snapshot(bundle: Path) -> dict[str, bytes] | None:
    """读一份证书包。读的时候有人在换证(证书前后读到的不一样),回 None。"""
    try:
        before = (bundle / "robot.crt").read_bytes()
        out = {n: (bundle / n).read_bytes() for n in BUNDLE if (bundle / n).is_file()}
        after = (bundle / "robot.crt").read_bytes()
    except OSError:
        return None
    if before != after or out.get("robot.crt") != before:
        return None
    return out


def claim(db: Any, home: Path, robot_id: str, token: str, *, now_ms: int) -> dict[str, str]:
    """领证书包:回 ``{文件名: base64}``。对不上抛 :class:`ProvisionError`(:data:`DENIED`)。

    - **给了就作废**:作废跟核对在同一个事务里,两只狗拿同一个码只有一只领得到。
    - **只给出码的那一代**(A 阶段外审 I1):码记着出码时证书的指纹;登记表里这只狗现在的证书、
      这一回读到的证书包都得是那一代、没吊销,才给。吊销、重新登记还会直接删掉旧码。"""
    if not isinstance(robot_id, str) or not isinstance(token, str) or not token:
        raise ProvisionError(DENIED)
    snap = _snapshot(Path(home) / "ca" / "issued" / robot_id)
    got_fp = _cert_fp(snap["robot.crt"]) if snap and "robot.crt" in snap else ""
    with db.tx() as c:
        row = c.execute("SELECT e.token_hash, e.expires_ms, e.used_ms, e.cert_fp, r.fingerprint, "
                        "r.revoked FROM enroll_claims e LEFT JOIN robots r "
                        "ON r.robot_id = e.robot_id WHERE e.robot_id=?", (robot_id,)).fetchone()
        ok = (row is not None and row["used_ms"] is None and now_ms <= row["expires_ms"]
              and hmac.compare_digest(row["token_hash"], _h(token))
              and row["revoked"] == 0 and row["cert_fp"] != ""
              and row["fingerprint"] == row["cert_fp"] == got_fp)
        if not ok:
            raise ProvisionError(DENIED)
        c.execute("UPDATE enroll_claims SET used_ms=? WHERE robot_id=?", (now_ms, robot_id))
    if snap is None or any(n not in snap for n in REQUIRED):
        raise ProvisionError("站点上这只狗的证书包不全:重新 enroll")
    return {n: base64.b64encode(v).decode("ascii") for n, v in snap.items()}
