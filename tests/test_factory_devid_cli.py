"""W63 出厂命令:烧写顺带出出厂申请、台式机上签(记出厂台账)、装回、出厂检验。"""

from __future__ import annotations

import argparse
import csv
import json

import pytest

from d1max_contract import ed25519
from d1max_patrol.bind import devid
from d1max_patrol.bind.cli import add_args, run
from d1max_patrol.bind.paths import BindPaths


def _run(argv):
    ap = argparse.ArgumentParser()
    add_args(ap.add_subparsers(dest="command", required=True))
    return run(ap.parse_args(argv))


@pytest.fixture
def 台(tmp_path):
    etc, data = tmp_path / "etc", tmp_path / "data"
    sn = tmp_path / "sn"
    sn.write_bytes(b"RK3588-0A1B2C\x00")
    unbind = tmp_path / "unbind-pub.pem"
    unbind.write_bytes(ed25519.public_pem(ed25519.public_key(ed25519.new_seed())))
    factory = tmp_path / "factory.key"
    seed = ed25519.new_seed()
    factory.write_bytes(ed25519.private_pem(seed))
    etc.mkdir()
    (etc / "factory-pub.pem").write_bytes(ed25519.public_pem(ed25519.public_key(seed)))
    common = ["--etc", str(etc), "--data-root", str(data)]
    return tmp_path, common, sn, unbind, factory


def test_W63_烧写出申请_签_装_检验(台, capsys):
    tmp, common, sn, unbind, factory = 台
    assert _run(["factory", "--no-service", "burn", *common, "--serial", "D1M-000123",
                 "--unbind-pub", str(unbind), "--out", str(tmp / "label"),
                 "--hw-source", str(sn)]) == 0
    paths = BindPaths(etc=tmp / "etc", data_root=tmp / "data")
    req = devid.request_path(paths)
    assert req.is_file() and "出厂申请" in capsys.readouterr().out
    ledger = tmp / "出厂台账.csv"
    assert _run(["factory", "sign-device", str(req), "--key", str(factory), "--ledger", str(ledger),
                 "--out-dir", str(tmp / "certs")]) == 0
    certs = list((tmp / "certs").glob("*.json"))
    assert len(certs) == 1
    rows = list(csv.DictReader(ledger.open(encoding="utf-8")))
    assert rows[0]["serial"] == "D1M-000123" and len(rows[0]["hw_id"]) == 64
    assert _run(["factory", "install-cert", str(certs[0]), *common, "--hw-source", str(sn)]) == 0
    sticker = next((tmp / "label").glob("*.txt")).read_text()
    scan = next(ln for ln in sticker.splitlines() if ln.startswith("二维码 ")).split(" ", 1)[1]
    capsys.readouterr()
    assert _run(["factory", "check", *common, "--scan", scan, "--hw-source", str(sn)]) == 0
    assert "出厂证书" in capsys.readouterr().out


def test_W63_换了板子装不上_检验报出来(台, tmp_path, capsys):
    tmp, common, sn, unbind, factory = 台
    _run(["factory", "--no-service", "burn", *common, "--serial", "D1M-000124",
          "--unbind-pub", str(unbind), "--out", str(tmp / "label"), "--hw-source", str(sn)])
    paths = BindPaths(etc=tmp / "etc", data_root=tmp / "data")
    _run(["factory", "sign-device", str(devid.request_path(paths)), "--key", str(factory),
          "--out-dir", str(tmp / "certs")])
    cert = next((tmp / "certs").glob("*.json"))
    other = tmp / "sn2"
    other.write_bytes(b"RK3588-FFFFFF\x00")
    capsys.readouterr()
    assert _run(["factory", "install-cert", str(cert), *common, "--hw-source", str(other)]) == 1
    assert "芯片编号" in capsys.readouterr().err


def test_W63_读不到芯片编号_不烧(台, capsys):
    tmp, common, _, unbind, _ = 台
    assert _run(["factory", "--no-service", "burn", *common, "--serial", "D1M-000125",
                 "--unbind-pub", str(unbind), "--out", str(tmp / "label"),
                 "--hw-source", str(tmp / "none")]) == 1
    assert "芯片编号" in capsys.readouterr().err
    assert not (tmp / "etc" / "device.json").exists(), "先读芯片编号,读不到什么都不写"


def test_W63_签名的key文件不对_不签(台, capsys):
    tmp, common, sn, unbind, _ = 台
    _run(["factory", "--no-service", "burn", *common, "--serial", "D1M-000126",
          "--unbind-pub", str(unbind), "--out", str(tmp / "label"), "--hw-source", str(sn)])
    bad = tmp / "bad.key"
    bad.write_text("not a key")
    paths = BindPaths(etc=tmp / "etc", data_root=tmp / "data")
    assert _run(["factory", "sign-device", str(devid.request_path(paths)), "--key", str(bad),
                 "--out-dir", str(tmp / "certs")]) == 1
    assert json.loads(devid.request_path(paths).read_text())["serial"] == "D1M-000126"


def test_外审W63_2_装证书可以直接给出厂公钥_机器人上没装发行包里那份也行(台, capsys):
    tmp, common, sn, unbind, factory = 台
    _run(["factory", "--no-service", "burn", *common, "--serial", "D1M-000127",
          "--unbind-pub", str(unbind), "--out", str(tmp / "label"), "--hw-source", str(sn)])
    paths = BindPaths(etc=tmp / "etc", data_root=tmp / "data")
    _run(["factory", "sign-device", str(devid.request_path(paths)), "--key", str(factory),
          "--out-dir", str(tmp / "certs")])
    pub = tmp / "factory-pub-given.pem"
    pub.write_bytes((tmp / "etc" / "factory-pub.pem").read_bytes())
    (tmp / "etc" / "factory-pub.pem").unlink()                 # 远程升级上来的,没装
    cert = next((tmp / "certs").glob("*.json"))
    assert _run(["factory", "install-cert", str(cert), *common, "--hw-source", str(sn)]) == 1
    assert _run(["factory", "install-cert", str(cert), *common, "--hw-source", str(sn),
                 "--factory-pub", str(pub)]) == 0
