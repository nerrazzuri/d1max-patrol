"""硬件组 C2 散热测试的满载脚本(``d1max-board-load``):占空比、温度稳定判据、温升换算、按码率写盘、
按码率上传(带收的那头)、命令行拼得对、短跑一遍出总结。"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from d1max_agent import boardload as bl


def test_占空比_写法和判定():
    assert bl.parse_duty("30/300") == (30.0, 300.0)
    assert bl.parse_duty("0") == (0.0, 0.0)
    for bad in ("300/30", "x", "-1/10", "5/0"):
        with pytest.raises(ValueError):
            bl.parse_duty(bad)
    assert bl.duty_on(0, 30, 300) and bl.duty_on(29.9, 30, 300)
    assert not bl.duty_on(30, 30, 300) and bl.duty_on(300, 30, 300)
    assert not bl.duty_on(5, 0, 0), "0 = 不开"


def test_温度稳定_满两小时且最近30分钟变化不到1度():
    st = bl.Stability()
    for i in range(0, 7200, 10):                       # 前 2 小时一直在升
        st.feed(i, 40 + i / 300)
    assert not st.stable(7190)
    t0 = 7200
    for i in range(0, 1800, 10):                       # 再 30 分钟只抖 0.5 度
        st.feed(t0 + i, 64 + (i % 20) / 40)
    assert st.stable(t0 + 1790)
    st2 = bl.Stability()
    for i in range(0, 1900, 10):                        # 30 分钟就平了,但不满 2 小时
        st2.feed(i, 50.0)
    assert not st2.stable(1890)


def test_温升换算到40度():
    assert bl.project(50.0, ambient=25.0) == pytest.approx(65.0)
    assert bl.project(50.0, ambient=None) is None


def test_按码率写盘_每2秒落盘_滚动删旧的_收尾删干净(tmp_path):
    syncs = []
    w = bl.DiskWriter(tmp_path / "rec", mbps=80.0, keep_mb=4, segment_mb=1,
                      fsync=lambda fd: syncs.append(fd), sync_every_s=0.2)
    w.start()
    time.sleep(1.0)
    w.stop()
    assert 6e6 <= w.written <= 12e6, w.written          # 80 Mbit/s × 1 s ≈ 10 MB
    assert len(syncs) >= 3
    assert not (tmp_path / "rec").exists() or not any((tmp_path / "rec").iterdir())
    assert w.max_kept_bytes <= 5 * 1024 * 1024


def test_按码率上传_收的那头收了就丢():
    sink = bl.Sink(0)
    port = sink.start()
    s = bl.Sender("127.0.0.1", port, mbps=40.0)
    s.start()
    time.sleep(1.0)
    s.stop()
    time.sleep(0.2)
    sink.stop()
    assert 3e6 <= s.sent <= 7e6, s.sent                 # 40 Mbit/s × 1 s ≈ 5 MB
    assert sink.received >= s.sent * 0.9


def test_上传对面不在_不炸_过一会儿重连():
    s = bl.Sender("127.0.0.1", 1, mbps=1.0, retry_s=0.1)
    s.start()
    time.sleep(0.3)
    s.stop()
    assert s.sent == 0 and s.error


def test_命令行_录包回放和视频编码拼得对(tmp_path):
    cmd = bl.replay_cmd("/usr/bin/d1max-loc", tmp_path / "b", tmp_path / "p", "0,0,0",
                        tmp_path / "o")
    assert cmd[:2] == ["/usr/bin/d1max-loc", "replay"] and "--bag" in cmd and "--prior" in cmd
    v = bl.video_cmd("/usr/bin/ffmpeg")
    assert "libx264" in v and "testsrc2=size=1280x720:rate=15" in " ".join(v) and v[-1] == "-"


def test_参数_没开任何负载就拒_录包要配先验图():
    with pytest.raises(SystemExit):
        bl.main(["--minutes", "1"])
    with pytest.raises(SystemExit):
        bl.main(["--replay-bag", "/tmp/x", "--minutes", "1"])


def test_短跑一遍_两个满载进程_出温度日志和总结(tmp_path, capsys):
    rc = bl.main(["--cpu", "2", "--minutes", "0.05", "--log-dir", str(tmp_path / "log"),
                  "--period", "1", "--status-every", "1", "--ambient", "25"])
    out = capsys.readouterr().out
    assert rc == 0
    assert list((tmp_path / "log").glob("board-*.csv"))
    assert "总结" in out and "最高" in out


def test_帮助文字不提狗():
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        bl.main(["--help"])
    assert "狗" not in buf.getvalue() and "dog" not in buf.getvalue().lower()


def test_灯和警笛按占空比开关(tmp_path):
    calls = []

    class 假上装:
        def has(self, o):
            return True

        async def set(self, output, on, max_s, **kw):
            calls.append((output, on))
    duty = bl.Duty(假上装(), {"strobe": (2.0, 4.0), "siren": (0.0, 0.0)})

    async def run():
        for t in (0.0, 1.0, 2.0, 3.0, 4.0):
            await duty.step(t)
        await duty.off()
    asyncio.run(run())
    assert calls == [("strobe", True), ("strobe", False), ("strobe", True), ("strobe", False)]
    assert threading.active_count() >= 1
