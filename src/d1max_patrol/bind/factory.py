"""出厂烧写、出厂检验、重做贴纸(W42 §3)。在生产线上、机器人的 3588 上以 root 跑。

- ``burn``:生成绑定码、热点名和密码、配网证书;写 ``device.json``(只存绑定码的哈希)、解绑公钥、
  ``env`` 出厂值;写路由器热点。回贴纸(:class:`~d1max_contract.binding.Sticker`),由调用方出打印文件,
  **打完删掉**。
- ``check``:扫贴纸得到的那串跟机器人里存的核对。
- ``rekey``:贴纸坏了重做:绑定码、配网证书一起换,旧的作废(热点密码统一,不换)。**只能在没绑定的时候**
  (绑着的先在站点上解绑,站点坏了先用解绑令解)。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

from d1max_contract.binding import (
    DEFAULT_BIND_ADDR,
    HOTSPOT_PSK,
    BindingError,
    Sticker,
    check_serial,
    code_hash,
    decode_sticker,
    hotspot_ssid,
    new_code,
    pretty_code,
    short_fp,
)
from d1max_patrol.bind.paths import FACTORY_ENV, BindPaths, read_json, write_json
from d1max_patrol.bind.router import Router
from d1max_patrol.provision import set_env


class FactoryError(RuntimeError):
    pass


def _der(pem: bytes) -> bytes:
    text = pem.decode("ascii")
    body = text.split("-----BEGIN CERTIFICATE-----", 1)[1].split("-----END CERTIFICATE-----")[0]
    return base64.b64decode("".join(body.split()))


def _make_cert(paths: BindPaths, serial: str) -> str:
    """配网服务的自签证书(私钥只在机器人上)。回贴纸上的 32 位指纹。"""
    paths.bind_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.bind_dir, 0o700)
    tmp_key = paths.bind_dir / "bind.key.new"
    tmp_crt = paths.bind_dir / "bind.crt.new"
    old = os.umask(0o077)
    try:
        r = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                            "ec_paramgen_curve:prime256v1", "-nodes", "-days", "7300",
                            "-subj", f"/CN=d1max-bind-{serial}", "-keyout", str(tmp_key),
                            "-out", str(tmp_crt)], capture_output=True, text=True, timeout=60)
    finally:
        os.umask(old)
    if r.returncode != 0:
        raise FactoryError(f"生成配网证书失败:{r.stderr.strip()[:300]}")
    os.replace(tmp_key, paths.tls_key)
    os.replace(tmp_crt, paths.tls_cert)
    return short_fp(_der(paths.tls_cert.read_bytes()))


def _new_secrets(paths: BindPaths, serial: str, router: Router, *, ssid: str,
                 model: str) -> Sticker:
    """新绑定码、新配网证书。热点密码所有机器人统一(:data:`HOTSPOT_PSK`),不用每台生成。"""
    code = new_code()
    psk = HOTSPOT_PSK
    fp = _make_cert(paths, serial)
    write_json(paths.device, {"serial": serial, "code_hash": code_hash(code),
                              "hotspot_ssid": ssid, "fingerprint": fp, "model": model})
    router.set_hotspot(ssid, psk)
    return Sticker(serial, code, ssid, psk, fp)


def burn(paths: BindPaths, serial: str, *, router: Router, unbind_pub: Path,
         model: str = "D1 Max", force: bool = False) -> Sticker:
    try:
        check_serial(serial)
    except BindingError as exc:
        raise FactoryError(str(exc)) from exc
    dev = read_json(paths.device, None)
    if dev and not force:
        raise FactoryError(f"此机器人已烧写过了(序列号 {dev.get('serial')}):重做贴纸用 rekey")
    if not unbind_pub.is_file():
        raise FactoryError(f"没有公司解绑公钥 {unbind_pub}")
    paths.etc.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(unbind_pub, paths.unbind_pub)
    os.chmod(paths.unbind_pub, 0o644)
    for k, v in FACTORY_ENV.items():
        set_env(paths.env, k, v)
    env = paths.env.read_text(encoding="utf-8")
    if not any(line.startswith("D1MAX_BIND_HOST=") and line.strip() != "D1MAX_BIND_HOST="
               for line in env.splitlines()):
        # 3588 在路由器 LAN1 上的地址(出厂配置定死;贴纸不带地址时手机就找它)
        set_env(paths.env, "D1MAX_BIND_HOST", DEFAULT_BIND_ADDR.split(":")[0])
    write_json(paths.state, {"state": "unbound", "error": ""})
    return _new_secrets(paths, serial, router, ssid=hotspot_ssid(serial), model=model)


def rekey(paths: BindPaths, *, router: Router) -> Sticker:
    dev = read_json(paths.device, None)
    if not dev:
        raise FactoryError("此机器人尚未烧写过:先 burn")
    st = (read_json(paths.state, None) or {}).get("state", "unbound")
    if st != "unbound":
        raise FactoryError(f"此机器人现在是「{st}」:"
                            f"先在站点上解绑(站点已损坏先用解绑令解),再重做贴纸")
    return _new_secrets(paths, dev["serial"], router, ssid=dev["hotspot_ssid"],
                        model=dev.get("model", ""))


def check(paths: BindPaths, scanned: str) -> list[str]:
    """出厂检验:回对不上的地方(空 = 都对)。"""
    dev = read_json(paths.device, None)
    if not dev:
        return ["此机器人尚未烧写过"]
    try:
        s = decode_sticker(scanned)
    except BindingError as exc:
        return [f"贴纸读不出来:{exc}"]
    bad = []
    if s.serial != dev["serial"]:
        bad.append(f"序列号不匹配:贴纸 {s.serial},机器人 {dev['serial']}")
    if code_hash(s.code) != dev["code_hash"]:
        bad.append("绑定码不匹配(贴错了机器人,或者重做过贴纸)")
    if s.hotspot_ssid != dev["hotspot_ssid"]:
        bad.append(f"热点名不匹配:贴纸 {s.hotspot_ssid},机器人 {dev['hotspot_ssid']}")
    try:
        fp = short_fp(_der(paths.tls_cert.read_bytes()))
    except (OSError, IndexError, ValueError):
        fp = ""
    if s.fingerprint != fp or fp != dev.get("fingerprint"):
        bad.append("配网证书指纹不匹配")
    return bad


def label_files(sticker: Sticker, out: Path) -> list[Path]:
    """出打印文件(文字;**打完删掉**)。二维码图在生产线的电脑上出:
    ``python tools/sticker_label.py <这个文件>``(要 ``pip install '.[factory]'``)。
    机器人上不装画码的库。"""
    out.mkdir(parents=True, exist_ok=True)
    text = out / f"{sticker.serial}.txt"
    fd = os.open(text, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"序列号 {sticker.serial}\n绑定码 {pretty_code(sticker.code)}\n"
                f"二维码 {sticker.encode()}\n")
    return [text]
