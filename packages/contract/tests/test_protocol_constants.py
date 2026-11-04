"""协议常量钉死(改名外审 r1 Critical):签名域、派生密钥的 info、哈希前缀是**协议的一部分**,
改名也不能改 —— 改了,已经签出去的出厂证书、贴纸、解绑令,已经加密的口令、备份、证据,混版本在线时的
地图 / 区域摘要,全都对不上。下面的期望值是**改名前的代码**(8ac4e680)算出来的;这里红了 = 有人改了
协议常量,要么改回去,要么另起一个格式版本并兼容老的。"""
from __future__ import annotations

import hashlib

from nexwar_contract import binding, ed25519, evseal, persbridge, relsign
from nexwar_contract import device_id as di
from nexwar_contract.maps import MapFile, MapRef, map_digest
from nexwar_contract.zones import ZoneSet, zones_digest

SEED = b"x" * 32


def test_贴纸绑定码哈希_核对码_跟改名前一样():
    assert binding.code_hash("7KQ2M9XD4HTPR8WC") == \
        "b822ffd28d319b41108835a3f49ce2ba8d8f862ef1c22186a5466e44c3ba3c16"
    assert binding.check_digits("a" * 32) == "851471"


def test_改名前签的解绑令_照样认():
    old = ("D1MAXUNBIND1.eyJleHAiOjM2MDEwMDAsIm4iOiJlZmYyNDkyMzg3ZjE1N2ViNTlmY2M0YTQyNjAxOGN"
           "jYyIsInMiOiJEMU0tMDAwMSJ9.wSA69pyHuLaJI8_YSchHoUiGyRhFRCXvJJOKhIabGZOtsF4wtQnUml"
           "OwKkCUn085KFWoouBIMKczooFzeBULDQ")
    t = binding.verify_unbind(old, ed25519.public_key(SEED), serial="D1M-0001", now_ms=2000)
    assert t.serial == "D1M-0001"


def test_出厂证书签名_跟改名前一样():
    req = di.make_request(serial="D1M-0001", model="d1max",
                          device_pub=ed25519.public_key(b"y" * 32), hw_id="a" * 64)
    assert di.sign_device(SEED, req, now_ms=1000)["sig"] == \
        "nWtdVHkkKZDbJoY3fo74kSynTKP5tyBcbjzi4Dq-OSDGLmXxlwnBTbc9aXQ5H8ABoEOANVFhp0BSd-ap8f_-Dw"


def test_地图_区域摘要_跟改名前一样():
    assert map_digest(MapRef("map", "v1", (MapFile("map.pcd", 10, "a" * 64),))) == \
        "295bc19ee684583883e8dc359244f5d3da72750f29a5eaf5d0a43cfff9c935ba"
    assert zones_digest(ZoneSet("map", "v1", 1, ())) == \
        "40479d9be9ace07aec26f80b6493cd646a005996e49c3cf7791a2c752842ce0d"


def test_人员检测桥的认证_跟改名前一样():
    assert persbridge.hello_mac(SEED, "n1", "det") == \
        "9b4b13cb83c9bd355a639f5991eab36dd7774115359c236841413b4f8ef3032d"
    assert persbridge.agent_mac(SEED, "n1", "n2") == \
        "457a46c7f01e3c6ce0c2bc309a36b8ed4da6fca8be86b52025b173056025b1a9"
    assert persbridge.session_key(SEED, "n2", "n1").hex() == \
        "9c1e1703f510ad09bbaee14099234e455f7fbddff84d515c9a6160223187cfe3"


def test_发行签名的内容_跟改名前一样():
    m = {"name": "2026-11-01-abcdef", "version": "1", "content_sha256": "a" * 64,
         "requires_mission_schema": 1}
    assert hashlib.sha256(relsign.message(m)).hexdigest() == \
        "9b4cedf076719add8d6a89bd2f2081139dea1565ea28febe39982268d1917f01"


def test_改名前机器人加密的证据_站点照样解得开(tmp_path):
    enc = tmp_path / "p.d1e"
    enc.write_bytes(bytes.fromhex(
        "44314556310a44b4b7b07d12d6f1142718315c66309df7b1197580cb52745ef93ed3bcc3550953616c7465645f5f4a52a74010ec2f0809261167322cda919e4413041b77d25cbcf255bab21a3f1084fca08fbecfed1dde331ec3de"))
    evseal.open_file(enc, tmp_path / "p.jpg", SEED)
    assert (tmp_path / "p.jpg").read_bytes() == b"photo"


def test_改名前的芯片编号算法不变(tmp_path):
    from nexwar_patrol.bind import devid
    (tmp_path / "sn").write_bytes(b"1234ABCD9876\0")
    assert devid.read_hw_id([tmp_path / "sn"]) == \
        "adb63ffc8601b494ce2fbe416a447f5e3c3e6b6dbfc6744bf0b32a7644698050"


def test_改名前加密的口令和备份_照样解得开(tmp_path):
    from nexwar_site.sealbox import SealBox
    box = SealBox(SEED)
    old = "enc1:U2FsdGVkX1/vs+5K682O8Fn51y9VLruUY+P2MPTK/iQBOt2h5WDf9QByTCWISQF5nUbiiSPufyk="
    assert box.open(old) == "password"
    bk = tmp_path / "bk"
    bk.write_bytes(bytes.fromhex(
        "44315345414c310a53616c7465645f5fa05f203f6303f575883a5639f748b71004a5626aa7e0ddfbea3704"
        "f7d8208ac68b362704e35c8f4bedcd6fe112e3e3ad37f6fa"))
    box.open_file(bk, tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"site backup"


