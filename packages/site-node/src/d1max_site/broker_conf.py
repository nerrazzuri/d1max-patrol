"""Mosquitto 配置与 ACL(W00c 设计决定二 A)。

- mTLS:``require_certificate true`` + ``use_identity_as_username true`` —— 证书 CN 就是用户名;
  狗的 CN 是 ``robot_id``,站点的 CN 是 ``site:<site_id>``。``allow_anonymous false``。
- 吊销:``crlfile`` 指着站点 CA 的 CRL;broker 只在启动时读它,吊销之后要重启 broker。
- ACL:狗侧按 ``pattern … %u`` 写,**与契约的 TopicAcl 出自同一张 ``PUBLISH_KINDS``**:
  一台狗只能发自己的六种主题、只能订自己的 ``cmd``;站点能给任何一台狗发 ``cmd``、能读所有
  狗的上行主题。有了 pattern,登记新狗不用改 ACL。

Mosquitto 的配置文件按空白切词,**路径里不许有空白**(``render`` 直接拒绝)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from d1max_contract.topics import PUBLISH_KINDS, Topics
from d1max_site.ca import site_principal


@dataclass(frozen=True)
class BrokerPaths:
    cafile: Path
    certfile: Path
    keyfile: Path
    crlfile: Path
    aclfile: Path
    persistence_dir: Path | None = None


def _p(path: Path, what: str) -> str:
    s = str(path)
    if any(ch.isspace() for ch in s):
        raise ValueError(f"Mosquitto 配置按空白切词,{what} 的路径里不能有空白: {s!r}")
    return s


def render_acl(site_id: str) -> str:
    Topics(site_id=site_id, robot_id="x")                       # site_id 过主题规矩
    base = f"site/{site_id}/robot"
    lines = ["# 由 d1max_site.broker_conf 生成,别手改。", "",
             "# 站点:给任何一台狗发 cmd,读所有狗的上行主题。",
             f"user {site_principal(site_id)}",
             f"topic write {base}/+/cmd"]
    lines += [f"topic read {base}/+/{k}" for k in PUBLISH_KINDS]
    lines += ["", "# 狗:用户名 = 证书 CN = robot_id;只发自己的上行主题,只订自己的 cmd。"]
    lines += [f"pattern write {base}/%u/{k}" for k in PUBLISH_KINDS]
    lines += [f"pattern read {base}/%u/cmd", ""]
    return "\n".join(lines)


def render_conf(*, port: int, paths: BrokerPaths, bind: str = "0.0.0.0",
                log_dest: str = "stderr") -> str:
    lines = [
        "# 由 d1max_site.broker_conf 生成,别手改。",
        "per_listener_settings false",
        "allow_anonymous false",
        # 一台被攻破的狗往自己的主题上发超大 retained 报文:上限 256 KB。
        "max_packet_size 262144",
        f"acl_file {_p(paths.aclfile, 'acl_file')}",
    ]
    if paths.persistence_dir is not None:
        # clean_session=False 的离线 QoS 1 报文要跨 broker 重启保留。
        lines += ["persistence true",
                  f"persistence_location {_p(paths.persistence_dir, 'persistence')}/"]
    else:
        lines += ["persistence false"]
    lines += [
        f"log_dest {log_dest}",
        "log_type error",
        "log_type warning",
        "log_type notice",
        "log_type information",
        "connection_messages true",
        f"listener {int(port)} {bind}",
        f"cafile {_p(paths.cafile, 'cafile')}",
        f"certfile {_p(paths.certfile, 'certfile')}",
        f"keyfile {_p(paths.keyfile, 'keyfile')}",
        f"crlfile {_p(paths.crlfile, 'crlfile')}",
        "require_certificate true",
        "use_identity_as_username true",
        # client_id 也用证书名:不然 A 拿 "B" 或 "site:<id>" 当 client_id 就能把别人踢下线、
        # 接管别人的持久会话(攒着的 cmd,包括 abort,就丢了)。
        "use_username_as_clientid true",
        "tls_version tlsv1.2",
        "",
    ]
    return "\n".join(lines)
