"""狗上证据加密(W30b,决策 51):X25519 对 RFC 7748 的测试向量;封、拆来回;
私钥不对、被改过、截断都拆不开。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from d1max_contract import evseal as E


def test_X25519_对得上RFC7748的向量():
    k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
    u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
    assert E.x25519(k, u).hex() == \
        "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"
    # §6.1 的 Diffie-Hellman
    a = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    b = bytes.fromhex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
    assert E.public_key(a).hex() == \
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    assert E.x25519(a, E.public_key(b)) == E.x25519(b, E.public_key(a))
    assert E.x25519(a, E.public_key(b)).hex() == \
        "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"


def test_封了拆得开_明文不落盘_两次封的密文不一样(tmp_path):
    priv, pub = E.keypair()
    data = os.urandom(300_000)
    s = E.Sealer(pub)
    a, b = tmp_path / "a.jpg.d1e", tmp_path / "b.jpg.d1e"
    s.seal_bytes(data, a)
    s.seal_bytes(data, b)
    assert a.read_bytes() != b.read_bytes(), "每个文件一对临时密钥"
    assert data[:64] not in a.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg.d1e", "b.jpg.d1e"], \
        "没留 .part、.tmp(封到一半的是点开头的,发件箱扫盘不排它)"
    E.open_file(a, tmp_path / "a.jpg", priv)
    assert (tmp_path / "a.jpg").read_bytes() == data


def test_封文件_大文件流式(tmp_path):
    priv, pub = E.keypair()
    src = tmp_path / "seg.mp4"
    src.write_bytes(os.urandom(3 * (1 << 20) + 17))
    E.Sealer(pub).seal_file(src, tmp_path / "seg.mp4.d1e")
    E.open_file(tmp_path / "seg.mp4.d1e", tmp_path / "out.mp4", priv)
    assert (tmp_path / "out.mp4").read_bytes() == src.read_bytes()


def test_别的私钥_改一个字节_截断_都拆不开_一个字节都不写(tmp_path):
    priv, pub = E.keypair()
    other, _ = E.keypair()
    p = tmp_path / "x.jpg.d1e"
    E.Sealer(pub).seal_bytes(b"secret face" * 1000, p)
    with pytest.raises(E.SealError, match="对不上"):
        E.open_file(p, tmp_path / "x.jpg", other)
    raw = bytearray(p.read_bytes())
    raw[len(E.MAGIC) + 40] ^= 1
    (tmp_path / "bad.d1e").write_bytes(bytes(raw))
    with pytest.raises(E.SealError, match="对不上"):
        E.open_file(tmp_path / "bad.d1e", tmp_path / "x.jpg", priv)
    (tmp_path / "short.d1e").write_bytes(p.read_bytes()[:20])
    with pytest.raises(E.SealError, match="太短"):
        E.open_file(tmp_path / "short.d1e", tmp_path / "x.jpg", priv)
    assert not (tmp_path / "x.jpg").exists() and not list(tmp_path.glob("*.part"))


def test_钥匙文件_私钥0600_有了不换_公钥十六进制(tmp_path):
    pub = E.write_keypair(tmp_path / "k" / "evidence.key", tmp_path / "evidence-pub.key")
    assert (tmp_path / "k" / "evidence.key").stat().st_mode & 0o777 == 0o600
    assert E.load_public(tmp_path / "evidence-pub.key") == pub
    assert E.public_key(E.load_private(tmp_path / "k" / "evidence.key")) == pub
    with pytest.raises(E.SealError, match="不换"):
        E.write_keypair(tmp_path / "k" / "evidence.key", tmp_path / "evidence-pub.key")
    (tmp_path / "bad.key").write_text("abcd")
    with pytest.raises(E.SealError):
        E.load_public(tmp_path / "bad.key")


def test_名字():
    assert E.plain_name("x.jpg.d1e") == "x.jpg" and E.plain_name("x.jpg") is None
    assert E.sealed_name("seg.mp4") == "seg.mp4.d1e"


def test_封到一半的临时文件是点开头的_发件箱扫盘不排它(tmp_path, monkeypatch):
    seen = []
    real = E._openssl

    def 记(args, **kw):
        if kw.get("dst") is not None:
            seen.append(Path(kw["dst"]).name)
        return real(args, **kw)
    monkeypatch.setattr(E, "_openssl", 记)
    priv, pub = E.keypair()
    E.Sealer(pub).seal_bytes(b"x" * 100, tmp_path / "a.jpg.d1e")
    src = tmp_path / "s.mp4"
    src.write_bytes(b"y" * 100)
    E.Sealer(pub).seal_file(src, tmp_path / "s.mp4.d1e")
    assert seen and all(n.startswith(".") for n in seen), seen
