"""故障注入。

只维护一份可变状态并解析命令行,不碰网络也不碰状态机 ——
施加动作由 nav_server 在 tick 循环里完成。这样注入语义能被单测锁死。

命令清单见设计文档 §7.1。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from d1max_patrol.protocol.nav_frames import AlgErrorItem
from d1max_patrol.protocol.nav_types import ALG_LIDAR_DISCONNECTED, ALG_NAV_BLOCKED

#: 已知故障码的默认描述,与 refs/nav-api §9 的样例一致
_KNOWN_CODES = {
    ALG_NAV_BLOCKED: "navigation blocked",
    ALG_LIDAR_DISCONNECTED: "lidar disconnected",
}

#: 属 SDK 链路的注入命令,第 2 卷实现
_SDK_ONLY = {"battery", "fault", "control_lost"}

HELP_TEXT = """\
可用注入命令:
  loc_lost                 制造定位丢失
  loc_ok                   从定位丢失中恢复
  alg_error <码> [严重度]   推送一条算法故障码
  nav_fail                 让下一次 start_nav 失败
  slow <倍数>              行走减速,slow 2 表示慢一倍
  stuck on|off             状态正常但完全不动
  frame_count_zero on|off  响应的 frame_count 一律填 0,逼客户端走名字回退匹配
  reorder <秒>             响应统一延迟,制造乱序到达
  disconnect <秒>          断开所有连接并静默这么久
  reset                    清空所有注入
  status                   回显当前注入状态
  help                     显示本帮助
"""


class InjectError(ValueError):
    """命令无法解析或参数非法。"""


@dataclass
class FaultState:
    """当前生效的注入。字段被 nav_server 在每个 tick 读取。"""

    #: 运动速度缩放,1.0 为正常
    speed_scale: float = 1.0
    #: 完全不动
    stuck: bool = False
    #: 响应 frame_count 一律填 0
    frame_count_zero: bool = False
    #: 响应统一延迟秒数
    response_delay_s: float = 0.0
    #: 待执行的断链秒数,服务端消费后清零
    disconnect_seconds: float = 0.0
    #: 下一次 start_nav 失败
    fail_next_nav: bool = False
    #: 待执行的定位丢失 / 恢复,服务端消费后清零
    loc_lost_requested: bool = False
    loc_recover_requested: bool = False
    #: 待推送的算法故障码
    queued_alg_errors: list[AlgErrorItem] = field(default_factory=list)

    def reset(self) -> None:
        self.speed_scale = 1.0
        self.stuck = False
        self.frame_count_zero = False
        self.response_delay_s = 0.0
        self.disconnect_seconds = 0.0
        self.fail_next_nav = False
        self.loc_lost_requested = False
        self.loc_recover_requested = False
        self.queued_alg_errors.clear()

    def take_alg_errors(self) -> list[AlgErrorItem]:
        """取走待推送的故障码并清空队列。"""
        items = list(self.queued_alg_errors)
        self.queued_alg_errors.clear()
        return items

    def describe(self) -> str:
        return (
            f"speed_scale={self.speed_scale} stuck={self.stuck} "
            f"frame_count_zero={self.frame_count_zero} "
            f"response_delay_s={self.response_delay_s} "
            f"fail_next_nav={self.fail_next_nav} "
            f"queued_alg_errors={len(self.queued_alg_errors)}"
        )


def _on_off(token: str | None, name: str) -> bool:
    if token is None or token.lower() not in ("on", "off"):
        raise InjectError(f"{name} 的参数必须是 on 或 off")
    return token.lower() == "on"


def _positive_float(token: str | None, name: str) -> float:
    if token is None:
        raise InjectError(f"{name} 缺少参数")
    try:
        value = float(token)
    except ValueError as exc:
        raise InjectError(f"{name} 的参数必须是数字: {token!r}") from exc
    if value <= 0:
        raise InjectError(f"{name} 的参数必须大于 0,实际为 {value}")
    return value


def apply_command(state: FaultState, line: str) -> str:
    """解析并应用一条注入命令,返回给操作者看的结果字符串。"""
    parts = line.strip().split()
    if not parts:
        raise InjectError("命令为空")
    name = parts[0].lower()
    args = parts[1:]

    if name in _SDK_ONLY:
        raise InjectError(f"{name} 属 SDK 链路的注入,本卷未实现,见第 2 卷")

    if name == "help":
        return HELP_TEXT
    if name == "status":
        return state.describe()
    if name == "reset":
        state.reset()
        return "已清空所有注入"

    if name == "loc_lost":
        state.loc_lost_requested = True
        return "已请求定位丢失"
    if name == "loc_ok":
        state.loc_recover_requested = True
        return "已请求定位恢复"

    if name == "alg_error":
        if not args:
            raise InjectError("alg_error 缺少故障码")
        try:
            code = int(args[0])
        except ValueError as exc:
            raise InjectError(f"故障码必须是整数: {args[0]!r}") from exc
        severity = 0
        if len(args) > 1:
            try:
                severity = int(args[1])
            except ValueError as exc:
                raise InjectError(f"严重度必须是整数: {args[1]!r}") from exc
        state.queued_alg_errors.append(
            AlgErrorItem(code=code,
                         description=_KNOWN_CODES.get(code, "injected"),
                         severity=severity)
        )
        return f"已排队故障码 {code}"

    if name == "nav_fail":
        state.fail_next_nav = True
        return "下一次 start_nav 将失败"

    if name == "slow":
        factor = _positive_float(args[0] if args else None, "slow")
        state.speed_scale = 1.0 / factor
        return f"行走速度缩放为 {state.speed_scale}"

    if name == "stuck":
        state.stuck = _on_off(args[0] if args else None, "stuck")
        return f"stuck={state.stuck}"

    if name == "frame_count_zero":
        state.frame_count_zero = _on_off(args[0] if args else None, "frame_count_zero")
        return f"frame_count_zero={state.frame_count_zero}"

    if name == "reorder":
        state.response_delay_s = _positive_float(args[0] if args else None, "reorder")
        return f"响应延迟 {state.response_delay_s}s"

    if name == "disconnect":
        state.disconnect_seconds = _positive_float(
            args[0] if args else None, "disconnect")
        return f"将断链 {state.disconnect_seconds}s"

    raise InjectError(f"未知命令: {name!r}。输入 help 查看可用命令")
