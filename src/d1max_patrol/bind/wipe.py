"""解绑 = 恢复出厂(W42 §5.4,用户 10-30):这个客户的东西全部删掉,出厂就有的留着。

- **删**:数据根整个(地图、发件箱、事件簿、录像片段、人员截图、幂等记录……)和指到别的盘的发件箱;
  ``/etc/d1max`` 里跟站点有关的(证书私钥、注册文件、证据公钥);``env`` 里跟站点有关的那几行回出厂值;
  系统日志;路由器里的庄园 WiFi。
- **留**:系统和程序、``device.json``、配网证书、解绑公钥、发行公钥、硬件标定和设备配置。
- 删完对盘做一次 ``fstrim``(固态盘光删文件不保证数据真没了)。

每一步都**照做到底**:一步失败记下来、接着做下一步,最后一起报。调用方(配网服务)状态记成 ``wiping``,
全部成功才改 ``unbound``;没成功下一拍再来一遍(删过的再删一遍没事)。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from d1max_patrol.bind.paths import FACTORY_ENV, BindPaths
from d1max_patrol.bind.router import Router
from d1max_patrol.provision import set_env

log = logging.getLogger(__name__)

#: 我们的服务(清机前全停;绑定后按「开机自启」的那几个再起)。
UNITS = ("d1max-agent", "d1max-localizer", "d1max-nav2", "d1max-obstacles", "d1max-persons",
         "d1max-lidar-merge", "d1max-sidecar")
#: ``/etc/d1max`` 里跟站点有关的(相对路径)。
SITE_FILES = ("tls", "registration.json", "evidence-pub.key")

Run = Callable[[Sequence[str]], int]


#: 停服务最多等多久:代理停在线建图要存盘,单元给了 420 s(外审 I8),这里多等一点。
STOP_TIMEOUT_S = 450


def _run(cmd: Sequence[str]) -> int:
    try:
        return subprocess.run(list(cmd), capture_output=True, timeout=STOP_TIMEOUT_S).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def mountpoint(path: Path) -> Path:
    """``path`` 所在的挂载点(``fstrim`` 要挂载点,给子目录不认;外审 I9)。"""
    p = Path(path).resolve()
    while not os.path.ismount(p) and p != p.parent:
        p = p.parent
    return p


def stop_services(run: Run = _run) -> list[str]:
    bad = []
    for u in UNITS:
        if run(["systemctl", "stop", u]) not in (0, 5):      # 5 = 没这个单元
            bad.append(u)
    return bad


def start_services(run: Run = _run) -> None:
    """绑定好了:开机自启的那几个起来(没开自启的不动)。"""
    for u in UNITS:
        if run(["systemctl", "is-enabled", "--quiet", u]) == 0:
            run(["systemctl", "start", u])


def _clear_dir(d: Path) -> None:
    """删目录里的东西,目录本身留着(属主、权限是装机脚本定的)。"""
    if not d.is_dir():
        return
    for p in d.iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()


def wipe(paths: BindPaths, *, router: Router, run: Run = _run) -> list[str]:
    """清机。回失败了的那几步(空 = 全清干净了)。"""
    failed: list[str] = []
    bad = stop_services(run)
    if bad:
        # 外审 I8:服务没停下就不删(它们还在写);下一轮整套再来
        return [f"停不下:{'、'.join(bad)}(数据还没删)"]
    for d in (paths.data_root, *paths.extra_data):
        try:
            _clear_dir(d)
        except OSError as exc:
            failed.append(f"删 {d}:{exc}")
    for rel in SITE_FILES:
        p = paths.etc / rel
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink(missing_ok=True)
        except OSError as exc:
            failed.append(f"删 {p}:{exc}")
    try:
        for k, v in FACTORY_ENV.items():
            set_env(paths.env, k, v)
    except OSError as exc:
        failed.append(f"改 env:{exc}")
    # 系统日志:先切一个新文件,再把旧的全扔掉
    if run(["journalctl", "--rotate"]) != 0 or run(["journalctl", "--vacuum-time=1s"]) != 0:
        failed.append("清系统日志")
    try:
        router.forget_wifi()
    except Exception as exc:  # noqa: BLE001 - 路由器那份失败记下来,下一拍再来
        failed.append(f"路由器忘 WiFi:{exc}")
    mounts = {mountpoint(d) for d in (Path("/"), paths.data_root, *paths.extra_data)
              if Path(d).exists()}
    for m in sorted(mounts):
        if run(["fstrim", str(m)]) != 0:
            # 盘不支持 trim 也不能卡在清机里出不来:文件已经删了,记下来
            log.warning("fstrim %s 没成(盘不支持?);文件已经删了", m)
    if failed:
        log.error("清机没清干净:%s", ";".join(failed))
    return failed
