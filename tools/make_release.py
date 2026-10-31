#!/usr/bin/env python3
"""出一套发给客户的正式包(商业化 RC0-3,决策 54:在离线的发行机上签,钥匙不进 GitHub)。

    .venv/bin/python tools/make_release.py --key /media/usb/d1max-release.key --out ~/release-out \\
        --wheels dist/wheels-aarch64-py310 --build-number 12

**发行闸**(.github/workflows/release.yml):要发的那个提交先在 GitHub 上过一遍闸(全套测试,集成层
必过),闸出一份记录(提交、包指纹)。这里用 `--gate <那份记录>` 对:不是同一个提交、打出来的包
指纹不一样,就不发。钥匙不上 GitHub,闸只管「这份代码测过了」,签名只在这台机器上做。

一条命令做完:先把条件全查一遍(有一条不过就一个字节都不写),再打包、签名、验签,
编正式签名的安卓包并核它不是调试签名,最后写一份发行记录和 SHA256SUMS。

**CI 的 `android` 作业出的包是调试签名的,不能发给客户**;发给客户的只能是这里出来的。
手册:docs/正式发行手册.md。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from d1max_agent.engine.release import ReleaseError, pack
from d1max_contract import relsign

REPO_PUBKEY = Path("deploy/release-pub.pem")
KEY_PROPS = Path("mobile/android/key.properties")
APK = Path("mobile/build/app/outputs/flutter-apk/app-release.apk")
DEBUG_DN = "android debug"


class ReleaseFail(RuntimeError):
    """发行做不下去:原因就是这句话。"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise ReleaseFail(f"git {' '.join(args)} 没成: {out.stderr.strip()}")
    return out.stdout.strip()


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def key_matches(private: Path, public: Path) -> bool:
    """私钥跟仓库里的公钥(含 release-pub.d)是不是一对:拿它签一份假自述,公钥验得过才算。"""
    probe: dict[str, Any] = {"name": "probe", "version": "0", "content_sha256": "0" * 64,
                             "requires_mission_schema": 0}
    try:
        probe["signature"] = relsign.sign(probe, private)
        relsign.verify(probe, public)
    except (relsign.SignError, OSError, ValueError):
        return False
    return True


def preflight(repo: Path, key: Path, out: Path, *, android: bool,
              wheels: Path | None) -> list[str]:
    """发行前的检查,**全查完再报**。空表 = 可以发。"""
    bad: list[str] = []
    try:
        if _git(repo, "status", "--porcelain"):
            bad.append("仓库里有没提交的改动或没进仓库的文件:正式包只能从提交过的代码出")
    except (ReleaseFail, OSError) as exc:
        bad.append(f"这不是一个能用的 git 仓库: {exc}")
    if _inside(out, repo):
        bad.append(f"输出目录 {out} 在仓库里面:放到仓库外面")
    pub = repo / REPO_PUBKEY
    try:
        _git(repo, "ls-files", "--error-unmatch", str(REPO_PUBKEY))
        have_pub = True
    except (ReleaseFail, OSError):
        have_pub = False
        bad.append(f"仓库里没有提交过的 {REPO_PUBKEY}:先 `d1max-patrol release keygen`,"
                   "把公钥提交进仓库")
    if not key.is_file():
        bad.append(f"发行私钥不在: {key}")
    else:
        if stat.S_IMODE(key.stat().st_mode) & 0o077:
            bad.append(f"发行私钥 {key} 别人也读得到:chmod 600")
        if _inside(key, repo):
            bad.append(f"发行私钥 {key} 放在仓库里面:挪到仓库外面(U 盘)")
        if have_pub and not key_matches(key, pub):
            bad.append(f"这把私钥跟仓库里的发行公钥({REPO_PUBKEY} 和 release-pub.d)不是一对")
    if wheels is not None and not any(wheels.glob("*.whl")):
        bad.append(f"离线轮子目录里没有 *.whl: {wheels}")
    if android:
        props = repo / KEY_PROPS
        if not props.is_file():
            bad.append(f"没有 {KEY_PROPS}:安卓正式包要正式 keystore(见手册)")
        else:
            m = re.search(r"^\s*storeFile\s*=\s*(.+?)\s*$", props.read_text(encoding="utf-8"),
                          re.M)
            store = Path(m.group(1)) if m else None
            if store is None:
                bad.append(f"{KEY_PROPS} 里没写 storeFile")
            else:
                if not store.is_absolute():
                    store = repo / "mobile/android/app" / store
                if not store.is_file():
                    bad.append(f"{KEY_PROPS} 写的 keystore 不在: {store}")
        if shutil.which("flutter") is None:
            bad.append("找不到 flutter")
        if apksigner() is None:
            bad.append("找不到 apksigner(设 ANDROID_HOME,装 build-tools)")
    return bad


def apksigner() -> str | None:
    found = shutil.which("apksigner")
    if found:
        return found
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") \
        or str(Path.home() / "Android/Sdk")
    tools = sorted(Path(home).glob("build-tools/*/apksigner"))
    return str(tools[-1]) if tools else None


def parse_signer(text: str) -> tuple[str, str]:
    """`apksigner verify --print-certs` 的输出 → (证书 DN, 证书 SHA-256)。
    **正好一个签名人**才认;调试证书不认。"""
    dns = re.findall(r"^Signer #\d+ certificate DN: (.+)$", text, re.M)
    shas = re.findall(r"^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})$", text, re.M)
    if len(dns) != 1 or len(shas) != 1:
        raise ReleaseFail(f"安卓包的签名人不是正好一个(读到 {len(dns)} 个)")
    if DEBUG_DN in dns[0].lower():
        raise ReleaseFail(f"安卓包是调试签名的({dns[0]}),不能发给客户")
    return dns[0], shas[0].lower()


def read_gate(path: Path) -> dict[str, Any]:
    try:
        gate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseFail(f"读不了发行闸的记录 {path}: {exc}") from exc
    if not isinstance(gate, dict) or gate.get("kind") != "d1max-release-gate" \
            or not isinstance(gate.get("commit"), str) \
            or not isinstance(gate.get("content_sha256"), str):
        raise ReleaseFail(f"{path} 不是发行闸的记录")
    return gate


def check_gate(gate: dict[str, Any], *, commit: str, content_sha256: str, wheels: bool) -> None:
    """闸过的是不是就是手上要发的这一份。"""
    if gate["commit"] != commit:
        raise ReleaseFail(f"发行闸过的是提交 {gate['commit'][:12]},现在要发的是 {commit[:12]}")
    if bool(gate.get("wheels")) != wheels:
        raise ReleaseFail("发行闸打包时带没带离线轮子,跟这次不一样(--wheels 要跟闸一致)")
    if gate["content_sha256"] != content_sha256:
        raise ReleaseFail("打出来的包指纹跟发行闸记的不一样:"
                          "这台机器上的代码或轮子跟闸测的不是同一份")


def write_gate(repo: Path, dest: Path, *, wheels: Path | None, now_ms: int) -> dict[str, Any]:
    """发行闸用:不签名,只打一遍包、记下提交和包指纹。工作区要干净。"""
    if _git(repo, "status", "--porcelain"):
        raise ReleaseFail("仓库里有没提交的改动:闸只认提交过的代码")
    scratch = Path(tempfile.mkdtemp(prefix="d1max-gate-"))
    try:
        pkg = pack(repo, scratch, now_ms=now_ms, wheels=wheels)
        manifest = json.loads((pkg / "release.json").read_text(encoding="utf-8"))
    except ReleaseError as exc:
        raise ReleaseFail(f"打包没成: {exc}") from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    gate = {"kind": "d1max-release-gate", "commit": _git(repo, "rev-parse", "HEAD"),
                    "content_sha256": manifest["content_sha256"], "version": manifest["version"],
                    "wheels": wheels is not None, "created_ms": now_ms}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return gate


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_package(repo: Path, key: Path, out: Path, *, wheels: Path | None, now_ms: int) -> Path:
    """打包 → 签名 → 拿仓库里的公钥验一遍。验不过就删掉半成品。"""
    try:
        pkg = pack(repo, out, now_ms=now_ms, wheels=wheels)
    except ReleaseError as exc:
        raise ReleaseFail(f"打包没成: {exc}") from exc
    try:
        path = pkg / "release.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["signature"] = relsign.sign(manifest, key)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
        why = relsign.check_package(pkg, repo / REPO_PUBKEY)
        if why:
            raise ReleaseFail(f"签完验不过: {why}")
    except (relsign.SignError, OSError, ValueError, ReleaseFail) as exc:
        shutil.rmtree(pkg, ignore_errors=True)
        raise ReleaseFail(str(exc)) from exc
    return pkg


def build_apk(repo: Path, build_name: str, build_number: int) -> Path:
    apk = repo / APK
    apk.unlink(missing_ok=True)                    # 上一次编出来的(可能是调试签名)不许冒充
    cmd = ["flutter", "build", "apk", "--release", "-Pd1max.requireReleaseKey=true",
           "--build-name", build_name, "--build-number", str(build_number)]
    if subprocess.run(cmd, cwd=repo / "mobile").returncode != 0 or not apk.is_file():
        raise ReleaseFail("安卓包没编出来(看上面 flutter 的输出)")
    return apk


def apk_signer(apk: Path) -> tuple[str, str]:
    tool = apksigner()
    if tool is None:
        raise ReleaseFail("找不到 apksigner")
    out = subprocess.run([tool, "verify", "--print-certs", str(apk)], capture_output=True,
                         text=True)
    if out.returncode != 0:
        raise ReleaseFail(f"安卓包验签没过: {out.stderr.strip() or out.stdout.strip()}")
    return parse_signer(out.stdout)


def pubspec_version(repo: Path) -> str:
    # W57:候选版写成 1.0.0-rc.1+构建号 —— 「-rc.1」要留着(安卓包名、App 签名清单里都用它)
    m = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?)",
                  (repo / "mobile/pubspec.yaml").read_text("utf-8"), re.M)
    if not m:
        raise ReleaseFail("mobile/pubspec.yaml 里读不到 version")
    return m.group(1)


def write_record(out: Path, record: dict[str, Any], files: list[Path]) -> None:
    (out / f"{record['package']}.release-record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"{sha256(f)}  {f.name}\n" for f in files]
    (out / f"{record['package']}.SHA256SUMS.txt").write_text("".join(lines), encoding="utf-8")


def run(args: argparse.Namespace, *, now_ms: int) -> int:
    repo = Path(args.repo).resolve()
    if getattr(args, "write_gate", None):
        wheels = Path(args.wheels).expanduser() if args.wheels else None
        try:
            gate = write_gate(repo, Path(args.write_gate).expanduser(), wheels=wheels,
                              now_ms=now_ms)
        except ReleaseFail as exc:
            print(f"没成: {exc}", file=sys.stderr)
            return 2
        print(f"发行闸记录 {args.write_gate}")
        print(f"提交 {gate['commit']}  指纹 {gate['content_sha256']}")
        return 0
    if not args.key or not args.out:
        print("不能发行:\n  - 要给 --key 和 --out", file=sys.stderr)
        return 2
    key = Path(args.key).expanduser()
    out = Path(args.out).expanduser()
    wheels = Path(args.wheels).expanduser() if args.wheels else None
    android = not args.skip_android
    bad = preflight(repo, key, out, android=android, wheels=wheels)
    if android and args.build_number is None:
        bad.append("没给 --build-number:安卓靠它判断新旧,每发一版加一")
    gate: dict[str, Any] | None = None
    gate_arg = getattr(args, "gate", None)
    if gate_arg:
        try:
            gate = read_gate(Path(gate_arg).expanduser())
            head = _git(repo, "rev-parse", "HEAD")
            if gate["commit"] != head:
                bad.append(f"发行闸过的是提交 {gate['commit'][:12]},现在要发的是 {head[:12]}")
        except (ReleaseFail, OSError) as exc:
            bad.append(str(exc))
    elif not getattr(args, "no_gate", False):
        bad.append("没给 --gate <发行闸的记录>(先在 GitHub 上跑 release-gate;"
                   "明知没过闸也要出包就加 --no-gate,发行记录里会写明)")
    if bad:
        print("不能发行:", file=sys.stderr)
        for b in bad:
            print(f"  - {b}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    try:
        pkg = make_package(repo, key, out, wheels=wheels, now_ms=now_ms)
        manifest = json.loads((pkg / "release.json").read_text(encoding="utf-8"))
        if gate is not None:
            try:
                check_gate(gate, commit=_git(repo, "rev-parse", "HEAD"),
                           content_sha256=manifest["content_sha256"], wheels=wheels is not None)
            except ReleaseFail:
                shutil.rmtree(pkg, ignore_errors=True)
                raise
        record: dict[str, Any] = {
            "package": manifest["name"], "version": manifest["version"],
            "content_sha256": manifest["content_sha256"],
            "commit": _git(repo, "rev-parse", "HEAD"),
            "release_key_id": relsign.key_id(repo / REPO_PUBKEY),
            "built_at_ms": now_ms, "android": None,
            "gate": None if gate is None else {"created_ms": gate.get("created_ms"),
                                               "run": gate.get("run")},
        }
        files: list[Path] = []
        if android:
            name = pubspec_version(repo)
            apk = build_apk(repo, name, args.build_number)
            dn, digest = apk_signer(apk)
            dest = out / f"{manifest['name']}-android-{name}+{args.build_number}.apk"
            shutil.copy2(apk, dest)
            files.append(dest)
            # W55:站点分发 App 用的签名清单(同一把发行钥匙;网页上连安装包一起传)
            app_man = {"name": dest.name, "version": f"{name}+{args.build_number}",
                       "requires_mission_schema": 1, "content_sha256": sha256(dest)}
            app_man["signature"] = relsign.sign(app_man, key)
            man_path = out / f"{dest.name}.manifest.json"
            man_path.write_text(json.dumps(app_man, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
            files.append(man_path)
            record["android"] = {"file": dest.name, "version": name,
                                 "build_number": args.build_number, "signer": dn,
                                 "cert_sha256": digest, "sha256": sha256(dest),
                                 "site_manifest": man_path.name}
        write_record(out, record, files)
    except ReleaseFail as exc:
        print(f"没成: {exc}", file=sys.stderr)
        return 2
    print(f"包目录        {pkg}")
    print(f"content_sha256 {record['content_sha256']}")
    print(f"提交          {record['commit']}")
    if record["android"]:
        print(f"安卓包        {record['android']['file']}")
        print(f"安卓证书      {record['android']['cert_sha256']}(第一次发行时抄下来,以后每次对)")
    else:
        print("安卓包        没编(--skip-android)")
    print("发行闸        " + ("过了" if record["gate"] else "**没过闸**(--no-gate)"))
    print(f"发行记录      {out / (record['package'] + '.release-record.json')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--key", default=None, help="发行私钥(离线保管的那一把)")
    p.add_argument("--out", default=None, help="放发行物的目录(在仓库外面)")
    p.add_argument("--gate", default=None, help="发行闸的记录(release-gate 工作流的产物 gate.json)")
    p.add_argument("--no-gate", action="store_true", help="明知没过闸也出包(发行记录里写明)")
    p.add_argument("--write-gate", default=None, metavar="FILE",
                   help="发行闸自己用:不签名,只记下提交和包指纹")
    p.add_argument("--wheels", default=None, help="离线轮子目录(*.whl),拷进包里")
    p.add_argument("--build-number", type=int, default=None, help="安卓构建号,每发一版加一")
    p.add_argument("--skip-android", action="store_true", help="只出站点和狗的包,不编安卓")
    p.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent),
                   help=argparse.SUPPRESS)
    return run(p.parse_args(argv), now_ms=int(time.time() * 1000))


if __name__ == "__main__":
    sys.exit(main())
