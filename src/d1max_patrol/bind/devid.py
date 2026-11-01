"""机器人这头的设备身份(W63,``docs/W63-设备身份-设计.md`` 一):在上装板上以 root 跑。

- 设备钥匙:``/etc/d1max/device/device.key``(Ed25519,0600,只有 root 读得到),**永不离开这块板**;
  已有就不换(换了出厂证书就作废了)。
- 芯片编号 ``hw_id``:芯片唯一序号(设备树 ``serial-number``,或 ``/proc/cpuinfo`` 的 ``Serial``)的
  SHA-256。读不到、全是 0 就不做(不拿 machine-id 这种能随手改的凑数)。
- 出厂申请 ``device-request.json`` → 台式机上用出厂钥匙签 → 出厂证书 ``device-cert.json`` 装回来
  (装之前核:序列号、芯片编号、设备公钥都是这块板的,而且是认的出厂钥匙签的)。
- 答题:站点出题,用设备私钥签,连同出厂证书、现场读的芯片编号一起交。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from d1max_contract import device_id as di
from d1max_contract import ed25519
from d1max_patrol.bind.paths import BindPaths, read_json, write_json

#: 芯片序号从哪儿读(按先后):RK3588 / Orin 的设备树,再是 cpuinfo 的 Serial。
HW_SOURCES: tuple[Path, ...] = (Path("/proc/device-tree/serial-number"),
                                Path("/sys/firmware/devicetree/base/serial-number"),
                                Path("/proc/cpuinfo"))
_SERIAL_LINE = re.compile(r"^Serial\s*:\s*(\S+)\s*$", re.M)


class DevIdError(RuntimeError):
    pass


def device_dir(p: BindPaths) -> Path:
    return p.etc / "device"


def key_path(p: BindPaths) -> Path:
    return device_dir(p) / "device.key"


def request_path(p: BindPaths) -> Path:
    return device_dir(p) / "device-request.json"


def cert_path(p: BindPaths) -> Path:
    return device_dir(p) / "device-cert.json"


def read_hw_id(sources: Iterable[Path] = HW_SOURCES) -> str:
    """芯片编号:第一个读得到、不是全 0 的序号的 SHA-256(64 位十六进制)。"""
    for src in sources:
        try:
            raw = Path(src).read_bytes()
        except OSError:
            continue
        text = raw.replace(b"\x00", b"").decode("ascii", "replace")
        m = _SERIAL_LINE.search(text)
        value = (m.group(1) if m else text).strip()
        if m is None and "\n" in value:                # 别的多行文件(cpuinfo 没 Serial 那一行)
            continue
        if value and value.strip("0") and len(value) <= 128:
            return hashlib.sha256(b"d1max-hw|" + value.encode("ascii", "replace")).hexdigest()
    raise DevIdError("读不到芯片编号(设备树 serial-number、cpuinfo 的 Serial 都没有):"
                     "这块板不能出厂")


def _model(dev: dict[str, Any]) -> str:
    m = re.sub(r"[^A-Za-z0-9._-]+", "-", str(dev.get("model") or "")).strip("-")[:32]
    return m or "d1max"


def _seed(p: BindPaths) -> bytes | None:
    try:
        return ed25519.seed_from_pem(key_path(p).read_bytes())
    except (OSError, ValueError):
        return None


def _ensure_key(p: BindPaths) -> bytes:
    seed = _seed(p)
    if seed is not None:
        return seed
    d = device_dir(p)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    seed = ed25519.new_seed()
    tmp = key_path(p).with_suffix(".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(ed25519.private_pem(seed))
    os.replace(tmp, key_path(p))
    return seed


def identity_path(p: BindPaths) -> Path:
    """没走过烧写的老机器(开发样机)补设备身份时,序列号、型号记在这里(不写 device.json)。"""
    return device_dir(p) / "identity.json"


def _identity(p: BindPaths) -> dict[str, Any]:
    """这块板的序列号、型号:烧写过的看 device.json,只补过身份的看 identity.json。"""
    return read_json(p.device, None) or read_json(identity_path(p), None) or {}


def init_identity(p: BindPaths, *, serial: str, model: str = "",
                  hw_sources: Iterable[Path] = HW_SOURCES) -> dict[str, Any]:
    """W63:只补设备身份(设备钥匙 + 出厂申请),不烧写绑定码、热点 —— 给没走过烧写的老机器
    (开发样机)。``serial`` 要是它在站点上的编号。烧写过的,序列号要跟烧写的一样。"""
    dev = read_json(p.device, None)
    if dev and dev.get("serial") != serial:
        raise DevIdError(f"这块板烧写时的序列号是 {dev.get('serial')},不是 {serial}")
    old = read_json(identity_path(p), None)
    if old and old.get("serial") != serial:
        raise DevIdError(f"这块板已经补过设备身份,序列号是 {old.get('serial')},不是 {serial}")
    if not dev:
        try:
            di.make_request(serial=serial, model=model or "d1max", device_pub=b"\0" * 32,
                            hw_id="0" * 64)
        except di.DeviceIdError as exc:
            raise DevIdError(str(exc)) from exc
        read_hw_id(hw_sources)                 # 读不到芯片编号就什么都不写
        device_dir(p).mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json(identity_path(p), {"serial": serial, "model": model or "d1max"}, mode=0o644)
    return make_request(p, hw_sources=hw_sources)


def make_request(p: BindPaths, *, hw_sources: Iterable[Path] = HW_SOURCES) -> dict[str, Any]:
    """出厂申请(``factory burn`` 里调;序列号、型号从 device.json 来,只补身份的从 identity.json)。
    设备钥匙没有就生成。"""
    dev = _identity(p)
    if not dev:
        raise DevIdError("此机器人尚未烧写过:先 burn(老机器只补设备身份:factory device-init)")
    hw = read_hw_id(hw_sources)
    seed = _ensure_key(p)
    try:
        req = di.make_request(serial=dev["serial"], model=_model(dev),
                              device_pub=ed25519.public_key(seed), hw_id=hw)
    except di.DeviceIdError as exc:
        raise DevIdError(str(exc)) from exc
    write_json(request_path(p), req, mode=0o644)
    return req


def install_cert(p: BindPaths, cert: Any, *, factory_pubs: Iterable[bytes],
                 hw_sources: Iterable[Path] = HW_SOURCES) -> None:
    """装出厂证书:序列号、芯片编号、设备公钥都是这块板的,而且是认的出厂钥匙签的。"""
    if not isinstance(cert, dict):
        raise DevIdError("出厂证书已损坏")
    _check_cert(p, cert, factory_pubs=factory_pubs, hw_sources=hw_sources)
    write_json(cert_path(p), cert, mode=0o644)


def problems(p: BindPaths, *, factory_pubs: Iterable[bytes],
             hw_sources: Iterable[Path] = HW_SOURCES) -> list[str]:
    """出厂检验(设备身份那几项):回对不上的地方(空 = 都对)。"""
    cert = read_json(cert_path(p), None)
    if not cert:
        return ["没有出厂证书(先 factory sign-device 签、install-cert 装)"]
    try:
        _check_cert(p, cert, factory_pubs=factory_pubs, hw_sources=hw_sources)
    except DevIdError as exc:
        return [str(exc)]
    return []


def _check_cert(p: BindPaths, cert: dict[str, Any], *, factory_pubs: Iterable[bytes],
                hw_sources: Iterable[Path]) -> None:
    """出厂证书是不是这块板的、是不是认的出厂钥匙签的(装之前、出厂检验都用)。"""
    dev = _identity(p)
    seed = _seed(p)
    if cert.get("serial") != dev.get("serial"):
        raise DevIdError(f"出厂证书的序列号 {cert.get('serial')} 不是这块板的({dev.get('serial')})")
    if cert.get("hw_id") != read_hw_id(hw_sources):
        raise DevIdError("出厂证书的芯片编号不是这块板的(板子换过,或者整盘拷过来的)")
    if seed is None or cert.get("device_pub") != di._b64(ed25519.public_key(seed)):
        raise DevIdError("出厂证书的设备钥匙不是这块板上的那把")
    try:
        di.verify_cert(cert, list(factory_pubs))
    except di.DeviceIdError as exc:
        raise DevIdError(f"出厂证书核不过:{exc}") from exc


def answer(p: BindPaths, *, purpose: str, site_id: str, nonce: str,
           hw_sources: Iterable[Path] = HW_SOURCES) -> dict[str, Any] | None:
    """答站点出的题:``{cert, hw_id, sig}``。没有出厂证书或设备钥匙:``None``(站点会拒,不报错)。"""
    cert = read_json(cert_path(p), None)
    seed = _seed(p)
    if not cert or seed is None:
        return None
    try:
        hw = read_hw_id(hw_sources)
        msg = di.attest_message(purpose=purpose, site_id=site_id, nonce=nonce,
                                serial=str(cert.get("serial")), hw_id=hw)
    except (DevIdError, di.DeviceIdError):
        return None
    return {"cert": cert, "hw_id": hw, "nonce": nonce, "sig": di.sign_attest(seed, msg)}


def attester(p: BindPaths, hw_sources: Iterable[Path] | None = None) -> Any:
    """给连站点的那一头用:``attester(paths)(purpose, site_id, nonce)`` → 答题或 ``None``。"""
    src = list(hw_sources) if hw_sources is not None else list(HW_SOURCES)
    return lambda purpose, site_id, nonce: answer(p, purpose=purpose, site_id=site_id,
                                                  nonce=nonce, hw_sources=src)


def factory_pubs(p: BindPaths) -> list[bytes]:
    """这台认的出厂公钥:``/etc/d1max/factory-pub.pem`` + ``factory-pub.d/*.pem``(随发行装的)。"""
    files = [p.etc / "factory-pub.pem"]
    d = p.etc / "factory-pub.d"
    if d.is_dir():
        files += sorted(d.glob("*.pem"))
    out = []
    for f in files:
        try:
            out.append(ed25519.pub_from_pem(f.read_bytes()))
        except (OSError, ValueError):
            continue
    return out


def load_cert_file(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DevIdError(f"读不了出厂证书 {path}:{exc}") from exc
