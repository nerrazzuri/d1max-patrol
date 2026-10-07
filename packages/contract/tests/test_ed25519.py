"""Ed25519 纯 Python(W30 外审 1):RFC 8032 测试向量、跟 OpenSSL 3 互相认、发布包签名不碰 openssl
(狗上是 OpenSSL 1.1.1,它的 pkeyutl 不支持 Ed25519)。"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from d1max_contract import ed25519, relsign

# RFC 8032 第 7.1 节 TEST 1、2、3
VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e3970"
     "1cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613"
     "d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760"
     "984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("sk,pk,msg,sig", VECTORS)
def test_RFC8032测试向量(sk, pk, msg, sig):
    seed, m = bytes.fromhex(sk), bytes.fromhex(msg)
    assert ed25519.public_key(seed).hex() == pk
    assert ed25519.sign(seed, m).hex() == sig
    assert ed25519.verify(bytes.fromhex(pk), m, bytes.fromhex(sig))
    bad = bytearray(bytes.fromhex(sig))
    bad[5] ^= 1
    assert not ed25519.verify(bytes.fromhex(pk), m, bytes(bad))
    assert not ed25519.verify(bytes.fromhex(pk), m + b"x", bytes.fromhex(sig))


def test_不成形的公钥签名_不认():
    pk = bytes.fromhex(VECTORS[0][1])
    sig = bytes.fromhex(VECTORS[0][3])
    assert not ed25519.verify(pk[:31], b"", sig)
    assert not ed25519.verify(pk, b"", sig[:63])
    q = 2 ** 252 + 27742317777372353535851937790883648493
    s_val = int.from_bytes(sig[32:], "little")
    malleable = sig[:32] + (s_val + q).to_bytes(32, "little")   # 数学上照样成立,要按 s ≥ q 拒
    assert not ed25519.verify(pk, b"", malleable), "可塑的签名(s+q)不认"
    with pytest.raises(ValueError):
        ed25519.pub_from_pem(b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n")


def _openssl3():
    exe = shutil.which("openssl")
    if exe is None:
        return None
    got = subprocess.run([exe, "pkeyutl", "-help"], capture_output=True, text=True)
    return exe if "-rawin" in got.stdout + got.stderr else None


@pytest.mark.skipif(_openssl3() is None, reason="本机没有支持 Ed25519 pkeyutl 的 OpenSSL 3")
def test_跟OpenSSL3互相认(tmp_path):
    exe = _openssl3()
    k, pub, msg, sig = (tmp_path / n for n in ("k.pem", "p.pem", "m", "s"))
    msg.write_bytes(b"d1max-release\nhello")
    subprocess.run([exe, "genpkey", "-algorithm", "ed25519", "-out", str(k)], check=True)
    subprocess.run([exe, "pkey", "-in", str(k), "-pubout", "-out", str(pub)], check=True)
    seed = ed25519.seed_from_pem(k.read_bytes())
    assert ed25519.public_key(seed) == ed25519.pub_from_pem(pub.read_bytes()), \
        "OpenSSL 的钥匙读得懂"
    subprocess.run([exe, "pkeyutl", "-sign", "-rawin", "-inkey", str(k), "-in", str(msg),
                    "-out", str(sig)], check=True)
    assert ed25519.verify(ed25519.pub_from_pem(pub.read_bytes()), msg.read_bytes(),
                          sig.read_bytes()), "OpenSSL 签的我们验得过"
    ours = tmp_path / "ours"
    ours.write_bytes(ed25519.sign(seed, msg.read_bytes()))
    got = subprocess.run([exe, "pkeyutl", "-verify", "-pubin", "-inkey", str(pub), "-rawin",
                          "-in", str(msg), "-sigfile", str(ours)], capture_output=True)
    assert got.returncode == 0, "我们签的 OpenSSL 验得过"
    k2, p2 = tmp_path / "k2.pem", tmp_path / "p2.pem"
    relsign.keygen(k2, p2)
    got = subprocess.run([exe, "pkey", "-in", str(k2), "-pubout"], capture_output=True)
    assert got.returncode == 0 and got.stdout == p2.read_bytes(), "我们生成的 PEM,OpenSSL 认"


def test_发布包签名_全程不碰openssl(tmp_path, monkeypatch):
    def 不许(*a, **k):
        raise AssertionError("狗上的 openssl 不支持 Ed25519:不许调")
    monkeypatch.setattr(subprocess, "run", 不许)
    k, pub = tmp_path / "k", tmp_path / "k.pub"
    relsign.keygen(k, pub)
    m = {"name": "r", "version": "1", "content_sha256": "ab" * 32, "requires_mission_schema": 1}
    m["signature"] = relsign.sign(m, k)
    relsign.verify(m, pub)
    with pytest.raises(relsign.SignError):
        relsign.verify(dict(m, version="2"), pub)
