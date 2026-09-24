"""站点自建 CA(W00c 设计决定三 A:调用系统 ``openssl`` 命令行,不引 Python 依赖)。

- 狗证书:CN = ``robot_id``(Mosquitto ``use_identity_as_username`` 拿它当用户名,ACL 按它限
  主题),``extendedKeyUsage = clientAuth``。证书包 = ``ca.crt`` + ``robot.crt`` + ``robot.key``
  + ``registration.json``(指纹 = 证书 DER 的 sha256),拷到狗的 ``/etc/d1max/``。
- 站点服务证书:CN = ``site:<site_id>``(保留名,狗的 robot_id 不许取它),带 SAN,既当 broker
  的服务端证书,也当站点自己连 broker 的客户端证书。
- 吊销:``openssl ca -revoke`` + 重新生成 CRL;broker 的 ``crlfile`` 指着它。

私钥一律 0600(openssl 子进程在 umask 077 下跑,文件生出来就是 0600,没有先宽后紧的窗口)。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from d1max_contract.errors import ContractError
from d1max_contract.registration import Registration
from d1max_contract.topics import Topics

DAY_MS = 86_400_000

#: 站点这边对 robot_id 比契约更严:它要进证书 CN、openssl 的 index.txt(制表符分隔)、
#: broker 的用户名与 ACL 的 %u。只许 ASCII 字母数字与 . _ -。
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: 主机名或 IP(IPv6 带冒号)。逗号、换行会注入到 SAN 扩展文件里。
_SAFE_HOST = re.compile(r"^[A-Za-z0-9.:-]{1,253}$")
#: 站点证书总带上本机:``serve`` 默认连 ``mqtts://127.0.0.1``,主机名校验要过。
LOCAL_NAMES = ("127.0.0.1", "localhost")


class CAError(RuntimeError):
    """CA 操作失败(openssl 报错、状态不对、名字不合规矩)。"""


def check_robot_id(site_id: str, robot_id: str) -> None:
    """站点侧的 robot_id 规矩:白名单字符 + 契约的主题规矩 + 不许是站点保留名。"""
    if not isinstance(robot_id, str) or not SAFE_ID.match(robot_id):
        raise CAError(f"robot_id 只许 ASCII 字母、数字、. _ -,1–64 个字符: {robot_id!r}")
    try:
        Topics(site_id=site_id, robot_id=robot_id)
    except ContractError as exc:
        raise CAError(f"robot_id 不合规矩: {exc}") from exc
    if robot_id.startswith("site:"):
        raise CAError(f"robot_id {robot_id!r} 是站点的保留名")


def site_principal(site_id: str) -> str:
    """站点自己在 broker 上的用户名(证书 CN)。狗的 robot_id 不许等于它。"""
    return f"site:{site_id}"


@dataclass(frozen=True)
class RobotBundle:
    robot_id: str
    dir: Path
    fingerprint: str
    registration: Registration


_CNF = """\
[ ca ]
default_ca = site_ca

[ site_ca ]
dir              = {root}
database         = $dir/index.txt
new_certs_dir    = $dir/newcerts
certificate      = $dir/ca.crt
private_key      = $dir/ca.key
serial           = $dir/serial
crlnumber        = $dir/crlnumber
default_md       = sha256
default_crl_days = 3650
policy           = policy_cn
unique_subject   = no
copy_extensions  = none

[ policy_cn ]
commonName = supplied

[ req ]
distinguished_name = req_dn
prompt             = no

[ req_dn ]
CN = placeholder

[ v3_ca ]
basicConstraints     = critical,CA:TRUE
keyUsage             = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash

[ robot_ext ]
basicConstraints = critical,CA:FALSE
keyUsage         = critical,digitalSignature
extendedKeyUsage = clientAuth
"""


class SiteCA:
    def __init__(self, root: Path, *, openssl: str = "openssl") -> None:
        self.root = Path(root)
        self._openssl = openssl

    # ------------------------------------------------------------ 路径

    @property
    def ca_key(self) -> Path:
        return self.root / "ca.key"

    @property
    def ca_cert(self) -> Path:
        return self.root / "ca.crt"

    @property
    def crl(self) -> Path:
        return self.root / "crl.pem"

    @property
    def _cnf(self) -> Path:
        return self.root / "openssl.cnf"

    @property
    def site_id(self) -> str:
        try:
            return (self.root / "site_id").read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CAError(f"CA 还没初始化: {self.root}") from exc

    def robot_dir(self, robot_id: str) -> Path:
        return self.root / "issued" / robot_id

    # ------------------------------------------------------------ 子进程

    def _run(self, *args: str) -> str:
        try:
            got = subprocess.run([self._openssl, *args], capture_output=True, text=True,
                                 preexec_fn=lambda: os.umask(0o077), timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CAError(f"openssl 起不来: {exc}") from exc
        if got.returncode != 0:
            raise CAError(f"openssl {args[0]} 失败: {got.stderr.strip() or got.stdout.strip()}")
        return got.stdout

    def _genkey(self, path: Path) -> None:
        self._run("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                  "-out", str(path))
        os.chmod(path, 0o600)

    # ------------------------------------------------------------ 初始化

    def init(self, site_id: str, *, days: int = 3650) -> None:
        if not SAFE_ID.match(site_id or ""):
            raise CAError(f"site_id 只许 ASCII 字母、数字、. _ -: {site_id!r}")
        if self.ca_cert.exists() or self.ca_key.exists():
            raise CAError(f"{self.root} 已经有 CA 了,不覆盖(换 CA 等于让所有狗失联)")
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        (self.root / "newcerts").mkdir(exist_ok=True)
        (self.root / "issued").mkdir(exist_ok=True)
        (self.root / "index.txt").write_text("")
        (self.root / "serial").write_text("1000\n")
        (self.root / "crlnumber").write_text("1000\n")
        self._cnf.write_text(_CNF.format(root=self.root), encoding="utf-8")
        (self.root / "site_id").write_text(site_id + "\n", encoding="utf-8")
        self._genkey(self.ca_key)
        self._run("req", "-x509", "-new", "-config", str(self._cnf), "-key", str(self.ca_key),
                  "-days", str(days), "-subj", f"/CN=d1max site CA {site_id}",
                  "-extensions", "v3_ca", "-out", str(self.ca_cert))
        self._gencrl()

    def _gencrl(self) -> None:
        self._run("ca", "-batch", "-config", str(self._cnf), "-gencrl", "-out", str(self.crl))

    # ------------------------------------------------------------ 签发

    def _valid_subjects(self) -> set[str]:
        """index.txt 里还有效(V)的 CN。"""
        out = set()
        for line in (self.root / "index.txt").read_text().splitlines():
            parts = line.split("\t")
            if len(parts) >= 6 and parts[0] == "V":
                for rdn in parts[5].split("/"):
                    if rdn.startswith("CN="):
                        out.add(rdn[3:])
        return out

    def _sign(self, key: Path, cn: str, days: int, out: Path, *, extensions: str,
              extfile: Path | None = None) -> None:
        csr = out.with_suffix(".csr")
        self._run("req", "-new", "-config", str(self._cnf), "-key", str(key), "-subj",
                  f"/CN={cn}", "-out", str(csr))
        args = ["ca", "-batch", "-notext", "-config", str(self._cnf), "-days", str(days),
                "-extensions", extensions, "-in", str(csr), "-out", str(out)]
        if extfile is not None:
            args += ["-extfile", str(extfile)]
        try:
            self._run(*args)
        finally:
            csr.unlink(missing_ok=True)
        os.chmod(out, 0o644)

    def issue_robot(self, robot_id: str, *, days: int, now_ms: int) -> RobotBundle:
        site_id = self.site_id
        check_robot_id(site_id, robot_id)
        if robot_id in self._valid_subjects():
            raise CAError(f"{robot_id} 已有一张有效证书;先 revoke 再重签")
        d = self.robot_dir(robot_id)
        if d.exists():
            shutil.rmtree(d)                     # 上一张已吊销;旧证书留在 newcerts/ 备查
        d.mkdir(parents=True)
        os.chmod(d, 0o700)
        self._genkey(d / "robot.key")
        self._sign(d / "robot.key", robot_id, days, d / "robot.crt", extensions="robot_ext")
        shutil.copyfile(self.ca_cert, d / "ca.crt")
        fp = self.fingerprint(d / "robot.crt")
        reg = Registration(site_id=site_id, robot_id=robot_id, credential_fingerprint=fp,
                           issued_at=now_ms, expires_at=now_ms + days * DAY_MS)
        reg.save(d / "registration.json")
        return RobotBundle(robot_id=robot_id, dir=d, fingerprint=fp, registration=reg)

    def issue_server(self, hostnames: list[str], *, days: int = 825) -> tuple[Path, Path]:
        """站点服务证书:broker 的服务端证书,也是站点连 broker 的客户端证书。"""
        if not hostnames:
            raise CAError("站点证书至少要一个主机名或 IP")
        for h in hostnames:
            if not isinstance(h, str) or not _SAFE_HOST.match(h):
                raise CAError(f"主机名只许字母、数字、. : -: {h!r}")
        d = self.root / "server"
        d.mkdir(exist_ok=True)
        san = []
        for h in dict.fromkeys([*hostnames, *LOCAL_NAMES]):     # 去重、保序
            is_ip = all(p.isdigit() for p in h.split(".")) and h.count(".") == 3
            san.append(f"IP:{h}" if is_ip or ":" in h else f"DNS:{h}")
        ext = d / "server_ext.cnf"
        ext.write_text("[ server_ext ]\nbasicConstraints = critical,CA:FALSE\n"
                       "keyUsage = critical,digitalSignature,keyEncipherment\n"
                       "extendedKeyUsage = serverAuth,clientAuth\n"
                       f"subjectAltName = {','.join(san)}\n", encoding="utf-8")
        key, crt = d / "server.key", d / "server.crt"
        self._genkey(key)
        self._sign(key, site_principal(self.site_id), days, crt, extensions="server_ext",
                   extfile=ext)
        return crt, key

    # ------------------------------------------------------------ 吊销

    def revoke(self, robot_id: str) -> None:
        crt = self.robot_dir(robot_id) / "robot.crt"
        if not crt.is_file() or robot_id not in self._valid_subjects():
            raise CAError(f"{robot_id} 没有有效证书可吊销")
        self._run("ca", "-batch", "-config", str(self._cnf), "-revoke", str(crt))
        self._gencrl()

    # ------------------------------------------------------------ 工具

    def fingerprint(self, cert: Path) -> str:
        der = subprocess.run([self._openssl, "x509", "-in", str(cert), "-outform", "DER"],
                             capture_output=True, check=True, timeout=30).stdout
        return "sha256:" + hashlib.sha256(der).hexdigest()
