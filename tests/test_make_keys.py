"""W57:公司三把钥匙的生成脚本(tools/make_keys.py)。"""

from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("make_keys", ROOT / "tools" / "make_keys.py")
mk = importlib.util.module_from_spec(spec)
sys.modules["make_keys"] = mk
spec.loader.exec_module(mk)


def _fake_keytool(tmp_path: Path) -> str:
    kt = tmp_path / "keytool"
    kt.write_text('#!/bin/sh\n'
                  'while [ $# -gt 0 ]; do [ "$1" = "-keystore" ] && out=$2; shift; done\n'
                  'echo JKS > "$out"\n')
    kt.chmod(0o755)
    return str(kt)


def test_三把都生成_私钥只有自己读_说明里有编号(tmp_path):
    out = tmp_path / "usb" / "nexus-keys"
    made = mk.make(out, org="Test Co", keytool=_fake_keytool(tmp_path))
    assert set(made) == set(mk.FILES)
    for f in ("release.key", "unbind.key", "android-release.jks"):
        assert stat.S_IMODE((out / f).stat().st_mode) == 0o600, f
    from d1max_contract import relsign
    note = (out / "钥匙说明.txt").read_text()
    assert relsign.key_id(out / "release-pub.pem") in note and "解绑令" in note
    from d1max_contract import ed25519
    from d1max_contract.binding import sign_unbind, verify_unbind
    seed = ed25519.seed_from_pem((out / "unbind.key").read_bytes())
    tok = sign_unbind(seed, "D1M-1", now_ms=1_800_000_000_000, hours=24)
    pub = ed25519.pub_from_pem((out / "unbind-pub.pem").read_bytes())
    assert verify_unbind(tok, pub, serial="D1M-1", now_ms=1_800_000_000_000).serial == "D1M-1"


def test_不覆盖已有的_不许放仓库里(tmp_path):
    out = tmp_path / "keys"
    out.mkdir()
    (out / "release.key").write_text("old")
    with pytest.raises(SystemExit, match="不覆盖"):
        mk.make(out, org="x", android=False)
    assert (out / "release.key").read_text() == "old"
    with pytest.raises(SystemExit, match="仓库里面"):
        mk.make(ROOT / "runs" / "keys-should-not-be-here", org="x", android=False)
