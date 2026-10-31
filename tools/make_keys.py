#!/usr/bin/env python3
"""生成公司的钥匙(W57,`docs/钥匙生成与保管.md`;W63 加第四把出厂钥匙):**你本人跑一次**。
现阶段放台式机 ~/nexus-keys,正式发行前挪到加密 U 盘。

    .venv/bin/python tools/make_keys.py --out ~/nexus-keys --org "<公司名>"

生成:
- ``release.key`` / ``release-pub.pem``:发行签名钥匙(站点、机器人的升级包,App 的签名清单)。
- ``unbind.key`` / ``unbind-pub.pem``:解绑令钥匙(跟发行钥匙分开)。
- ``factory.key`` / ``factory-pub.pem``:出厂钥匙(W63:签每块上装板的出厂证书;
  站点只让它签过的机器人加入)。
- ``android-release.jks``:安卓签名钥匙(keytool 会当场问你两次密码 —— 记牢,写在保管记录里)。
- ``钥匙说明.txt``:每一把是什么、编号、生成时间、下一步。

不覆盖已有的文件;输出目录不许在仓库里。私钥文件权限 600。什么都不上传。

已经有前三把(W57 生成过)、只补出厂钥匙:

    .venv/bin/python tools/make_keys.py --out ~/nexus-keys --factory-only
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/contract/src",):
    sys.path.insert(0, str(ROOT / _p))

FILES = ("release.key", "release-pub.pem", "unbind.key", "unbind-pub.pem", "factory.key",
         "factory-pub.pem", "android-release.jks", "钥匙说明.txt")
FACTORY_FILES = ("factory.key", "factory-pub.pem")


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def make(out: Path, *, org: str, android: bool = True,
         keytool: str | None = None) -> list[str]:
    """生成到 ``out``,回生成了哪些文件。出错抛 ``SystemExit``(说清楚)。"""
    from d1max_contract import ed25519, relsign
    from d1max_contract.binding import key_id
    if _inside(out, ROOT):
        raise SystemExit(f"输出目录 {out} 在仓库里面:放到仓库外面(~/nexus-keys 或加密 U 盘)")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    out.chmod(0o700)
    clash = [f for f in FILES if (out / f).exists()]
    if clash:
        raise SystemExit(f"{out} 里已经有 {clash}:不覆盖(钥匙丢不起;换一个空目录)")
    tool = keytool or shutil.which("keytool")
    if android and tool is None:
        raise SystemExit("找不到 keytool(装 openjdk-17-jdk);或者先加 --skip-android 只生成另外两把")
    relsign.keygen(out / "release.key", out / "release-pub.pem")
    seed = ed25519.new_seed()
    (out / "unbind.key").write_bytes(ed25519.private_pem(seed))
    (out / "unbind-pub.pem").write_bytes(ed25519.public_pem(ed25519.public_key(seed)))
    for f in ("release.key", "unbind.key"):
        (out / f).chmod(0o600)
    fac_id = _factory_key(out)
    made = ["release.key", "release-pub.pem", "unbind.key", "unbind-pub.pem", *FACTORY_FILES]
    if android:
        print("\n接下来 keytool 会问两次密码(keystore 密码):记牢,写进保管记录。\n")
        rc = subprocess.run([tool, "-genkeypair", "-keystore", str(out / "android-release.jks"),
                             "-alias", "warden", "-keyalg", "RSA", "-keysize", "4096",
                             "-validity", "10000", "-dname", f"CN=Nexus Warden, O={org}"]
                            ).returncode
        if rc != 0 or not (out / "android-release.jks").is_file():
            raise SystemExit("keytool 没生成安卓签名钥匙。另外几把已经生成好了,留着;安卓那把按 "
                             "docs/钥匙生成与保管.md 里的 keytool 命令单独补")
        (out / "android-release.jks").chmod(0o600)
        made.append("android-release.jks")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rel_id = relsign.key_id(out / "release-pub.pem")
    unb_id = key_id(ed25519.public_key(seed))
    android_state = "已生成" if android else "未生成(--skip-android)"
    (out / "钥匙说明.txt").write_text(f"""Nexus Warden 公司钥匙(生成于 {now} UTC)

1. 发行签名钥匙  release.key(私钥)/ release-pub.pem(公钥)  编号 {rel_id}
   签:站点、机器人的升级包,App 的签名清单。
   下一步:把 release-pub.pem 复制到仓库的 deploy/release-pub.pem,提交(走 PR)。
   私钥只留在这里。
2. 解绑令钥匙  unbind.key(私钥)/ unbind-pub.pem(公钥)  编号 {unb_id}
   签:解绑令。公钥在出厂烧写时装进每台机器人(factory burn --unbind-pub unbind-pub.pem)。
   签发按 docs/解绑令签发流程.md(每张记台账)。
3. 安卓签名钥匙  android-release.jks(别名 warden)  {android_state}
   签:安卓 App。发行机上 mobile/android/key.properties 指向它(见 docs/正式发行手册.md)。
   密码不写在这里:写在纸质保管记录上,跟 U 盘分开放。
{_factory_note(fac_id, 4)}
保管:现阶段放台式机,至少再拷一份离线备份;正式发行前挪到两个加密 U 盘(分开放),删掉台式机上的。
不要放 GitHub、网盘、聊天软件、邮箱。丢了或怀疑泄露:见 docs/钥匙生成与保管.md 第四节。
""", encoding="utf-8")
    made.append("钥匙说明.txt")
    return made


def _factory_key(out: Path) -> str:
    from d1max_contract import device_id as di
    from d1max_contract import ed25519
    seed = ed25519.new_seed()
    (out / "factory.key").write_bytes(ed25519.private_pem(seed))
    (out / "factory.key").chmod(0o600)
    pub = ed25519.public_key(seed)
    (out / "factory-pub.pem").write_bytes(ed25519.public_pem(pub))
    return di.key_id(pub)


def _factory_note(kid: str, n: int) -> str:
    return f"""{n}. 出厂钥匙  factory.key(私钥)/ factory-pub.pem(公钥)  编号 {kid}
   签:每块上装板的出厂证书(factory sign-device,或 tools/factory.py 一条命令烧一台)。
   站点只让它签过出厂证书的机器人加入。
   下一步:把 factory-pub.pem 复制到仓库的 deploy/factory-pub.pem,提交(走 PR)。
   私钥只留在这里;每签一台记出厂台账(公司内部,不进仓库)。
"""


def make_factory(out: Path) -> list[str]:
    """W63:已有前三把,只补出厂钥匙;钥匙说明里追加一段。"""
    if _inside(out, ROOT):
        raise SystemExit(f"输出目录 {out} 在仓库里面:放到仓库外面(~/nexus-keys 或加密 U 盘)")
    clash = [f for f in FACTORY_FILES if (out / f).exists()]
    if clash:
        raise SystemExit(f"{out} 里已经有 {clash}:不覆盖(钥匙丢不起)")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    kid = _factory_key(out)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with (out / "钥匙说明.txt").open("a", encoding="utf-8") as f:
        f.write(f"\n(补充于 {now} UTC)\n" + _factory_note(kid, 4))
    return list(FACTORY_FILES)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成公司的钥匙(在你自己的电脑上跑)")
    ap.add_argument("--out", required=True, help="输出目录(仓库外的空目录,如 ~/nexus-keys)")
    ap.add_argument("--org", default="Nexus Dynamic", help="公司名(写进安卓签名证书)")
    ap.add_argument("--skip-android", action="store_true", help="不生成安卓签名钥匙")
    ap.add_argument("--factory-only", action="store_true",
                    help="已有前三把,只补出厂钥匙(W63)")
    a = ap.parse_args(argv)
    if a.factory_only:
        made = make_factory(Path(a.out).expanduser())
    else:
        made = make(Path(a.out).expanduser(), org=a.org, android=not a.skip_android)
    print(f"\n生成好了({len(made)} 个文件):{', '.join(made)}")
    print(f"先看 {Path(a.out) / '钥匙说明.txt'} 里的下一步;再把整个目录拷一份离线备份。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
