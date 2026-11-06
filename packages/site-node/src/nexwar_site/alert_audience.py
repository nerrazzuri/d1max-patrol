"""告警分给谁(W44,设计 §7,用户 2026-10-27):保安只看四类,其余(技术类)给管理员。

- ``intrusion`` 有人闯入:要人马上去。
- ``fire`` 起火、过热:跟有人闯入一样急(热像温度告警,等热像实物接 ISAPI;这一期还没有 kind)。
- ``help`` 机器人需要帮忙:卡住、倒了、迷路、没电、急停、警笛关不上……要人去看。
- ``offline`` 机器人(或固定摄像头)掉线。
- ``owner`` 给管理员:证书、备份、硬盘、加密、版本、后台卡住……保安页面不显示,管理员的提醒、推送照旧。

保安页面只是**不显示**管理员那一类,不是不报。**新加的 kind 要在这张表里写明给谁**(有测试看着);
没写的兜底给管理员 —— 宁可保安少看一条,也不让保安页面混进技术字眼。网页、推送都按这一张表。
"""

from __future__ import annotations

from typing import Literal

Audience = Literal["intrusion", "fire", "help", "offline", "owner"]

GUARD_AUDIENCES: frozenset[str] = frozenset({"intrusion", "fire", "help", "offline"})

_INTRUSION = ("intrusion", "intrusion_unanswered", "intrusion_person", "dog_sees_person",
              "person_left",             # W74 三期:被跟的人越过出口线
              "force_lifted",            # 被抱起来了:可能有人偷机器人
              "force_covered",           # W74 四期:被人罩住了
              "authorized_person")       # 授权的人在场(P3,只记进报警记录,不弹横幅)
_HELP = ("stuck", "nav_blocked", "fallen", "force_flipped", "deter_cornered", "battery_abort",
         "run_abort", "charge_failed", "dock_stuck", "loc_lost_paused", "loc_lost_idle",
         "estop_pressed", "nogo", "payload_siren", "payload_strobe", "payload_spotlight",
         "schedule_missed", "battery_swap", "standby_failed", "lease_expired", "suspend_stale",
         "board_overheat",
         "deter_failed",                       # POC 11-05:那句没喊出去
         "payload_sos", "payload_estop",       # POC 11-06:上装的求助、急停按钮
         "zone_unwatched", "guard_stuck",      # W73:防区没人看 —— 保安盯摄像头或人去
         "swap_soon", "swap_needed", "guard_prep",   # W73 第四期:保安换电池 / 布防前先补能
         "swap_stuck")                         # 外审 r3:拔着电池还在走
_OFFLINE = ("robot_offline", "robot_offline_idle", "cctv_offline")
_OWNER = ("agent_args_skipped", "agent_too_old", "archive_failed", "backup_stale", "board_hot",
          "bind_request", "wifi_change",
          "swap_overdue",                      # W73 第四期:换电等了 20 分钟没人来 → 经理
          "person_overdue",                    # W74:看见人 10 分钟没人到场 → 经理
          # W53 / W52:临时巡逻的通知、账号申请 —— 不进保安的报警列表(保安在「今天的巡逻」里看)
          "adhoc_patrol", "adhoc_queue", "account_request",
          "lens_blocked", "backup_verify_failed", "bundle_lag", "cert_expiring",
          "cert_renew_failed", "charge_overlap",
          "clock_skew",
          "disk_80", "evidence_plain", "evidence_seal_failed", "evidence_unopened", "finding",
          "force_bump", "force_config_bad", "incident_flood", "lane_stuck", "loc_degraded",
          "map_failed", "map_integrity", "payload_amp", "purged_incoming_stuck", "push_failed",
          "recording_delete_failed", "recording_dropped", "recording_failed", "recording_trimmed",
          "release_failed", "rtk", "run_done", "run_start", "schedule_blocked", "schedule_died",
          "schedule_interrupted", "schedule_skipped", "site_disk_low", "site_too_old",
          "upload_backlog", "upload_refused", "watchdog_died")

AUDIENCE_OF: dict[str, Audience] = {
    **{k: "intrusion" for k in _INTRUSION},
    **{k: "help" for k in _HELP},
    **{k: "offline" for k in _OFFLINE},
    **{k: "owner" for k in _OWNER},
}


def audience(kind: str) -> Audience:
    """这一种告警给谁看。没写进表的给管理员。"""
    return AUDIENCE_OF.get(kind, "owner")
