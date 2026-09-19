#!/usr/bin/env python3
"""D1 Max 标定动作 SDK 驱动 —— 复用项目 SidecarDeviceBackend 的 walk/stand/lie。

用途:录标定包时,由脚本发一套多轴激励动作(yaw 多样 + 前后加减速 + 侧移 +
8字 + stand↔lie 补 pitch/Z),解决 IMU-雷达外参弱可观。**录包另开一个终端跑
`record_bag.sh calib`**(本脚本只管动,不管录)。

安全:
- 默认 **dry-run**(只打印计划,不动)。真要动必须 `--arm`。
- `--test`:只发一小拍前进 + halt,现场先确认方向/死区/安全。
- 全程 try/finally 兜底 `halt()`;任何异常都先停车再退出。
- **现场必须有人守硬急停。** walk 有硬上限(≤10s, |amt|≤0.5),这里再收紧。

前提:**旁路进程已在开机窗口抢到控制权**(重启狗时抢握手)。否则 acquire 抛错。

用法:
  python3 calibrate_drive.py --host <Orin_IP> [--port 8090] --dry-run      # 看计划
  python3 calibrate_drive.py --host <Orin_IP> --test                       # 小步试
  python3 calibrate_drive.py --host <Orin_IP> --measure-yaw                 # 测 yaw 角速率
  python3 calibrate_drive.py --host <Orin_IP> --arm                         # 正式跑全序列
调 yaw:先 --measure-yaw 转 5 秒,从 /odom/current_pose 或 IMU 看转了多少度,
       把 YAW_DEG_PER_S 改成实测值,再 --arm。
"""
from __future__ import annotations
import argparse, asyncio, sys

# ---- 可调参数(现场调;先小后大)----
FWD_AMT = 0.40          # 前进控制量(百分比;0.5≈0.5-0.6 m/s,死区~0.3)
LAT_AMT = 0.40          # 侧移控制量
YAW_AMT = 0.40          # 转向控制量(死区 MIN_YAW=0.30)
YAW_DEG_PER_S = 30.0    # ★ 未实测的估计!先 --measure-yaw 校准再正式跑
PULSE_MAX = 4.0         # 单拍最长(收紧;backend 上限 10s)
STATIC_S = 10.0

def yaw_secs(deg: float) -> float:
    return max(0.3, min(PULSE_MAX, deg / YAW_DEG_PER_S))

async def run(args):
    # 延迟导入,--dry-run 不需要连狗也能看计划
    if not args.dry_run:
        sys.path.insert(0, "src")
        from d1max_patrol.backends.sidecar_device import SidecarDeviceBackend
        dev = SidecarDeviceBackend(host=args.host, port=args.port)
    else:
        dev = None

    async def walk(sec, fwd=0.0, lat=0.0, yaw=0.0, note=""):
        print(f"  walk {sec:.1f}s fwd={fwd:+.2f} lat={lat:+.2f} yaw={yaw:+.2f}  {note}")
        if dev: await dev.walk(seconds=sec, forward=fwd, lateral=lat, yaw=yaw); await asyncio.sleep(0.2)
    async def stand(): print("  stand");
    async def do_stand():
        print("  stand");
        if dev: await dev.stand(); await asyncio.sleep(2.0)
    async def do_lie():
        print("  lie");
        if dev: await dev.lie(); await asyncio.sleep(2.0)
    async def hold(sec, note=""):
        print(f"  静止 {sec:.0f}s  {note}")
        if dev: await asyncio.sleep(sec)

    # 连接 + 核对控制权
    if dev:
        await dev.connect()
        try:
            await dev.acquire_control()
        except Exception as e:
            print(f"!! 拿不到控制权:{e}\n   → 旁路进程没抢到握手。重启狗、抢握手后再来。")
            await dev.close(); return
        print("已核对:旁路进程握着控制权。")

    try:
        if args.test:
            print("[TEST] 一小拍前进 0.4s,然后停。看方向对不对、死区够不够。")
            await walk(0.4, fwd=FWD_AMT, note="TEST")
            if dev: await dev.halt()
            print("[TEST] 完成。方向/幅度 OK 就 --arm 正式跑。")
            return
        if args.measure_yaw:
            print(f"[MEASURE] 以 yaw={YAW_AMT} 转 5s。转完从 /odom/current_pose 或 IMU 读转过角度,")
            print(f"          YAW_DEG_PER_S = 角度/5。当前估计 {YAW_DEG_PER_S}。")
            await walk(5.0, yaw=YAW_AMT, note="measure")
            if dev: await dev.halt()
            return

        # ===== 全标定序列(~5 分钟)=====
        print("=== 标定序列开始(务必已开 record_bag.sh calib)===")
        await hold(STATIC_S, "初始静止")
        print("[2] yaw ±90° ×3");
        for _ in range(3):
            await walk(yaw_secs(90), yaw=+YAW_AMT); await walk(yaw_secs(90), yaw=-YAW_AMT)
        print("[3] yaw ±180° ×2")
        for _ in range(2):
            await walk(yaw_secs(180), yaw=+YAW_AMT); await walk(yaw_secs(180), yaw=-YAW_AMT)
        print("[4] 360° 正反各一")
        await walk(yaw_secs(360), yaw=+YAW_AMT); await walk(yaw_secs(360), yaw=-YAW_AMT)
        print("[5] 前后加减速 3-5m")
        for amt in (0.30, 0.45, 0.30):     # 变速
            await walk(3.0, fwd=amt); await walk(3.0, fwd=-amt)
        print("[6] 8字 ×2(前进+交替转向)")
        for _ in range(2):
            await walk(3.0, fwd=FWD_AMT, yaw=+YAW_AMT); await walk(3.0, fwd=FWD_AMT, yaw=-YAW_AMT)
        if args.stand_lie:
            print("[7] stand↔lie ×3(补 pitch + 竖直 Z)")
            for _ in range(3):
                await do_lie(); await do_stand()
        else:
            print("[7] 跳过 stand/lie(未启用;加 --stand-lie 开启)—— pitch/Z 激励会不足")
        print("[8] 侧移 左右 ×3")
        for _ in range(3):
            await walk(2.0, lat=+LAT_AMT); await walk(2.0, lat=-LAT_AMT)
        print("[9] 8字 ×1")
        await walk(3.0, fwd=FWD_AMT, yaw=+YAW_AMT); await walk(3.0, fwd=FWD_AMT, yaw=-YAW_AMT)
        await hold(STATIC_S, "结束静止")
        print("=== 序列完成。停 record_bag.sh(Ctrl-C)===")
    except KeyboardInterrupt:
        print("\n!! 中断,停车")
    except Exception as e:
        print(f"\n!! 出错:{e},停车")
    finally:
        if dev:
            try: await dev.halt()
            except Exception: pass
            try: await dev.release_control()
            except Exception: pass
            await dev.close()
            print("已停车 + 还控制权 + 断开。")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="旁路进程 host(笔记本上填 Orin IP)")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--arm", action="store_true", help="真正驱动(否则 dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划,不连狗")
    ap.add_argument("--test", action="store_true", help="只发一小拍前进")
    ap.add_argument("--measure-yaw", action="store_true", help="转 5s 测 yaw 角速率")
    ap.add_argument("--stand-lie", action="store_true", help="序列里加 stand↔lie(补 pitch/Z)")
    args = ap.parse_args()
    if not (args.arm or args.test or args.measure_yaw):
        args.dry_run = True
        print("(未 --arm/--test/--measure-yaw → dry-run,只打印计划)\n")
    asyncio.run(run(args))

if __name__ == "__main__":
    main()
