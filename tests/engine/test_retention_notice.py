"""删除预告:先说,说满七天,才允许删(spec §4.6 单机硬约束)。

**为什么要有这个文件。** 一句只显示在 app 上的提示不可验证 —— 狗这边没有
任何东西能证明"说过了",于是那条硬约束在代码里等于没写。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from d1max_patrol.engine.retention import (
    MIN_NOTICE_DAYS,
    NOTICE_REL,
    Forecast,
    RunInfo,
    delete_starts_at,
    forecast,
    read_notice,
    run_key,
    write_notice,
)

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _info(days_ago: float, *, mission: str = "巡检一号", retention: int = 90,
          size: int = 1000) -> RunInfo:
    started = NOW - timedelta(days=days_ago)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    return RunInfo(path=Path("runs") / mission / stamp, mission=mission,
                   started_at=started, retention_days=retention,
                   size_bytes=size, settled=True, uploaded=False)


def test_键是任务名加目录名():
    info = _info(3.0)
    assert run_key(info) == "巡检一号/20260829T120000Z"


def test_七天内要到期的进名单():
    runs = [_info(84.0), _info(83.0), _info(1.0)]
    fc = forecast(runs, now=NOW, used_ratio=0.78)
    assert [r.days_left(NOW) for r in fc.runs] == pytest.approx([6.0, 7.0])


def test_已经过期还没删的也在名单里():
    # §4.4:过期不等于立刻删,真删等水位线。所以"已过期还在盘上"是常态,
    # 而它们恰恰是下一次腾地方时第一批被删的 —— 漏掉就等于没预告。
    fc = forecast([_info(120.0)], now=NOW, used_ratio=0.78)
    assert len(fc.runs) == 1


def test_名单最老在前():
    fc = forecast([_info(85.0), _info(120.0), _info(84.0)],
                  now=NOW, used_ratio=0.5)
    assert [r.days_left(NOW) for r in fc.runs] == pytest.approx([0.0, 5.0, 6.0])


def test_还早的不进名单():
    assert forecast([_info(10.0)], now=NOW, used_ratio=0.5).runs == ()


def test_算得出名单一共多少字节():
    fc = forecast([_info(120.0, size=300), _info(85.0, size=200)],
                  now=NOW, used_ratio=0.5)
    assert fc.bytes_at_risk == 500


def test_提示语里有盘用量和趟数和开始删除的日子():
    fc = forecast([_info(120.0), _info(85.0)], now=NOW, used_ratio=0.78)
    assert "78%" in fc.detail
    assert "2 趟" in fc.detail
    assert "导出" in fc.detail
    assert "开始删除" in fc.detail


def test_提示语报的是开始删除日而不是到期日():
    """spec §4.6 第 1 条的原话是「盘 78%,预计 6 天后开始删除 8 月 12 日之前的
    记录」—— **两个数都要**。

    而**到期日那天什么也不会发生**:预告的钟还没满。报到期日等于报一个必然
    平安无事的日子,客户看到"最早 9-12 到期",9-12 安然无恙,9-15 突然删了。
    """
    老的 = _info(120.0)                        # 2026-05-04 起,90 天前就过期了
    fc = forecast([老的], now=NOW, used_ratio=0.78)
    # 首次预告就是此刻,所以开始删除是 7 天之后,不是那个早就过了的到期日。
    assert "预计 7 天后开始删除" in fc.detail
    assert 老的.expires_at().strftime("%Y-%m-%d") not in fc.detail
    # "之前"是严格早于:取名单里最新那一趟的次日。
    assert "2026-05-05 之前的记录" in fc.detail


def test_钟走了几天预告里的天数就少几天():
    # 台账里已经记着首次预告时刻,forecast 拿它算,不是每次都从头数七天。
    老的 = _info(120.0)
    fc = forecast([老的], now=NOW, used_ratio=0.9,
                  noticed={run_key(老的): NOW - timedelta(days=3)})
    assert "预计 4 天后开始删除" in fc.detail


def test_预告挂满了就说最快今天():
    老的 = _info(120.0)
    fc = forecast([老的], now=NOW, used_ratio=0.9,
                  noticed={run_key(老的): NOW - timedelta(days=MIN_NOTICE_DAYS)})
    assert "最快今天" in fc.detail


def test_还没到期的那一趟拦着它的是保留期():
    # ``max(到期日, 首次预告 + 7 天)``:这一趟的预告早挂满了,轮到保留期说话。
    未到期 = _info(85.0)                       # 还有 5 天到期
    fc = forecast([未到期], now=NOW, used_ratio=0.9,
                  noticed={run_key(未到期): NOW - timedelta(days=30)})
    assert "预计 5 天后开始删除" in fc.detail


def test_开始删除的时刻是两个钟里晚的那个():
    老的 = _info(120.0)
    早就到期 = 老的.expires_at()
    # 预告的钟晚:到期日早就过了,还得再挂满七天。
    assert (delete_starts_at(老的, first_noticed=NOW)
            == NOW + timedelta(days=MIN_NOTICE_DAYS))
    # 保留期的钟晚:预告一年前就挂上了,那就是到期日说了算。
    assert (delete_starts_at(老的, first_noticed=NOW - timedelta(days=365))
            == 早就到期)


def test_名单空的时候提示语说没有要过期的():
    fc = forecast([_info(1.0)], now=NOW, used_ratio=0.31)
    assert fc.runs == ()
    assert "31%" in fc.detail
    assert "没有" in fc.detail


def test_预告能转成线格式():
    fc = forecast([_info(120.0)], now=NOW, used_ratio=0.78)
    assert isinstance(fc, Forecast)
    wire = fc.to_wire()
    assert wire["used_ratio"] == pytest.approx(0.78)
    assert wire["bytes_at_risk"] == 1000
    assert wire["runs"][0]["mission"] == "巡检一号"
    assert wire["detail"] == fc.detail


# ----------------------------------------------------------------- 落盘


def test_预告写下去再读回来(tmp_path):
    fc = forecast([_info(120.0)], now=NOW, used_ratio=0.78)
    write_notice(tmp_path, fc)
    got = read_notice(tmp_path)
    assert got == {"巡检一号/20260504T120000Z": NOW}
    assert (tmp_path / NOTICE_REL).is_file()


def test_首次预告的时刻永不重写(tmp_path):
    # 每次预告都刷新时间戳的话,只要还在名单里就永远挂不满七天,删除永远
    # 轮不到 —— 盘会一直满下去。
    old = _info(120.0)
    write_notice(tmp_path, forecast([old], now=NOW, used_ratio=0.78))
    later = NOW + timedelta(days=3)
    write_notice(tmp_path, forecast([old], now=later, used_ratio=0.80))
    assert read_notice(tmp_path)[run_key(old)] == NOW


def test_不在名单里的键要清掉(tmp_path):
    # 否则这个文件只增不减,几年之后是一大坨早已不存在的目录。
    a, b = _info(120.0, mission="甲"), _info(119.0, mission="乙")
    write_notice(tmp_path, forecast([a, b], now=NOW, used_ratio=0.78))
    write_notice(tmp_path, forecast([b], now=NOW, used_ratio=0.78))
    assert set(read_notice(tmp_path)) == {run_key(b)}


def test_没有预告文件就是空的(tmp_path):
    assert read_notice(tmp_path) == {}


def test_预告文件坏了当成空的_而不是抛(tmp_path):
    # 读不出来只该让删除**推迟**(等于从没预告过),绝不能让整条链炸掉。
    (tmp_path / NOTICE_REL).write_text("{不是 json", encoding="utf-8")
    assert read_notice(tmp_path) == {}


def test_预告文件里的坏时间戳单条丢掉(tmp_path):
    (tmp_path / NOTICE_REL).write_text(json.dumps({
        "甲/20260504T120000Z": "不是时间",
        "乙/20260504T120000Z": "20260504T120000Z",
    }), encoding="utf-8")
    assert set(read_notice(tmp_path)) == {"乙/20260504T120000Z"}


def test_写预告是原子的(tmp_path):
    write_notice(tmp_path, forecast([_info(120.0)], now=NOW, used_ratio=0.78))
    assert not list(tmp_path.glob("*.tmp"))


def test_预告期就是七天():
    assert MIN_NOTICE_DAYS == 7
