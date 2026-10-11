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
    gate = [] if "--gate" in more else ["--no-gate"]
    return ["--repo", str(repo), "--key", str(key), "--out", str(out), "--skip-android",
            *gate, *more]


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
    for name in ("--key", "--out", "--wheels", "--repo", "--gate", "--write-gate"):
        p.add_argument(name, default=None)
    p.add_argument("--no-gate", action="store_true")
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
    argv = ["--repo", str(repo), "--key", str(key), "--out", str(out), "--no-gate"]
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


def _gate(world, tmp_path, *more) -> Path:
    dest = tmp_path / "gate" / "gate.json"
    assert mr.run(_parse(["--repo", str(world[0]), "--write-gate", str(dest), *more]),
                  now_ms=NOW - 5000) == 0
    return dest


def test_发行闸_过了闸的同一份_发得出去_记录里写着(world, tmp_path, capsys):
    repo, key, out = world
    dest = _gate(world, tmp_path)
    gate = json.loads(dest.read_text("utf-8"))
    assert gate["kind"] == "d1max-release-gate" and len(gate["commit"]) == 40
    assert not gate["wheels"]
    assert mr.run(_parse(["--repo", str(repo), "--key", str(key), "--out", str(out),
                          "--skip-android", "--gate", str(dest)]), now_ms=NOW) == 0
    pkg = next(p for p in out.iterdir() if p.is_dir())
    rec = json.loads((out / f"{pkg.name}.release-record.json").read_text("utf-8"))
    assert rec["gate"] == {"created_ms": NOW - 5000, "run": None}
    assert rec["content_sha256"] == gate["content_sha256"], "隔了一阵、换了目录再打,指纹要一样"
    assert "发行闸        过了" in capsys.readouterr().out


def test_发行闸_不给记录不发_明说不过闸才发_记录里写明(world, capsys):
    repo, key, out = world
    argv = ["--repo", str(repo), "--key", str(key), "--out", str(out), "--skip-android"]
    assert mr.run(_parse(argv), now_ms=NOW) == 2
    assert "--gate" in capsys.readouterr().err and not out.exists()
    assert mr.run(_parse([*argv, "--no-gate"]), now_ms=NOW) == 0
    pkg = next(p for p in out.iterdir() if p.is_dir())
    assert json.loads((out / f"{pkg.name}.release-record.json").read_text("utf-8"))["gate"] is None
    assert "没过闸" in capsys.readouterr().out


def test_发行闸_过的是别的提交_不发(world, tmp_path, capsys):
    repo = world[0]
    dest = _gate(world, tmp_path)
    (repo / "src/new.py").write_text("x = 1\n", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "after gate")
    assert "发行闸过的是提交" in _refused(world, capsys, "--gate", str(dest))


def test_发行闸_同一个提交但打出来的包不一样_不发_半成品删掉(world, tmp_path, capsys):
    repo, key, out = world
    dest = _gate(world, tmp_path)
    gate = json.loads(dest.read_text("utf-8"))
    gate["content_sha256"] = "0" * 64
    dest.write_text(json.dumps(gate), "utf-8")
    assert mr.run(_parse(_args(repo, key, out, "--gate", str(dest))), now_ms=NOW) == 2
    assert "指纹跟发行闸记的不一样" in capsys.readouterr().err
    assert not [p for p in out.iterdir() if p.is_dir()]
    assert not list(out.glob("*.release-record.json"))


def test_发行闸_带没带轮子不一致_不发(world, tmp_path, capsys):
    repo, key, out = world
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "a-1-py3-none-any.whl").write_bytes(b"w")
    dest = _gate(world, tmp_path, "--wheels", str(wheels))
    assert json.loads(dest.read_text("utf-8"))["wheels"] is True
    assert mr.run(_parse(_args(repo, key, out, "--gate", str(dest))), now_ms=NOW) == 2
    assert "--wheels 要跟闸一致" in capsys.readouterr().err


def test_发行闸_记录读不了_不是闸的记录_不发(world, tmp_path, capsys):
    bad = tmp_path / "x.json"
    assert "读不了发行闸的记录" in _refused(world, capsys, "--gate", str(bad))
    bad.write_text('{"kind": "else", "commit": "a", "content_sha256": "b"}', "utf-8")
    assert "不是发行闸的记录" in _refused(world, capsys, "--gate", str(bad))


def test_发行闸_工作区不干净不出记录(world, tmp_path, capsys):
    (world[0] / "stray.py").write_text("x = 1\n", "utf-8")
    dest = tmp_path / "gate.json"
    assert mr.run(_parse(["--repo", str(world[0]), "--write-gate", str(dest)]), now_ms=NOW) == 2
    assert not dest.exists() and "没提交的改动" in capsys.readouterr().err


def test_发行闸的工作流_整套_ci_严格跑_点名的测试文件都在_不碰钥匙():
    import yaml
    wf = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text("utf-8"))
    assert wf["jobs"]["ci"] == {"uses": "./.github/workflows/ci.yml", "with": {"strict": True}}
    assert wf["jobs"]["gate"]["needs"] == "ci"
    text = (ROOT / ".github/workflows/release.yml").read_text("utf-8")
    assert "secrets." not in text, "钥匙不上 GitHub(决策 54)"
    import re
    named = re.findall(r"(?:packages|tests)/[\w/.-]+\.py", text)
    assert len(named) >= 11 and all((ROOT / n).is_file() for n in named), named
    ci = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
    assert "continue-on-error: ${{ !inputs.strict }}" in ci
    assert "group: ${{ github.workflow }}-${{ github.ref }}" in ci, "闸调进来不许顶掉平时那一趟"


def test_CI_的安卓包是调试签名_发行脚本才要求正式钥匙():
    ci = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
    assert "requireReleaseKey" not in ci
    assert "-Pd1max.requireReleaseKey=true" in (ROOT / "tools/make_release.py").read_text("utf-8")
