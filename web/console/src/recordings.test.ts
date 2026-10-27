// W44 录像页(设计 §4.4):那一刻机器人在哪、点轨迹跳到它经过那里的时刻、时间轴每台一行带报警记号。
import { describe, expect, it } from "vitest";
import type { Alert } from "./model";
import { lanes, MAX_GAP_MS, nearestTime, poseAt, viewSpan, type TrackPt } from "./recordings";

const pt = (t: number, x: number, y: number, map = "m", version = "1"): TrackPt => ({ t, x, y, map_id: map, map_version: version });

describe("那一刻在哪", () => {
  const track = [pt(1000, 0, 0), pt(2000, 1, 0), pt(3000, 2, 0)];
  it("取那一刻之前最后一个点", () => {
    expect(poseAt(track, 2500)).toEqual(pt(2000, 1, 0));
    expect(poseAt(track, 2000)).toEqual(pt(2000, 1, 0));
  });
  it("第一个点之前不知道", () => {
    expect(poseAt(track, 500)).toBeNull();
  });
  it("poseAt_空档超过20秒算不知道", () => {
    expect(MAX_GAP_MS).toBe(20_000);
    expect(poseAt(track, 3000 + MAX_GAP_MS)).toEqual(pt(3000, 2, 0));
    expect(poseAt(track, 3000 + MAX_GAP_MS + 1)).toBeNull();
  });
});

describe("点轨迹跳时刻", () => {
  it("离点的那一处最近的点的时刻", () => {
    const track = [pt(1000, 0, 0), pt(2000, 5, 0), pt(3000, 10, 0)];
    expect(nearestTime(track, 4.4, 0.3, { map_id: "m", version: "1" })).toBe(2000);
  });
  it("nearestTime_只看同一张图", () => {
    const track = [pt(1000, 0, 0), pt(2000, 5, 0, "m", "2")];
    expect(nearestTime(track, 2.5, 0, { map_id: "m", version: "1" })).toBe(1000);
    expect(nearestTime([pt(2000, 5, 0, "m", "2")], 5, 0, { map_id: "m", version: "1" })).toBeNull();
  });
  it("点得太远(3 米外)不算", () => {
    expect(nearestTime([pt(1000, 0, 0)], 10, 10, { map_id: "m", version: "1" })).toBeNull();
  });
});

describe("时间轴每台一行", () => {
  const day = { since: 0, until: 86_400_000 };
  const alert = (robot: string, at: number, audience: string): Alert => ({
    key: `${robot}${at}`, kind: "k", level: "P1", robot, title: "", detail: "", first_ms: at, last_ms: at, count: 1,
    acked_by: "", acked_ms: null, resolved_ms: null, audience,
  });
  it("lanes_报警和需要帮忙分开标", () => {
    const recs = [
      { id: 1, robot_id: "A", camera: "front", start_ms: 0, bytes: 1, keep: 0 },
      { id: 2, robot_id: "A", camera: "front", start_ms: 60_000, bytes: 1, keep: 0 },
      { id: 3, robot_id: "B", camera: "front", start_ms: 600_000, bytes: 1, keep: 0 },
    ];
    const got = lanes(["A", "B"], recs, [alert("A", 30_000, "intrusion"), alert("B", 700_000, "help"), alert("B", 800_000, "owner")], day, 60);
    expect(got).toEqual([
      { robot: "A", segs: [[0, 120_000]], marks: [{ t: 30_000, kind: "alarm" }] },
      { robot: "B", segs: [[600_000, 660_000]], marks: [{ t: 700_000, kind: "help" }] },
    ]);
  });
});

describe("时间轴拉宽拉窄", () => {
  const day = { since: 0, until: 86_400_000 };
  it("放大以鼠标那一刻为中心,最窄 5 分钟,最宽一整天,不出这一天", () => {
    expect(viewSpan(day, day, 43_200_000, 0.5)).toEqual({ since: 21_600_000, until: 64_800_000 });
    const tiny = viewSpan(day, { since: 0, until: 400_000 }, 100_000, 0.01);
    expect(tiny.until - tiny.since).toBe(300_000);
    expect(viewSpan(day, { since: 1000, until: 2000 + 300_000 }, 0, 1000)).toEqual(day);
    expect(viewSpan(day, day, 0, 0.1)).toEqual({ since: 0, until: 8_640_000 });
    expect(viewSpan(day, day, 86_400_000, 0.1)).toEqual({ since: 77_760_000, until: 86_400_000 });
  });
});
