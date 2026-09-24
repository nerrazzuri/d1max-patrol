"""盘水位的判据。**纯函数,不碰盘。**

"盘满那一刻该说什么话"是最难在现场复现的逻辑之一 —— 你得先把盘写满。
做成纯函数,组合就能离机跑全(跟 spec §8.5 对回滚判据的要求同一个道理)。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 报警线。**不能等到拦停才说话** —— 中间这 10% 是留给人反应的时间,
#: 跟电量那三条线是同一个道理。
WARN_USED_RATIO = 0.80
#: 拦停线。**是"不许出发",不是"就地中止"**:已经在跑的那一趟不因为盘满而停,
#: 半趟数据比空盘值钱得多(spec §4.7)。
STOP_USED_RATIO = 0.90


@dataclass(frozen=True, slots=True)
class StorageVerdict:
    ok: bool        # 能不能起飞
    warn: bool      # 要不要报警
    detail: str     # 给人看的话。**说病因,不说症状。**


def _age(days: float | None) -> str:
    return "很久" if days is None else f"{days:.0f} 天"


def storage_verdict(*, free_mb: float, used_ratio: float, min_free_mb: float,
                    has_upload: bool,
                    last_upload_age_days: float | None) -> StorageVerdict:
    """两条门槛并列,**都得过**(spec §4.7)。

    ``free >= min_free_mb`` 且 ``used <= 90%``。大盘上 90% 还剩几十 GB,
    小盘上 500MB 已经是 99% —— 谁先触发听谁的。

    拦停时的话按形态分两套,因为**两档的病根本不是同一个**:联网档 90% 本来
    就不该触发(传成功的都标了可删,水位一到自动清),真触发了说明回传断了很久;
    单机档的"90% + 保留期未到"是个必然会撞上的死锁,而**那个死锁是故意留的**
    —— 它逼出一次人的确认,而这正是"删掉唯一副本"应该有的门槛。
    """
    used_pct = used_ratio * 100.0
    warn = used_ratio >= WARN_USED_RATIO
    room_ok = free_mb >= min_free_mb
    ratio_ok = used_ratio <= STOP_USED_RATIO
    if room_ok and ratio_ok:
        return StorageVerdict(True, warn,
                              f"剩余 {free_mb:.0f}MB,已用 {used_pct:.0f}%")
    if has_upload:
        # 说病因。说"盘满了"会让人去换块大盘,而那治不了回传断掉这件事。
        detail = (f"回传已中断{_age(last_upload_age_days)},盘要满了"
                  f"(剩余 {free_mb:.0f}MB,已用 {used_pct:.0f}%)")
    else:
        detail = (f"盘已用 {used_pct:.0f}%(剩余 {free_mb:.0f}MB),"
                  f"而文件都还在保留期内 —— 需要人在 app 上「导出并释放」:"
                  f"先打包拉走,确认拿到了,才删")
    return StorageVerdict(False, True, detail)
