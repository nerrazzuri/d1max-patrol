"""正式发行脚本(RC0-3,决策 54):条件不齐一个字节都不写;签出来的包验得过;调试签名的安卓包不认。"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from d1max_contract import relsign

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_791_000_000_000


def _tool():
    spec = importlib.util.spec_from_file_location("make_release", ROOT / "tools/make_release.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["make_release"] = m
    spec.loader.exec_module(m)
    return m


mr = _tool()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


@pytest.fixture()
def world(tmp_path):
    """一棵提交干净的小仓库(带发行公钥)、仓库外面的私钥和输出目录。"""
    repo = tmp_path / "repo"
    for d in ("deploy", "src", "config", "packages"):
        (repo / d).mkdir(parents=True)
        (repo / d / "keep.txt").write_text("x\n", "utf-8")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n', "utf-8")
    key = tmp_path / "usb" / "release.key"
    key.parent.mkdir()
    relsign.keygen(key, repo / "deploy/release-pub.pem")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, key, tmp_path / "out"


def _args(repo, key, out, *more):
    return ["--repo", str(repo), "--key", str(key), "--out", str(out), "--skip-android", *more]


def test_条件齐了_出签好的包和发行记录(world, capsys):
    repo, key, out = world
    assert mr.run(_parse(_args(repo, key, out)), now_ms=NOW) == 0
    pkgs = [p for p in out.iterdir() if p.is_dir()]
    assert len(pkgs) == 1
    assert relsign.check_package(pkgs[0], repo / "deploy/release-pub.pem") == ""
    assert (pkgs[0] / "deploy/release-pub.pem").is_file(), "公钥要跟着包走,装机时装到狗上"
    rec = json.loads((out / f"{pkgs[0].name}.release-record.json").read_text("utf-8"))
    manifest = json.loads((pkgs[0] / "release.json").read_text("utf-8"))
    assert rec["content_sha256"] == manifest["content_sha256"] and rec["version"] == "1.2.3"
    assert len(rec["commit"]) == 40 and rec["android"] is None
    assert rec["release_key_id"] == relsign.key_id(repo / "deploy/release-pub.pem")
    shown = capsys.readouterr().out
    assert manifest["content_sha256"] in shown and "没编(--skip-android)" in shown


def _parse(argv):
    import argparse
    p = argparse.ArgumentParser()
    for name in ("--key", "--out", "--wheels", "--repo"):
        p.add_argument(name, default=None)
    p.add_argument("--build-number", type=int, default=None)
    p.add_argument("--skip-android", action="store_true")
    return p.parse_args(argv)


def _refused(world, capsys, *more) -> str:
    repo, key, out = world
    assert mr.run(_parse(_args(repo, key, out, *more)), now_ms=NOW) == 2
    assert not out.exists(), "条件不齐不许写任何东西"
    return capsys.readouterr().err


def test_有没提交的改动_不发(world, capsys):
    (world[0] / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9"\n', "utf-8")
    assert "没提交的改动" in _refused(world, capsys)


def test_有没进仓库的文件_不发(world, capsys):
    (world[0] / "stray.py").write_text("x = 1\n", "utf-8")
    assert "没提交的改动" in _refused(world, capsys)


def test_公钥没提交进仓库_不发(world, capsys):
    repo = world[0]
    _git(repo, "rm", "-q", "--cached", "deploy/release-pub.pem")
    _git(repo, "commit", "-q", "-m", "drop")
    (repo / "deploy/release-pub.pem").unlink()
    assert "没有提交过的 deploy/release-pub.pem" in _refused(world, capsys)


def test_私钥跟仓库里的公钥不是一对_不发(world, capsys, tmp_path):
    other = tmp_path / "usb" / "other.key"
    relsign.keygen(other, tmp_path / "usb" / "other.pub")
    repo, _, out = world
    assert "不是一对" in _refused((repo, other, out), capsys)


def test_轮换后的新钥匙_公钥在_release_pub_d_里也认(world, tmp_path):
    repo, _, out = world
    new = tmp_path / "usb" / "new.key"
    (repo / "deploy/release-pub.d").mkdir()
    relsign.keygen(new, repo / "deploy/release-pub.d/2027.pem")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rotate")
    assert mr.run(_parse(_args(repo, new, out)), now_ms=NOW) == 0


def test_私钥别人读得到_放在仓库里_都不发(world, capsys):
    repo, key, out = world
    key.chmod(0o644)
    assert "chmod 600" in _refused(world, capsys)
    key.chmod(0o600)
    inside = repo / "release.key"
    inside.write_bytes(key.read_bytes())
    inside.chmod(0o600)
    err = _refused((repo, inside, out), capsys)
    assert "放在仓库里面" in err


def test_私钥不在_输出目录在仓库里_一次全报(world, capsys):
    repo, key, _ = world
    assert mr.run(_parse(_args(repo, key.with_name("nope"), repo / "out")), now_ms=NOW) == 2
    err = capsys.readouterr().err
    assert "发行私钥不在" in err and "在仓库里面" in err
    assert not (repo / "out").exists()


def test_轮子目录是空的_不发(world, capsys, tmp_path):
    (tmp_path / "wheels").mkdir()
    assert "没有 *.whl" in _refused(world, capsys, "--wheels", str(tmp_path / "wheels"))


def test_要编安卓_没有正式_keystore_没给构建号_不发(world, capsys):
    repo, key, out = world
    argv = ["--repo", str(repo), "--key", str(key), "--out", str(out)]
    assert mr.run(_parse(argv), now_ms=NOW) == 2
    err = capsys.readouterr().err
    assert "没有 mobile/android/key.properties" in err and "--build-number" in err
    assert not out.exists()


def test_keystore_配置写的文件不在_不发(world):
    repo, key, out = world
    props = repo / "mobile/android/key.properties"
    props.parent.mkdir(parents=True)
    props.write_text("storePassword=x\nstoreFile=/nonexistent/k.jks\n", "utf-8")
    (repo / ".gitignore").write_text("mobile/android/key.properties\n", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ignore")
    bad = mr.preflight(repo, key, out, android=True, wheels=None)
    assert any("keystore 不在" in b for b in bad)
    assert not any("没提交的改动" in b for b in bad), "key.properties 不进仓库,不算脏"


_CERTS = """Signer #1 certificate DN: {dn}
Signer #1 certificate SHA-256 digest: {sha}
Signer #1 certificate SHA-1 digest: 00
"""


def test_安卓签名人_正式的认_调试的不认_不止一个不认():
    sha = "AB" * 32
    dn, digest = mr.parse_signer(_CERTS.format(dn="CN=Nexus, O=Nexus", sha=sha))
    assert dn == "CN=Nexus, O=Nexus" and digest == sha.lower()
    with pytest.raises(mr.ReleaseFail, match="调试签名"):
        mr.parse_signer(_CERTS.format(dn="C=US, O=Android, CN=Android Debug", sha=sha))
    with pytest.raises(mr.ReleaseFail, match="不是正好一个"):
        mr.parse_signer("")
    two = _CERTS.format(dn="CN=A", sha=sha) + _CERTS.format(dn="CN=B", sha=sha).replace("#1", "#2")
    with pytest.raises(mr.ReleaseFail, match="不是正好一个"):
        mr.parse_signer(two)


def test_签完验不过_半成品删掉(world, monkeypatch):
    repo, key, out = world
    monkeypatch.setattr(relsign, "check_package", lambda *a, **k: "验不过")
    with pytest.raises(mr.ReleaseFail, match="验不过"):
        mr.make_package(repo, key, out, wheels=None, now_ms=NOW)
    assert not [p for p in out.iterdir() if p.is_dir()]


def test_CI_的安卓包是调试签名_发行脚本才要求正式钥匙():
    ci = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
    assert "requireReleaseKey" not in ci
    assert "-Pd1max.requireReleaseKey=true" in (ROOT / "tools/make_release.py").read_text("utf-8")
