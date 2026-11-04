"""配置数据模型。全部冻结,配置在进程启动后不可变。"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 厂商自主导航 WebSocket 默认地址,见 refs/nav-api §概述。
DEFAULT_NAV_URL = "ws://192.168.144.100:10010"


@dataclass(frozen=True)
class NavConfig:
    """自主导航 WebSocket 链路参数。"""

    url: str = DEFAULT_NAV_URL
    #: 单个请求等待响应的上限
    request_timeout_s: float = 5.0
    #: 建链上限
    connect_timeout_s: float = 5.0
    #: 状态轮询周期。协议无状态推送通道,只能轮询,见计划"协议地雷 4"。
    status_poll_interval_s: float = 0.5
    #: 断线重连退避区间
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 15.0


@dataclass(frozen=True)
class AppConfig:
    """进程级配置。后续卷次会在此追加 bridge / media 小节。"""

    nav: NavConfig = field(default_factory=NavConfig)
    runs_dir: str = "runs"
