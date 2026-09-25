"""特权助手的客户端(W01b)。

服务以 ``User=robot`` 跑,而 OTA 要做的三件事都要 root:把随包发布的单元装进
``/etc/systemd/system``、``daemon-reload``、``systemctl restart``/``reboot``。
这些由 ``deploy/d1max-privileged``(root 拥有,``install.sh`` 装到
``/usr/local/sbin``)去做,``robot`` 通过 ``/etc/sudoers.d/d1max`` 那一行白名单
免密只能跑它。这个模块是 app 与 CLI 调它的唯一入口。

**助手不在(开发机、老装机)时一律退回现状**:单元不装(回一句 ``skipped:``
说怎么补),重启用裸 ``systemctl``。助手在而 sudo 不通,才是要喊出来的错 ——
原来 ``_spawn_restart`` 只 ``Popen`` 不看结果,被 polkit 拒了界面照样显示
「已重启」(``docs/真机待验证清单.md`` 第 6 条),这里把它变成看得见的失败。

真正起子进程的只有 ``runner``,可注入;engine 层的其他地方不许直接碰 sudo。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from d1max_agent.engine.selfcheck import RestartPlan

#: 助手装在哪。**必须是 root 拥有的目录** —— ``/opt/d1max`` 整棵归 robot,
#: sudo 白名单指着 robot 可写的脚本等于把 root 送给 robot。
HELPER = Path("/usr/local/sbin/d1max-privileged")

#: ``check`` 只是 ``exit 0``,超过这个数就是 sudo 在等口令(``-n`` 下会直接失败)
#: 或者机器卡死了。
_CHECK_TIMEOUT_S = 5.0
#: ``install-unit`` 里有一次 ``daemon-reload``,慢机器上也就一两秒。
_INSTALL_TIMEOUT_S = 30.0


class PrivilegedError(Exception):
    """助手在,但这一次没成:sudo 不通、单元被拒、超时。文本是给现场看的人话。"""


@dataclass(frozen=True)
class Privileged:
    helper: Path = HELPER
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def _argv(self, *args: str) -> list[str]:
        # ``-n``:绝不停下来等口令。白名单是 NOPASSWD 的;要是它不在,sudo 会
        # 立刻失败而不是把服务挂在一个永远没人回答的提示上。
        return ["sudo", "-n", str(self.helper), *args]

    def present(self) -> bool:
        """助手文件在不在。不在 = 开发机或 W01b 之前装的机器,一切退回现状。"""
        return self.helper.is_file()

    def available(self) -> bool:
        """助手在、而且 ``sudo -n … check`` 真的通。"""
        if not self.present():
            return False
        try:
            got = self.runner(self._argv("check"), capture_output=True, text=True,
                              timeout=_CHECK_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return got.returncode == 0

    def install_unit(self, name: str) -> str:
        """让助手把 ``releases/<name>/deploy/d1max-patrol.service`` 装进系统。

        回 ``installed`` / ``unchanged``(助手 stdout 的最后一行),助手不在时回
        ``skipped: …``。被拒、炸了、超时 → :class:`PrivilegedError`,带助手的原话。
        """
        if not self.present():
            return ("skipped: 没有特权助手,单元文件没更新 —— "
                    "重跑一次 deploy/install.sh 会装上它")
        try:
            got = self.runner(self._argv("install-unit", name), capture_output=True,
                              text=True, timeout=_INSTALL_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise PrivilegedError(f"装单元超时({_INSTALL_TIMEOUT_S:g}s)") from exc
        except OSError as exc:
            raise PrivilegedError(f"跑不起特权助手: {exc}") from exc
        if got.returncode != 0:
            why = (got.stderr or got.stdout or "").strip() or f"退出码 {got.returncode}"
            raise PrivilegedError(why)
        lines = (got.stdout or "").strip().splitlines()
        return lines[-1] if lines else "installed"

    def restart_argv(self, plan: RestartPlan) -> tuple[str, ...]:
        """按方案给出真正要起的命令。助手在就走它;不在就是方案里那条裸命令。"""
        if not self.present():
            return tuple(plan.argv)
        return tuple(self._argv("reboot" if plan.kind == "machine" else "restart"))

    def restart(self, plan: RestartPlan) -> None:
        """真去重启。

        助手在:**同步跑** ``sudo -n helper restart|reboot`` 并等它退 0。助手的
        restart 只负责让 systemd-run 把 stop→migrate→start 排进一个临时单元,自己
        不停服务,所以等得起;排不进去(systemd-run 不在、单元名冲突、systemd
        拒绝、助手炸了)退非零 → :class:`PrivilegedError`。原来这里 ``Popen`` 完就
        算成功,这些失败全看不见,界面照样显示「已重启」(外部审核阻断项)。
        **能确认的是「排进去了」**,稍后那个临时单元里的 start 成不成,只有
        journal 知道(``真机待验证`` 里有查法)。

        助手不在:退回裸 ``systemctl restart``。它会先把我们停掉、永远不正常返回,
        只能 ``Popen`` 不等 —— 跟 W01b 之前一样。
        """
        argv = list(self.restart_argv(plan))
        if not self.present():
            subprocess.Popen(argv, start_new_session=True)
            return
        try:
            got = self.runner(argv, capture_output=True, text=True,
                              timeout=_INSTALL_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise PrivilegedError(f"重启命令超时({_INSTALL_TIMEOUT_S:g}s)") from exc
        except OSError as exc:
            raise PrivilegedError(f"跑不起特权助手: {exc}") from exc
        if got.returncode != 0:
            why = (got.stderr or got.stdout or "").strip() or f"退出码 {got.returncode}"
            raise PrivilegedError(f"重启命令发不出去: {why}")

    def ensure_can_restart(self) -> None:
        """重启命令发得出去吗。助手不在 → 不管(退回裸 systemctl,现状);
        在但 sudo 不通 → 抛,**在切链之前**就让人知道,而不是切完了假装已重启。
        """
        if not self.present():
            return
        if not self.available():
            raise PrivilegedError(
                f"重启命令发不出去:sudo -n {self.helper} check 没通 —— "
                "sudo 白名单(/etc/sudoers.d/d1max)没装或被改了,重跑 deploy/install.sh")
