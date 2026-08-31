"""命令行入口。对着仿真器跑,断言退出码与关键输出。

订正 A(已实测证伪,见 task-17-report.md):正文原样的"同步测试 + 异步 sim
fixture + main() 内部 asyncio.run()"组合会挂 —— sim fixture 的事件循环在
同步测试函数体执行期间不转,握手等不到应答,一路挂到 connect_timeout_s
(默认 5s)超时,main() 于是稳定返回 1(而非期望的 0)。所以但凡测试要让
仿真器真的干活,一律改成 `async def`,直接 `await _amain(...)`,与仿真器
共享同一个事件循环。不碰仿真器的三条(覆盖 main() 这层壳本身:argparse
接线、asyncio.run、连不上时的退出码)保持同步,照原样调 main()。
"""

import pytest

from d1max_patrol.cli import _amain, build_parser, main
from d1max_sim.nav_server import SimNavServer


@pytest.fixture
async def sim():
    server = SimNavServer(tick_hz=100.0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def run(sim, *args: str) -> int:
    argv = ["--url", sim.url, "--timeout", "30", *args]
    return await _amain(build_parser().parse_args(argv))


def test_没有子命令时打印帮助并返回2():
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2


def test_解析器认识所有子命令():
    parser = build_parser()
    for argv in (["status"], ["maps"], ["paths", "m"], ["map-start"], ["map-stop"],
                 ["load", "m"], ["goto", "1", "2"], ["walk", "m", "p"],
                 ["path-save", "m", "p", "--point", "A:1:2"],
                 ["speed"], ["sim"]):
        assert parser.parse_args(argv) is not None


def test_连不上时返回1(capsys):
    assert main(["--url", "ws://127.0.0.1:1", "status"]) == 1
    assert "连接" in capsys.readouterr().err


async def test_status(sim, capsys):
    assert await run(sim, "status") == 0
    out = capsys.readouterr().out
    assert "StandBy" in out
    assert "Init" in out
    assert "x=" in out


async def test_maps_初始为空(sim, capsys):
    assert await run(sim, "maps") == 0
    assert "没有地图" in capsys.readouterr().out


async def test_建图与列图(sim, capsys):
    assert await run(sim, "map-start") == 0
    assert await run(sim, "map-stop") == 0
    assert await run(sim, "maps") == 0
    assert "map_1" in capsys.readouterr().out


async def test_load_与_goto(sim, capsys):
    await run(sim, "map-start")
    await run(sim, "map-stop")
    assert await run(sim, "load", "map_1") == 0
    assert "ContinuousLoc" in capsys.readouterr().out

    assert await run(sim, "goto", "1", "0", "0") == 0
    assert "Succeed" in capsys.readouterr().out


async def test_goto_在定位未就绪时返回1(sim, capsys):
    assert await run(sim, "goto", "1", "0") == 1
    assert "定位" in capsys.readouterr().err


async def test_paths_与_walk(sim, capsys):
    from d1max_patrol.protocol.nav_types import Pose, Waypoint

    map_id = sim.store.create_map("厂区")
    sim.store.set_path(map_id, "一号线", [
        Waypoint("P1", Pose.from_xy_yaw(1.0, 0.0, 0.0)),
        Waypoint("P2", Pose.from_xy_yaw(1.0, 1.0, 1.5708)),
    ])

    assert await run(sim, "paths", map_id) == 0
    out = capsys.readouterr().out
    assert "一号线" in out and "P1" in out and "P2" in out

    assert await run(sim, "load", map_id) == 0
    assert await run(sim, "walk", map_id, "一号线") == 0
    out = capsys.readouterr().out
    assert "1/2" in out and "2/2" in out
    assert "全部完成" in out


async def test_path_save_写入并读回(sim, capsys):
    map_id = sim.store.create_map("厂区")
    assert await run(sim, "path-save", map_id, "新线",
                      "--point", "配电柜:1.0:2.0:1.57", "--point", "水泵:3.0:4.0") == 0
    assert "2 个点" in capsys.readouterr().out

    assert await run(sim, "paths", map_id) == 0
    out = capsys.readouterr().out
    assert "配电柜" in out and "水泵" in out and "x=3.00" in out


async def test_path_save_覆盖已有路线(sim, capsys):
    map_id = sim.store.create_map("厂区")
    await run(sim, "path-save", map_id, "线", "--point", "A:1:0", "--point", "B:2:0")
    assert await run(sim, "path-save", map_id, "线", "--point", "A:1:0") == 0
    assert await run(sim, "paths", map_id) == 0
    assert "(1 点)" in capsys.readouterr().out


async def test_path_save_航点格式错误返回1(sim, capsys):
    map_id = sim.store.create_map()
    assert await run(sim, "path-save", map_id, "线", "--point", "缺了坐标") == 1
    assert "格式" in capsys.readouterr().err


async def test_walk_遇到不存在的路线返回1(sim, capsys):
    map_id = sim.store.create_map()
    assert await run(sim, "walk", map_id, "没这条线") == 1
    assert "没这条线" in capsys.readouterr().err


async def test_walk_中途失败即刻停下(sim, capsys):
    from d1max_patrol.protocol.nav_types import Pose, Waypoint

    map_id = sim.store.create_map("厂区")
    sim.store.set_path(map_id, "线", [
        Waypoint("P1", Pose.from_xy_yaw(1.0, 0.0, 0.0)),
        Waypoint("P2", Pose.from_xy_yaw(2.0, 0.0, 0.0)),
    ])
    await run(sim, "load", map_id)
    sim.faults.fail_next_nav = True

    assert await run(sim, "walk", map_id, "线") == 1
    err = capsys.readouterr().err
    assert "P1" in err and "Failed" in err


async def test_speed_读与写(sim, capsys):
    assert await run(sim, "speed") == 0
    assert "x=0.8" in capsys.readouterr().out

    assert await run(sim, "speed", "0.5", "--y", "0.3", "--z", "1.0") == 0
    out = capsys.readouterr().out
    assert "x=0.5" in out and "y=0.3" in out and "z=1.0" in out


async def test_超时参数会传下去(sim, capsys):
    await run(sim, "map-start")
    await run(sim, "map-stop")
    await run(sim, "load", "map_1")
    sim.faults.stuck = True
    argv = ["--url", sim.url, "--timeout", "1", "goto", "5", "0"]
    assert await _amain(build_parser().parse_args(argv)) == 1
    assert "超时" in capsys.readouterr().err
